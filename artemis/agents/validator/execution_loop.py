# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Per-turn action execution loop for the Validator.

Orchestrates, for each Operator decision: precondition safety-net checks
(XML-first with pixel fallback), local execution with retries, screenshot and
trace bookkeeping, and FailureAnalyzer-driven local repair.

Extracted from ``validator.py``; the public entry point remains
``ValidatorNode.__call__``, which delegates here. All patchable seams
(_exec_action, _validate_action_precondition*, _get_initial_screenshot, ...)
are dispatched through the node so existing test patches keep working.
"""

import asyncio
import base64
from dataclasses import dataclass
from pathlib import Path
import time
import uuid
from uuid import UUID

from artemis.agents.validator.failure_analyzer import (
    FailureAnalyzer,
    ValidationErrorCategory,
)
from artemis.data_engine.trace import CURRENT_TRACE_ID
from artemis.graph.state import State
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class _ActionOutcome:
    """Result of processing a single action item (pre-check + execution)."""

    action_item: dict
    success: bool = False
    error_msg: str = ""
    error_category: ValidationErrorCategory = ValidationErrorCategory.GENERAL
    post_screenshot_b64: str | None = None


async def _run_precondition_gate(
    node, session, action_item: dict, state: State, pre_screenshot_b64: str, original_coords: list
) -> tuple[bool, ValidationErrorCategory, str]:
    """Runs the pre-execution Safety Net: XML-first with pixel fallback."""
    is_explorer_candidate = action_item.get("target_class") == "ExplorerCandidate"
    is_ocr_element = "[OCR]" in str(action_item.get("target_class") or "")
    has_index_metadata = bool(
        (
            action_item.get("target_text")
            or action_item.get("target_bounds")
            or action_item.get("target_resource_id")
        )
        and not is_explorer_candidate
        and not is_ocr_element
    )

    if not has_index_metadata:
        # Fall back to Pixel-based validation for pure coordinate/non-index interactions
        return await node._validate_action_precondition_pixel(
            session, action_item, pre_screenshot_b64, original_coords, state=state
        )

    # Index-based validation is fastest and safest when metadata is present
    (
        validation_passed,
        validation_category,
        validation_error,
    ) = await node._validate_action_precondition(session, action_item, state=state)

    if validation_passed:
        return validation_passed, validation_category, validation_error

    if validation_category == ValidationErrorCategory.XML_BYPASSED:
        logger.info(
            "Pre-execution XML-based validation bypassed"
            f" ({validation_error}). Falling back to"
            " Pixel-based validation..."
        )
        return await node._validate_action_precondition_pixel(
            session, action_item, pre_screenshot_b64, original_coords, state=state
        )

    logger.info(
        "Pre-execution XML-based validation failed:"
        f" {validation_error}. Attempting Pixel-based"
        " validation fallback..."
    )
    (
        pixel_passed,
        pixel_category,
        pixel_error,
    ) = await node._validate_action_precondition_pixel(
        session, action_item, pre_screenshot_b64, original_coords, state=state
    )
    if pixel_passed and pixel_category == ValidationErrorCategory.NONE:
        logger.success(
            "Pixel-based validation fallback PASSED! Overriding XML validation failure."
        )
        return True, ValidationErrorCategory.NONE, ""

    return validation_passed, validation_category, validation_error


async def _attempt_local_execution(
    node, session, action_item: dict, action_name, attempts_log: list
) -> tuple[bool, str]:
    """Executes the action locally with a short retry loop."""
    max_local_retries = 1 if action_name == "launch_app" else 2
    success = False
    error_msg = ""
    for attempt in range(max_local_retries):
        try:
            exec_success, exec_error = await node._exec_action(session, action_item)
        except Exception as e:
            exec_success = False
            exec_error = str(e)

        # Skip polling: assume execution success implies action success
        if exec_success:
            success = True
            error_msg = ""
            attempts_log.append("Success")
            break
        else:
            success = False
            error_msg = exec_error
            attempts_log.append(error_msg)

        if attempt < max_local_retries - 1:
            await asyncio.sleep(0.5)

    return success, error_msg


async def _capture_live_screenshot(session, context_desc: str) -> str | None:
    try:
        return await session.screenshot_b64()
    except Exception as e:
        logger.error(f"Failed to capture {context_desc}: {e!r}")
        return None


async def _process_action(
    node, state: State, session, action_item: dict, action_name, pre_screenshot_b64: str
) -> _ActionOutcome:
    """Runs precondition checks and execution for one action item."""
    action_item = dict(action_item)  # Make a copy to avoid mutating original state actions
    outcome = _ActionOutcome(action_item=action_item)
    attempts_log = []

    # 1. Pre-execution validation (Safety Net)
    original_coords = list(action_item.get("coordinates") or [])
    (
        validation_passed,
        validation_category,
        validation_error,
    ) = await _run_precondition_gate(
        node, session, action_item, state, pre_screenshot_b64, original_coords
    )

    if not validation_passed:
        outcome.success = False
        outcome.error_msg = f"Pre-execution validation failed: {validation_error}"
        outcome.error_category = validation_category
        attempts_log.append(outcome.error_msg)

        # Capture the live mismatch screenshot to provide context for FailureAnalyzer
        outcome.post_screenshot_b64 = await _capture_live_screenshot(
            session, "live screenshot for failure analysis"
        )
    else:
        # 2. Local execution attempts
        outcome.success, outcome.error_msg = await _attempt_local_execution(
            node, session, action_item, action_name, attempts_log
        )
        if not outcome.success and not outcome.post_screenshot_b64:
            logger.info("Action failed, capturing failure screenshot...")
            outcome.post_screenshot_b64 = await _capture_live_screenshot(
                session, "failure screenshot"
            )

    # Enrich the executed action record
    if len(attempts_log) > 1 or not outcome.success:
        action_item["attempts"] = list(attempts_log)

    return outcome


def _absorb_post_screenshot(ctx, state: State, post_screenshot_b64: str) -> str | None:
    """Persists the post-action screenshot and points state at it.

    Returns the stored image name (None when no data engine is attached).
    """
    decoded_bytes = base64.b64decode(post_screenshot_b64)
    logger.info(f"Successfully decoded post_screenshot_b64 ({len(decoded_bytes)} bytes)")
    if not ctx.data_engine:
        return None

    post_image_name = ctx.data_engine.get_or_create_image(decoded_bytes)
    screenshot_path = str(ctx.data_engine.get_image_path(post_image_name))
    state.latest_screenshot = screenshot_path
    logger.info(f"Validator updated state.latest_screenshot to: {screenshot_path}")
    return post_image_name


def _record_action_start(ctx, step_id, action_name, action_item, parent_id, action_trace_id):
    if ctx.data_engine and step_id:
        ctx.data_engine.record_trace(
            type="action",
            name=action_name,
            payload={"action": action_item, "status": "running"},
            status="running",
            parent_trace_id=parent_id,
            step_id=step_id,
            trace_id=action_trace_id,
        )


def _record_action_end(
    ctx,
    step_id,
    action_name,
    outcome: _ActionOutcome,
    post_image_name,
    parent_id,
    action_trace_id,
    start_time: float,
):
    duration = time.time() - start_time
    if ctx.data_engine and step_id:
        relative_time = ctx.data_engine.get_relative_time(time.time())
        ctx.data_engine.record_trace(
            type="action",
            name=action_name,
            payload={
                "action": outcome.action_item,
                "success": outcome.success,
                "error_msg": outcome.error_msg,
                "post_screenshot": post_image_name,
                "timestamp": time.time(),
                "relative_time": relative_time,
            },
            status="success" if outcome.success else "failed",
            duration=duration,
            parent_trace_id=parent_id,
            step_id=step_id,
            trace_id=action_trace_id,
        )


def _reload_post_repair_screenshot(
    ctx, state: State, last_screenshot_b64: str, last_screenshot_name: str | None
) -> tuple[str, str | None]:
    """Reloads the latest screenshot updated by FailureAnalyzer during repair."""
    try:
        screenshot_path = getattr(state, "latest_screenshot", None)
        if screenshot_path:
            if Path(screenshot_path).exists():
                with open(screenshot_path, "rb") as f:
                    last_screenshot_b64 = base64.b64encode(f.read()).decode("utf-8")
                if ctx.data_engine:
                    last_screenshot_name = ctx.data_engine.get_or_create_image(
                        base64.b64decode(last_screenshot_b64)
                    )
                logger.info(f"Validator reloaded post-repair screenshot from: {screenshot_path}")
    except Exception as e:
        logger.error(f"Failed to reload post-repair screenshot: {e}")
    return last_screenshot_b64, last_screenshot_name


async def _analyze_failure(
    node,
    state: State,
    outcome: _ActionOutcome,
    *,
    decision_screenshot_b64,
    decision_screenshot_name,
    post_image_name,
    executed_actions,
    unexecuted_actions,
) -> dict:
    """Triggers the FailureAnalyzer for a failed action and records its analysis."""
    logger.warning(f"Action failed: {outcome.action_item}. Triggering failure analysis.")

    analyzer = FailureAnalyzer(node.ctx)
    analysis_result = await analyzer.analyze(
        state,
        outcome.action_item,
        outcome.error_msg,
        pre_screenshot=decision_screenshot_b64,
        post_screenshot=outcome.post_screenshot_b64,
        pre_screenshot_name=decision_screenshot_name,
        post_screenshot_name=post_image_name,
        executed_actions=executed_actions,
        unexecuted_actions=unexecuted_actions,
        error_category=outcome.error_category,
    )
    outcome.action_item["repair"] = analysis_result.get("analysis", "No analysis provided.")
    return analysis_result


async def run_validation_loop(node, state: State) -> dict:
    """Executes all pending Operator decisions and returns the execution report.

    ``node`` is the ValidatorNode facade; every overridable seam is invoked
    through it.
    """
    ctx = node.ctx

    actions, error_msg = node._parse_decisions(state.structured_decisions)
    if error_msg:
        return {}

    execution: list[dict] = []
    failed_action = None
    actions_to_execute = list(actions)

    session = await node._get_mcp_session()

    try:
        last_screenshot_b64, last_screenshot_name = await node._get_initial_screenshot(
            session, state
        )
        # Preserve the turn-initial screenshot (what the operator saw when making decisions)
        decision_screenshot_b64 = last_screenshot_b64
        decision_screenshot_name = last_screenshot_name
    except Exception:
        return {}

    step_id = UUID(state.current_step_id) if state.current_step_id else None

    analysis_result: dict = {}
    success = False

    while actions_to_execute:
        action_item = actions_to_execute.pop(0)
        action_name = action_item.get("action")

        logger.info(f"Executing action: {action_name}")

        action_trace_id = uuid.uuid4()
        parent_id = CURRENT_TRACE_ID.get()
        token = CURRENT_TRACE_ID.set(action_trace_id)
        start_time = time.time()

        _record_action_start(ctx, step_id, action_name, action_item, parent_id, action_trace_id)

        try:
            outcome = await _process_action(
                node, state, session, action_item, action_name, last_screenshot_b64
            )
            success = outcome.success
            execution.append(outcome.action_item)

            post_image_name = None
            if outcome.post_screenshot_b64:
                post_image_name = _absorb_post_screenshot(
                    ctx, state, outcome.post_screenshot_b64
                )
                last_screenshot_b64 = outcome.post_screenshot_b64
                if post_image_name:
                    last_screenshot_name = post_image_name

            _record_action_end(
                ctx,
                step_id,
                action_name,
                outcome,
                post_image_name,
                parent_id,
                action_trace_id,
                start_time,
            )
        finally:
            CURRENT_TRACE_ID.reset(token)

        if not success:
            failed_action = outcome.action_item

            analysis_result = await _analyze_failure(
                node,
                state,
                outcome,
                decision_screenshot_b64=decision_screenshot_b64,
                decision_screenshot_name=decision_screenshot_name,
                post_image_name=post_image_name,
                executed_actions=execution[:-1],
                unexecuted_actions=list(actions_to_execute),
            )

            if analysis_result.get("status") == "fixed":
                logger.success("Failure repaired locally!")
                actions_to_execute = []
                last_screenshot_b64, last_screenshot_name = _reload_post_repair_screenshot(
                    ctx, state, last_screenshot_b64, last_screenshot_name
                )
                continue
            else:
                break

    # Append any skipped actions to the execution log
    for skipped in actions_to_execute:
        skipped_copy = dict(skipped)
        skipped_copy["attempts"] = ["Skipped"]
        execution.append(skipped_copy)

    report = {
        "execution": execution,
        "status": (
            "success"
            if success or (failed_action and analysis_result.get("status") == "fixed")
            else "failed"
        ),
        "repair_status": (analysis_result.get("status") if failed_action else None),
    }

    # Determine if a distinct post-action screenshot was captured
    distinct_post_image_name = (
        last_screenshot_name
        if (last_screenshot_name and last_screenshot_name != decision_screenshot_name)
        else None
    )

    if ctx.data_engine and step_id:
        ctx.data_engine.update_step_execution_result(
            step_id, report, post_image_name=distinct_post_image_name
        )

    return {"last_execution_result": report}
