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

Two execution tiers, selected by how many turn-ending actions the Operator
emitted in the turn:

* **Vetted single action** (one action): the pre-execution Safety Net runs
  first (XML-first; the pixel judge decides only when the XML verdict is
  weak), then the action executes locally with a short retry loop.
* **Fast-action burst** (two or more actions): the first member is vetted by
  the same Safety Net (the screen it was decided on is still the live screen)
  and a refusal aborts the whole burst; the remaining members fire back to
  back with no safety net, no retries and no screenshots in between. The burst
  is the Operator's tool against turn latency (transient menus, toasts,
  control bars); the Operator owns the risk. The first failure aborts the rest.

Either way, a failure does not spawn a repair agent. The loop opens an
*execution incident* (:mod:`artemis.agents.validator.incidents`) that rides in
graph state and in the step's execution report, and the Operator resolves it
on its next turn with the live screen in front of it.

Extracted from ``validator.py``; the public entry point remains
``ValidatorNode.__call__``, which delegates here. All patchable seams
(_exec_action, _validate_action_precondition*, _get_initial_screenshot, ...)
are dispatched through the node so existing test patches keep working.
"""

import asyncio
import base64
from dataclasses import dataclass
import time
import uuid
from uuid import UUID

from artemis.agents.validator.categories import ValidationErrorCategory
from artemis.agents.validator.incidents import (
    KIND_EXEC_ERROR,
    KIND_SAFETY_NET,
    open_incident,
)
from artemis.agents.validator.precondition_xml import XML_VERDICT_STRONG
from artemis.data_engine.trace import CURRENT_TRACE_ID
from artemis.graph.state import State
from artemis.utils.coordinates import normalize_action_dict
from artemis.utils.logger import get_logger
from artemis.utils.task_tree import format_action_clean

logger = get_logger(__name__)

#: ``attempts`` marker for actions a burst never reached.
BURST_SKIPPED_MARKER = "Skipped (burst aborted)"


@dataclass
class _ActionOutcome:
    """Result of processing a single action item (pre-check + execution)."""

    action_item: dict
    success: bool = False
    error_msg: str = ""
    error_category: ValidationErrorCategory = ValidationErrorCategory.GENERAL
    intercepted: bool = False
    post_screenshot_b64: str | None = None


#: ``safety_net_evidence`` keys the incident carries through verbatim (how the
#: verdict was reached), next to the normalized location/occupant facts.
_VERDICT_EVIDENCE_KEYS = ("xml_verdict", "xml_element_count", "xml_weak_reason", "pixel_judge")


def _note_pixel_judge(action_item: dict, note: str) -> None:
    """Records on the action item how the pixel judge figured in the verdict."""
    action_item.setdefault("safety_net_evidence", {})["pixel_judge"] = note


async def _run_precondition_gate(
    node, session, action_item: dict, state: State, pre_screenshot_b64: str, original_coords: list
) -> tuple[bool, ValidationErrorCategory, str]:
    """Runs the pre-execution Safety Net: XML-first, pixel judge for weak verdicts.

    The pixel judge (a VLM call) is a fallback for when the hierarchy could not
    see the target, never a second opinion when it could. A STRONG XML failure
    (healthy, non-trivial tree; the target's identifiers were searched and not
    found) is final and blocks the action without consulting the judge. A WEAK
    one (XML bypassed/timed out, sparse tree, identifier-less target) hands the
    decision to the judge. The path taken is recorded in ``safety_net_evidence``
    and appended to the reason as one short clause.
    """
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

    async def _pixel_judge(note: str) -> tuple[bool, ValidationErrorCategory, str]:
        passed, category, error = await node._validate_action_precondition_pixel(
            session, action_item, pre_screenshot_b64, original_coords, state=state
        )
        if not passed:
            _note_pixel_judge(action_item, note)
        return passed, category, error

    if not has_index_metadata:
        # Pure coordinate/non-index interactions: the pixel judge is the only gate.
        return await _pixel_judge("consulted alone (target had no hierarchy metadata)")

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
        return await _pixel_judge("consulted alone (XML bypassed)")

    evidence = action_item.get("safety_net_evidence") or {}
    if evidence.get("xml_verdict") == XML_VERDICT_STRONG:
        clause = (
            f"XML verdict final: {evidence.get('xml_element_count')} nodes searched for the"
            " target's identifiers; pixel judge not consulted"
        )
        _note_pixel_judge(action_item, "not consulted (strong XML verdict)")
        logger.info(f"Pre-execution XML-based validation failed: {validation_error}. {clause}.")
        return False, validation_category, f"{validation_error} [{clause}]"

    weak_reason = evidence.get("xml_weak_reason") or "no verdict strength recorded"
    logger.info(
        f"Pre-execution XML-based validation failed: {validation_error}. XML evidence is"
        f" weak ({weak_reason}); asking the pixel judge..."
    )
    pixel_passed, pixel_category, _pixel_error = await _pixel_judge(
        "consulted (weak XML verdict); did not confirm the target"
    )
    if pixel_passed and pixel_category == ValidationErrorCategory.NONE:
        logger.success("Pixel judge confirmed the target is present; overriding weak XML verdict.")
        action_item.pop("safety_net_evidence", None)
        return True, ValidationErrorCategory.NONE, ""

    clause = f"XML evidence weak ({weak_reason}); pixel judge did not confirm the target"
    return validation_passed, validation_category, f"{validation_error} [{clause}]"


async def _attempt_local_execution(
    node, session, action_item: dict, action_name, attempts_log: list, *, max_local_retries: int
) -> tuple[bool, str]:
    """Executes the action locally with a short retry loop."""
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
    node,
    state: State,
    session,
    action_item: dict,
    action_name,
    pre_screenshot_b64: str,
    *,
    burst: bool,
    vetted: bool = True,
) -> _ActionOutcome:
    """Runs precondition checks (``vetted`` items only) and execution for one action item.

    ``vetted`` is True for a single action and for the first member of a burst;
    later burst members fire unvetted. ``burst`` members never retry.
    """
    action_item = dict(action_item)  # Make a copy to avoid mutating original state actions
    outcome = _ActionOutcome(action_item=action_item)
    attempts_log = []

    validation_passed = True
    if vetted:
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
            outcome.intercepted = True
            outcome.error_msg = f"Pre-execution validation failed: {validation_error}"
            outcome.error_category = validation_category
            attempts_log.append(outcome.error_msg)

    if validation_passed:
        # 2. Local execution: a vetted action gets a short retry loop, a burst
        # member fires exactly once (retries would break the burst's timing).
        if burst:
            max_local_retries = 1
        else:
            max_local_retries = 1 if action_name == "launch_app" else 2
        outcome.success, outcome.error_msg = await _attempt_local_execution(
            node,
            session,
            action_item,
            action_name,
            attempts_log,
            max_local_retries=max_local_retries,
        )

    if not outcome.success:
        logger.info("Action failed, capturing failure screenshot...")
        outcome.post_screenshot_b64 = await _capture_live_screenshot(session, "failure screenshot")

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
    *,
    burst: bool,
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
                "burst": burst,
            },
            status="success" if outcome.success else "failed",
            duration=duration,
            parent_trace_id=parent_id,
            step_id=step_id,
            trace_id=action_trace_id,
        )


def _normalize_point(ctx, point) -> list[int] | None:
    """Pixel [x, y] -> normalized 0-1000 [x, y] using the device dims."""
    try:
        x, y = int(point[0]), int(point[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    width = getattr(getattr(ctx, "device", None), "device_width", None) or 1080
    height = getattr(getattr(ctx, "device", None), "device_height", None) or 2400
    return [
        max(0, min(1000, round(x * 1000 / width))),
        max(0, min(1000, round(y * 1000 / height))),
    ]


def _incident_evidence(ctx, outcome: _ActionOutcome) -> dict:
    """Structured facts the safety net attached to the action item, normalized."""
    raw = outcome.action_item.pop("safety_net_evidence", None) or {}
    evidence: dict = {}
    if raw.get("new_center"):
        normalized = _normalize_point(ctx, raw["new_center"])
        if normalized:
            evidence["new_location"] = normalized
        if raw.get("new_bounds") is not None:
            evidence["new_bounds"] = raw["new_bounds"]
    if raw.get("occupant"):
        evidence["occupant"] = raw["occupant"]
        if raw.get("occupant_bounds") is not None:
            evidence["occupant_bounds"] = raw["occupant_bounds"]
    for key in _VERDICT_EVIDENCE_KEYS:
        if raw.get(key) is not None:
            evidence[key] = raw[key]
    return evidence


def _current_step_number(ctx, state: State) -> int | None:
    step_id = getattr(state, "current_step_id", None)
    if ctx.data_engine and step_id:
        try:
            return ctx.data_engine.get_step_number(UUID(step_id))
        except Exception:
            return None
    return None


def _model_facing_action(ctx, action_item: dict) -> dict:
    """The action item in the Operator's own 0–1000 coordinate space.

    The live item carries the controller's pixels; every phrase rendered for
    a model is normalized first, and the space marker makes that idempotent.
    """
    width = getattr(getattr(ctx, "device", None), "device_width", None) or 1080
    height = getattr(getattr(ctx, "device", None), "device_height", None) or 2400
    return normalize_action_dict(action_item, width, height)


def _build_incident(ctx, state: State, outcome: _ActionOutcome, *, index: int, total: int) -> dict:
    step_number = _current_step_number(ctx, state)
    return open_incident(
        previous=getattr(state, "open_incident", None),
        kind=KIND_SAFETY_NET if outcome.intercepted else KIND_EXEC_ERROR,
        category=outcome.error_category,
        reason=outcome.error_msg,
        action_item=outcome.action_item,
        action_description=format_action_clean(_model_facing_action(ctx, outcome.action_item)),
        action_index=index,
        burst_size=total,
        step_number=step_number,
        evidence=_incident_evidence(ctx, outcome),
    )


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
    actions_to_execute = list(actions)
    total_actions = len(actions_to_execute)
    burst = total_actions > 1
    if burst:
        logger.info(
            f"Fast-action burst: {total_actions} actions; the first is vetted by the"
            " safety net, the rest fire back to back unvetted."
        )

    session = await node._get_mcp_session()

    try:
        last_screenshot_b64, last_screenshot_name = await node._get_initial_screenshot(
            session, state
        )
        # Preserve the turn-initial screenshot (what the operator saw when making decisions)
        decision_screenshot_name = last_screenshot_name
    except Exception:
        return {}

    step_id = UUID(state.current_step_id) if state.current_step_id else None

    incident: dict | None = None
    success = False
    index = 0

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
                node,
                state,
                session,
                action_item,
                action_name,
                last_screenshot_b64,
                burst=burst,
                # The first burst member was decided on the live screen, so the
                # safety net still applies to it; later members fire unvetted.
                vetted=(not burst or index == 0),
            )
            success = outcome.success
            execution.append(outcome.action_item)

            post_image_name = None
            if outcome.post_screenshot_b64:
                post_image_name = _absorb_post_screenshot(ctx, state, outcome.post_screenshot_b64)
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
                burst=burst,
            )
        finally:
            CURRENT_TRACE_ID.reset(token)

        if not success:
            incident = _build_incident(ctx, state, outcome, index=index, total=total_actions)
            logger.warning(
                f"Action failed: {outcome.action_item}. Opened execution incident"
                f" ({incident['kind']}/{incident['category']},"
                f" consecutive={incident['consecutive_failures']}) for the Operator."
            )
            break
        index += 1

    # Append any skipped actions to the execution log
    for skipped in actions_to_execute:
        skipped_copy = dict(skipped)
        skipped_copy["attempts"] = [BURST_SKIPPED_MARKER if burst else "Skipped"]
        execution.append(skipped_copy)

    report = {
        "execution": execution,
        "status": "success" if success else "failed",
        "burst": burst,
        "incident": incident,
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

    # A successful turn closes whatever incident was open (handing the Operator
    # a one-turn CLOSED notice); a failed one keeps and escalates it.
    closed = None
    previous = getattr(state, "open_incident", None)
    if success and isinstance(previous, dict) and previous.get("kind"):
        closed = dict(previous)
        closed["closed_at_step"] = _current_step_number(ctx, state)
    return {
        "last_execution_result": report,
        "open_incident": incident,
        "last_closed_incident": closed,
    }
