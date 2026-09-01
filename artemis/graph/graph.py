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

import asyncio
import base64
import functools
import json
from typing import Annotated, Literal

from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from langchain_core.tools.base import InjectedToolCallId
from langgraph.constants import END, START
from langgraph.graph import StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import InjectedState
from pydantic import BaseModel, Field

from artemis.agents.checker.checker import run_final_check, verdicts_allow_release
from artemis.agents.operator.operator import OperatorNode
from artemis.agents.planner.planner import (
    PlannerNode,
    run_async_planner_validation,
)
from artemis.agents.summarizer.summarizer import SummarizerNode
from artemis.agents.validator.validator import ValidatorNode
from artemis.context import ArtemisContext
from artemis.graph.checkpoints import (
    append_ledger_record,
    compute_run_outcome,
    final_check_enabled,
    harvest_finished_checkpoints,
    has_ledger_records,
    queue_checkpoints,
    read_ledger,
    revert_subgoal_status,
    settle_all_checkpoints,
    spawn_pending_checkpoints,
    write_run_outcome,
)
from artemis.graph.perception import perception_node
from artemis.graph.state import State
from artemis.graph.visibility import strict_state
from artemis.tools.command_tool import (
    manage_task_wrapper,
    run_adb_command_wrapper,
)
from artemis.tools.committee_tool import ask_committee_wrapper
from artemis.tools.diagnostic_tool import ask_diagnoser_wrapper
from artemis.tools.explorer_tool import ask_explorer_wrapper
from artemis.tools.index import get_tools_from_wrappers
from artemis.tools.scratchpad import (
    append_note_wrapper,
    list_notes_wrapper,
    read_note_wrapper,
    save_note_wrapper,
    update_note_wrapper,
)
from artemis.tools.tool_wrapper import invoke_tool_with_injection
from artemis.tools.video_tool import get_video_analyzer_tool
from artemis.utils.logger import get_logger
from artemis.utils.notes import (
    SAVE_NOTE_ARG_CONTENT_DESC,
    SAVE_NOTE_ARG_KEY_DESC,
    UPDATE_NOTE_ARG_KEY_DESC,
    UPDATE_NOTE_ARG_REPLACEMENT_DESC,
    UPDATE_NOTE_ARG_TARGET_DESC,
    get_note_file_path,
)
from artemis.utils.plan_grammar import (
    milestones_changed,
    new_top_level_completions,
    parse_plan,
    restore_missing_check_items,
    subgoal_hash,
    unintended_milestone_edits,
)
from artemis.utils.task_tree import get_active_subgoal_hashes

logger = get_logger(__name__)


def convergence_node(state: State):
    """Convergence point for parallel execution paths."""
    return {}


async def execution_check_node(state: State, ctx: ArtemisContext):
    state = strict_state(state, "execution_check")
    logger.info("Starting execution_check_node")

    if not ctx.data_engine:
        return {"checker_success": True}

    checker_success = True

    def _record_turn(extra_metadata: dict) -> str | None:
        """Records this Operator turn in the DataEngine — unconditionally: every
        turn (including planner-rejected ones) leaves a step record."""
        raw_data = state.operator_raw_data
        if not raw_data:
            return None
        try:
            screenshot_b64 = raw_data.get("screenshot_b64")
            xml_hierarchy = raw_data.get("xml_hierarchy")
            ocr_results = raw_data.get("ocr_results")

            screenshot_bytes = base64.b64decode(screenshot_b64)

            action_taken = None
            if state.structured_decisions:
                try:
                    action_taken = json.loads(state.structured_decisions)
                except Exception as e:
                    logger.warning(f"Failed to parse structured decisions: {e}")

            subgoal_hash_val, sub_subgoal_hash = _get_active_subgoal_hashes(ctx)
            step_id = ctx.data_engine.record_step(
                pre_screenshot_bytes=screenshot_bytes,
                ui_tree=xml_hierarchy,
                ocr_result=ocr_results,
                action_taken=action_taken,
                operator_raw_thinking=getattr(state, "operator_raw_thinking", None),
                operator_native_thinking=getattr(state, "operator_native_thinking", None),
                extra_metadata={
                    "subgoal_hash": subgoal_hash_val,
                    "sub_subgoal_hash": sub_subgoal_hash,
                    "width": raw_data.get("width"),
                    "height": raw_data.get("height"),
                    **extra_metadata,
                },
            )
            logger.info(f"Recorded step in DataEngine: {step_id}")
            return str(step_id)
        except Exception as e:
            logger.error(f"Failed to record step in DataEngine: {e}")
            return None

    # Harvest finished checkpoint attempts (non-blocking: done() tasks only).
    check_findings = harvest_finished_checkpoints(ctx, state)

    if hasattr(ctx, "planner_task") and ctx.planner_task:
        logger.info("Waiting for planner validation task to complete...")
        awaited_planner_task = ctx.planner_task
        try:
            planner_result = await awaited_planner_task
            ctx.planner_task = None

            if planner_result and planner_result.get("status") == "failed":
                logger.warning("Planner rejected the task plan changes.")

                # Targeted rollback: restore task_plan.md to the last content
                # consistent with the validated baseline
                if ctx.task_plan_content_before is not None:
                    task_plan_path = get_note_file_path(ctx.data_engine.base_dir, "task_plan")
                    if task_plan_path.exists():
                        task_plan_path.write_text(ctx.task_plan_content_before, encoding="utf-8")
                        logger.info("Rolled back task_plan.md to its original state.")
                ctx.pending_validated_plan = None
                ctx.task_plan_content_before = None

                # Surface the rejection through operator_feedback — the only
                # feedback channel the Operator's next prompt actually renders.
                feedback = planner_result.get("feedback", "Your task plan changes were rejected.")
                check_findings.append(
                    "[planner] Your recent modification to the top-level task"
                    " plan was rejected and the plan was rolled back to its"
                    f" previous state. Reason: {feedback} Any terminal actions"
                    " you outputted were not executed. Review the task goal"
                    " again and consider what the optimal strategy is now."
                )

                # Even a rejected turn leaves a step record: the record reflects
                # the decision made and the fact that its terminal actions were
                # intercepted before execution.
                rejected_step_id = _record_turn({"planner_rejected": True})

                return {
                    "checker_success": False,
                    "structured_decisions": "",
                    "current_step_id": rejected_step_id,
                    "operator_feedback": check_findings or None,
                }

            else:
                logger.info("Planner approved the task plan changes.")
                # Advance the ratchet baseline: the validated content becomes
                # the new reference for future drift comparisons.
                if ctx.pending_validated_plan is not None:
                    ctx.last_validated_plan = ctx.pending_validated_plan
                ctx.pending_validated_plan = None
                ctx.task_plan_content_before = None
        except asyncio.CancelledError:
            if not awaited_planner_task.cancelled():
                raise  # this node itself is being cancelled, not the awaited task
            logger.info("Planner validation task was superseded by a newer plan write.")
            ctx.planner_task = None
        except Exception as e:
            logger.error(f"Planner validation task failed: {e}")

    # Every Operator turn is recorded — verification never gates history.
    current_step_id = _record_turn({})

    # Spawn queued checkpoints only now: the just-recorded step is the evidence
    # anchor (its pre screenshot plus the previous turn's post state capture
    # the completion moment). Superseded-but-finished attempts are booked here.
    check_findings.extend(await spawn_pending_checkpoints(ctx, state, current_step_id))

    update = {
        "checker_success": checker_success,
        "current_step_id": current_step_id,
        "operator_feedback": check_findings or None,
    }
    if checker_success:
        update["subagent_calls"] = []
    return update


async def exit_settlement_node(state: State, ctx: ArtemisContext):
    """Two-phase exit: settlement (unconditional) then final review (gated).

    Phase 1 — settlement barrier: the only intentional blocking wait in the
    whole flow. Every started check attempt gets booked into the ledger before
    exit, no matter which switches are on.

    Phase 2 — final review (``disable_final_check`` off): audits the USER'S
    ORIGINAL GOAL plus all declared check items. Unmet verify criteria route
    back into the loop (bounded by ``final_check_max_attempts``); assert
    failures never do.
    """
    state = strict_state(state, "exit_settlement")
    logger.info("Starting exit_settlement_node")
    update: dict = {"exit_settlement_route": "end"}
    if not ctx.data_engine:
        return update

    # Phase 1: settle every outstanding attempt (bounded by settlement_timeout).
    await settle_all_checkpoints(ctx, state)
    # Queued-but-never-spawned checkpoints are dropped here: their items simply
    # remain unchecked and are reported as such.
    ctx.pending_checkpoints.clear()

    base_dir = ctx.data_engine.base_dir
    plan_text = ""
    plan_path = get_note_file_path(base_dir, "task_plan")
    if plan_path.exists():
        try:
            plan_text = plan_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.error(f"Failed to read task plan at settlement: {e}")
    snapshot = parse_plan(plan_text)

    setup = ctx.execution_setup
    user_stopped = bool(getattr(state, "user_stop_requested", False))
    halted = bool(getattr(ctx, "assert_halt", False))
    max_attempts = int(getattr(setup, "final_check_max_attempts", 3) or 3) if setup else 3

    verify_blocked = False
    blocked_findings: list[str] = []
    if final_check_enabled(ctx):
        attempt_no = ctx.final_check_attempts + 1
        ctx.final_check_attempts = attempt_no
        checkpoint_timeout = float(getattr(setup, "checkpoint_timeout", 180.0) or 180.0)
        ledger = read_ledger(base_dir)
        try:
            report = await asyncio.wait_for(
                run_final_check(
                    ctx,
                    goal=getattr(state, "initial_goal", ""),
                    plan_text=plan_text,
                    ledger=ledger,
                    check_items=list(snapshot.all_check_items),
                ),
                timeout=checkpoint_timeout,
            )
        except Exception as e:
            # Fail-open: release, but the verdict value stays inconclusive.
            logger.warning(f"Final check errored ({e}); releasing fail-open.")
            for ci in snapshot.all_check_items:
                append_ledger_record(
                    base_dir,
                    {
                        "attempt_id": f"final#{attempt_no}",
                        "checkpoint_id": "final",
                        "item_text": ci.text,
                        "kind": ci.kind,
                        "when": ci.when,
                        "status": "inconclusive",
                        "evidence": f"final check error: {e}",
                        "anchor_step_id": None,
                    },
                )
            report = None

        if report is not None:
            for v in report.verdicts:
                when = next(
                    (
                        ci.when
                        for ci in snapshot.all_check_items
                        if ci.kind == v.kind and ci.text == v.item_text
                    ),
                    "at_end",
                )
                append_ledger_record(
                    base_dir,
                    {
                        "attempt_id": f"final#{attempt_no}",
                        "checkpoint_id": "final",
                        "item_text": v.item_text,
                        "kind": v.kind,
                        "when": when,
                        "status": v.status,
                        "evidence": v.evidence,
                        "suggestion": v.suggestion,
                        "anchor_step_id": None,
                    },
                )

            passed = verdicts_allow_release(report) and not report.unmet_subgoals
            if not passed:
                if user_stopped or halted or attempt_no >= max_attempts:
                    verify_blocked = True
                    for v in report.verdicts:
                        if v.kind == "verify" and v.status == "failed":
                            blocked_findings.append(
                                f"[verify failed] '{v.item_text}': {v.evidence}"
                            )
                    for text in report.unmet_subgoals:
                        blocked_findings.append(f"[unmet subgoal] '{text}'")
                    logger.warning(
                        "Final check found unmet verify criteria but the retry"
                        " budget is exhausted (or a stop/halt is latched);"
                        " ending with a blocked outcome."
                    )
                else:
                    findings: list[str] = []
                    for text in report.unmet_subgoals:
                        if revert_subgoal_status(ctx, subgoal_hash(text)):
                            findings.append(
                                f"[final check] Subgoal '{text}' was set back to"
                                " in-progress: its acceptance criteria are not met."
                            )
                    for v in report.verdicts:
                        if v.kind == "verify" and v.status == "failed":
                            suggestion = f" Suggestion: {v.suggestion}" if v.suggestion else ""
                            findings.append(
                                f"[verify failed] '{v.item_text}': {v.evidence}{suggestion}"
                            )
                    update["operator_feedback"] = findings or None
                    update["exit_settlement_route"] = "continue"
                    return update

    # END path: assemble the machine-readable run outcome.
    records = read_ledger(base_dir)
    outcome = compute_run_outcome(snapshot, records, verify_blocked=verify_blocked)
    if snapshot.all_check_items or records or verify_blocked:
        update["run_outcome"] = outcome.model_dump()
        # BLOCKED/partial wrap-ups carry the last findings in the metadata file.
        extra = {"last_findings": blocked_findings} if blocked_findings else None
        write_run_outcome(base_dir, outcome, extra)
        logger.info(
            f"Run outcome: task_status={outcome.task_status},"
            f" tests(passed={outcome.tests.passed}, failed={outcome.tests.failed},"
            f" inconclusive={outcome.tests.inconclusive},"
            f" unchecked={outcome.tests.unchecked})"
        )
    return update


def exit_settlement_gate(state: State) -> Literal["continue", "end"]:
    route = getattr(state, "exit_settlement_route", None)
    return "continue" if route == "continue" else "end"


def execution_check_edge(
    state: State,
) -> Literal["execute_decisions", "review_subgoals"]:
    if state.checker_success:
        if state.structured_decisions:
            return "execute_decisions"
        return "review_subgoals"
    return "review_subgoals"


def _get_active_subgoal_hashes(ctx: ArtemisContext) -> tuple[str, str | None]:
    """Parses task_plan.md to find the active subgoal and sub-subgoal hashes."""

    if not ctx.data_engine:
        return "default", None

    task_plan_path = get_note_file_path(ctx.data_engine.base_dir, "task_plan")

    if not task_plan_path.exists():
        return "default", None

    try:
        content = task_plan_path.read_text(encoding="utf-8")

        return get_active_subgoal_hashes(content)
    except Exception as e:
        logger.error(f"Failed to parse active subgoal: {e}")

    return "default", None


class _CyFunctionDetectorMeta(type):
    def __instancecheck__(self, instance):
        name = type(instance).__name__
        return (
            name
            in (
                "cyfunction",
                "cython_function_or_method",
                "builtin_function_or_method",
            )
            or "cyfunction" in name.lower()
        )


class CyFunctionDetector(metaclass=_CyFunctionDetectorMeta):
    pass


def check_plan_mutation_rejections(
    content_before: str, content_after: str, state: State | None = None
) -> str | None:
    """Checks if a task plan modification violates the machine-channel safety rules."""
    before = parse_plan(content_before)
    after = parse_plan(content_after)

    # 1. Incomplete nested subgoals check
    if after.all_top_level_done and any(
        (item.is_pending or item.is_active) and not item.is_top_level for item in after.items
    ):
        return (
            "Your changes to the task plan were not applied. Your changes would cause the"
            " task to end completely, but there seem to be goals in the task plan"
            " that have not been marked as completed. If you believe these goals"
            " are already completed, please update their statuses."
        )

    # 2. Continuous monitoring protection: a [Loop:continuous] milestone can only
    #    transition to [x] (or disappear) after the user injects an explicit stop
    #    signal (user_stop_requested); intent is never inferred from wording.
    user_stopped = bool(state is not None and getattr(state, "user_stop_requested", False))

    if before.continuous_top_level and not user_stopped:
        if not after.continuous_top_level:
            return (
                "Your changes to the task plan were rejected. You cannot delete an active"
                " [Loop:continuous] continuous monitoring milestone or remove its tag."
                " Please keep the milestone active (in progress [/]) and record each"
                " check cycle as an indented subtask."
            )
        if any(item.is_done for item in after.continuous_top_level):
            return (
                "Your changes to the task plan were rejected. [Loop:continuous] continuous"
                " monitoring milestones must remain in progress ([/]) and cannot be"
                " unilaterally marked as completed [x] until the user injects an explicit"
                " stop signal. Please keep it active ([/]) and record each polling check"
                " as an indented subtask."
            )

    return None


def check_unintended_rewrite(content_before: str, content_after: str) -> str | None:
    """Detects the hand-slip signature of a full-file rewrite: top-level milestones
    whose status did not change but whose text drifted. Declared edits via
    update_note (explicit target/replacement) are exempt.
    """
    edits = unintended_milestone_edits(parse_plan(content_before), parse_plan(content_after))
    if not edits:
        return None

    listing = "\n".join(f'- "{b}"\n  -> "{a}"' for b, a in edits)
    return (
        "Your changes to the task plan were not applied. Your rewrite reworded"
        " top-level milestones whose status you did not change — this usually means"
        " the plan was regenerated from memory and historical milestones drifted"
        f" unintentionally:\n{listing}\nIf a rewording was intentional, apply it"
        " explicitly with the update_note tool (target/replacement). Otherwise,"
        " re-apply only the changes you actually intended."
    )


class NoteArgs(BaseModel):
    model_config = {"ignored_types": (CyFunctionDetector,)}
    key: str = Field(..., description=SAVE_NOTE_ARG_KEY_DESC)
    content: str = Field(..., description=SAVE_NOTE_ARG_CONTENT_DESC)


async def _process_plan_write(
    ctx: ArtemisContext,
    state: State | None,
    task_plan_path,
    content_before: str,
    content_after: str,
    result,
    declared_intent: bool,
):
    """Shared post-write pipeline for every task_plan mutation.

    Constitution order: hard rejections with rollback (machine-channel rules
    only), then ratchet-baseline planner validation, then checker trigger on
    new top-level completions. ``declared_intent`` marks surgical update_note
    edits, which are exempt from the full-rewrite hand-slip check.
    """
    rejection_message = check_plan_mutation_rejections(content_before, content_after, state)
    if rejection_message is None and not declared_intent:
        rejection_message = check_unintended_rewrite(content_before, content_after)
    if rejection_message:
        logger.info(f"Rejected task plan update: {rejection_message}")
        if task_plan_path.exists():
            task_plan_path.write_text(content_before, encoding="utf-8")

        # Guidance, not a tool failure: status stays "success" so the Operator
        # loop treats it as feedback content (matching the legacy envelope
        # behavior where the SystemMessage carried no error status).
        return ToolMessage(content=rejection_message, tool_call_id="", status="success")

    # Deterministic check-line guard (Operator-source writes only; the Planner
    # node writes through unwrapped tools and retains revision authority):
    # deleted/rewritten check lines are merged back in a pure text operation.
    # This is content-driven and independent of any switch — a resumed plan
    # with check lines stays protected even when checking is disabled.
    merged = restore_missing_check_items(content_before, content_after)
    if merged is not None:
        logger.info(
            "Plan write removed or rewrote declared check lines; merging them"
            " back (check standards are protected deterministically)."
        )
        content_after = merged
        try:
            task_plan_path.write_text(content_after, encoding="utf-8")
        except Exception as e:
            logger.error(f"Failed to write merged check lines: {e}")

    before = parse_plan(content_before)
    after = parse_plan(content_after)

    # Ratchet baseline: judge the current plan against the last *validated*
    # plan rather than the previous write, so many small edits cannot drift
    # the milestones below any per-edit trigger.
    if ctx.last_validated_plan is None:
        ctx.last_validated_plan = content_before

    disable_planner_validation = (
        ctx.execution_setup and ctx.execution_setup.disable_planner_validation
    )
    if not disable_planner_validation:
        baseline = parse_plan(ctx.last_validated_plan)
        if milestones_changed(baseline, after):
            logger.info(
                "Top-level task plan diverged from the validated baseline."
                " Triggering async Planner Validation."
            )

            if ctx.planner_task and not ctx.planner_task.done():
                # A newer write supersedes the in-flight validation.
                ctx.planner_task.cancel()

            if not milestones_changed(baseline, before):
                # Last content still consistent with the baseline: preserves
                # status progress made since the baseline as rollback target.
                ctx.task_plan_content_before = content_before
            elif ctx.task_plan_content_before is None:
                ctx.task_plan_content_before = ctx.last_validated_plan

            ctx.pending_validated_plan = content_after
            initial_goal = (
                state.initial_goal if state and hasattr(state, "initial_goal") else "Unknown Goal"
            )

            ctx.planner_task = asyncio.create_task(
                run_async_planner_validation(
                    ctx,
                    initial_goal,
                    ctx.last_validated_plan,
                    content_after,
                    getattr(state, "operator_raw_thinking", None),
                    getattr(state, "operator_native_thinking", None),
                )
            )

            if isinstance(result, ToolMessage) and isinstance(result.content, str):
                result.content = (
                    f"{result.content}\n\nWe have applied your changes"
                    " to the task plan. A background verification task"
                    " is currently reviewing these changes. If there"
                    " are any issues with your modifications, you will"
                    " be notified shortly."
                )

    # Queue (never spawn) a checkpoint for EVERY newly completed top-level
    # subgoal carrying on_complete check items. Spawning happens in
    # execution_check_node after this turn's step is recorded, so the evidence
    # anchor points at the correct step.
    new_completions = new_top_level_completions(before, after)
    if new_completions:
        queue_checkpoints(ctx, state, after, new_completions, content_after)

    return result


def wrap_note_tool(ctx: ArtemisContext, original_tool):

    async def wrapped_note_tool(
        key: str,
        content: str,
        tool_call_id: Annotated[str, InjectedToolCallId] = None,
        state: Annotated[State, InjectedState] = None,
    ):
        """Wrapped tool to intercept task plan updates."""
        args = {"key": key, "content": content}
        if key != "task_plan" or not ctx.data_engine:
            return await invoke_tool_with_injection(
                original_tool, args, tool_call_id, state, record_trace=False
            )

        task_plan_path = get_note_file_path(ctx.data_engine.base_dir, "task_plan")

        content_before = ""
        if task_plan_path.exists():
            content_before = task_plan_path.read_text(encoding="utf-8")

        result = await invoke_tool_with_injection(
            original_tool, args, tool_call_id, state, record_trace=False
        )

        content_after = content
        if task_plan_path.exists():
            content_after = task_plan_path.read_text(encoding="utf-8")

        return await _process_plan_write(
            ctx,
            state,
            task_plan_path,
            content_before,
            content_after,
            result,
            declared_intent=False,
        )

    return StructuredTool.from_function(
        func=lambda *a, **kw: None,
        coroutine=wrapped_note_tool,
        name=original_tool.name,
        description=original_tool.description,
        args_schema=NoteArgs,
    )


class UpdateNoteArgs(BaseModel):
    model_config = {"ignored_types": (CyFunctionDetector,)}
    key: str = Field(..., description=UPDATE_NOTE_ARG_KEY_DESC)
    target: str = Field(..., description=UPDATE_NOTE_ARG_TARGET_DESC)
    replacement: str = Field(..., description=UPDATE_NOTE_ARG_REPLACEMENT_DESC)


def wrap_update_note_tool(ctx: ArtemisContext, original_tool):

    async def wrapped_update_note(
        key: str,
        target: str,
        replacement: str,
        tool_call_id: Annotated[str, InjectedToolCallId] = None,
        state: Annotated[State, InjectedState] = None,
    ):
        """Wrapped tool to intercept task plan updates via update_note."""
        args = {"key": key, "target": target, "replacement": replacement}
        if key != "task_plan" or not ctx.data_engine:
            return await invoke_tool_with_injection(
                original_tool, args, tool_call_id, state, record_trace=False
            )

        task_plan_path = get_note_file_path(ctx.data_engine.base_dir, "task_plan")

        content_before = ""
        if task_plan_path.exists():
            content_before = task_plan_path.read_text(encoding="utf-8")

        result = await invoke_tool_with_injection(
            original_tool, args, tool_call_id, state, record_trace=False
        )

        content_after = ""
        if task_plan_path.exists():
            content_after = task_plan_path.read_text(encoding="utf-8")

        return await _process_plan_write(
            ctx,
            state,
            task_plan_path,
            content_before,
            content_after,
            result,
            declared_intent=True,
        )

    return StructuredTool.from_function(
        func=lambda *a, **kw: None,
        coroutine=wrapped_update_note,
        name=original_tool.name,
        description=original_tool.description,
        args_schema=UpdateNoteArgs,
    )


def convergence_gate(
    state: State, ctx: ArtemisContext
) -> Literal["continue", "exit_settlement", "end"]:
    logger.info("Starting convergence_gate")

    if not ctx.data_engine:
        return "end"

    def _read_plan() -> str:
        file_path = get_note_file_path(ctx.data_engine.base_dir, "task_plan")
        if not file_path.exists():
            return ""
        try:
            return file_path.read_text(encoding="utf-8")
        except Exception:
            return ""

    def _terminal_route() -> Literal["exit_settlement", "end"]:
        """Settlement and final review are decoupled from each other but share
        the entry: any check item, in-flight/queued attempt, ledger record, or
        an enabled final review forces the settlement node."""
        plan_snapshot = parse_plan(_read_plan())
        needs_settlement = (
            bool(plan_snapshot.all_check_items)
            or bool(getattr(ctx, "checkpoint_tasks", None))
            or bool(getattr(ctx, "pending_checkpoints", None))
            or has_ledger_records(ctx.data_engine.base_dir)
            or final_check_enabled(ctx)
        )
        return "exit_settlement" if needs_settlement else "end"

    # A halt latched by an assert failure (policy 'halt') terminates the run
    # regardless of plan progress.
    if getattr(ctx, "assert_halt", False):
        logger.warning("Assert-halt latched; routing to exit settlement.")
        return _terminal_route()

    # Check planner-validation result
    if hasattr(state, "checker_success") and not state.checker_success:
        logger.info("Plan validation failed, returning to operator for retry.")
        return "continue"

    file_path = get_note_file_path(ctx.data_engine.base_dir, "task_plan")
    if not file_path.exists():
        logger.warning(f"Task plan file not found at {file_path}")
        return "continue"
    try:
        task_plan_content = file_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.error(f"Failed to read task plan: {e}")
        return "continue"

    snapshot = parse_plan(task_plan_content)

    if not snapshot.has_top_level:
        # An empty plan does not bypass an enabled final review: the review
        # object is the user's original goal, not the plan.
        logger.info("No subgoals found in file, ending")
        return _terminal_route()

    if snapshot.all_top_level_done:
        user_stopped = bool(getattr(state, "user_stop_requested", False)) if state else False
        if snapshot.continuous_top_level and not user_stopped:
            logger.warning(
                "All subgoals marked [x] but task plan has a [Loop:continuous]"
                " milestone without a user stop signal. Continuing."
            )
            return "continue"
        logger.info("All subgoals are completed, ending the goal")
        return _terminal_route()

    return "continue"


async def get_graph(ctx: ArtemisContext) -> CompiledStateGraph:
    graph_builder = StateGraph(State)

    # Get native tools
    native_wrappers = [
        save_note_wrapper,
        read_note_wrapper,
        list_notes_wrapper,
        update_note_wrapper,
        append_note_wrapper,
    ]

    native_tools = get_tools_from_wrappers(ctx, native_wrappers)

    ## Define nodes
    graph_builder.add_node("planner", PlannerNode(ctx, tools=native_tools))

    # Wrap native tools for Operator
    operator_native_tools = []
    for t in native_tools:
        if t.name == "save_note":
            operator_native_tools.append(wrap_note_tool(ctx, t))
        elif t.name == "update_note":
            operator_native_tools.append(wrap_update_note_tool(ctx, t))
        elif t.name == "append_note":
            operator_native_tools.append(wrap_note_tool(ctx, t))
        else:
            operator_native_tools.append(t)

    # Get specialized tools for Operator
    operator_specialized_wrappers = [
        ask_diagnoser_wrapper,
        run_adb_command_wrapper,
        manage_task_wrapper,
        ask_explorer_wrapper,
    ]
    if ctx.execution_setup and ctx.execution_setup.enable_committee:
        operator_specialized_wrappers.append(ask_committee_wrapper)

    operator_specialized_tools = get_tools_from_wrappers(ctx, operator_specialized_wrappers)

    # Customize descriptions for run_adb_command and manage_task to match exploratory role
    for t in operator_specialized_tools:
        if t.name == "run_adb_command":
            t.description = (
                "[EXPLORER] Executes a shell command directly on the Android"
                " mobile device via ADB shell. Use this tool when normal"
                " operating tools cannot achieve the goal, do not rely on it."
                " You must not use this tool to bypass goals that could be"
                " achieved by action tools.\nCan run synchronously or"
                " transition to a background task if execution takes longer"
                " than WaitMsBeforeAsync.\nSupports persistent environments"
                " (environment variables) on the phone across invocations."
            )
        elif t.name == "manage_task":
            t.description = (
                "[EXPLORER] Manage background ADB shell tasks launched via"
                " run_adb_command to monitor or terminate ongoing background"
                " commands."
            )

    operator_tools = operator_native_tools + operator_specialized_tools

    if ctx.execution_setup and ctx.execution_setup.video_recording_tools_enabled:
        operator_tools.append(get_video_analyzer_tool(ctx, role="operator"))

    graph_builder.add_node("operator", OperatorNode(ctx, tools=operator_tools))
    graph_builder.add_node("validator", ValidatorNode(ctx))

    graph_builder.add_node("summarizer", SummarizerNode(ctx))
    graph_builder.add_node("execution_check", functools.partial(execution_check_node, ctx=ctx))
    graph_builder.add_node("perception", functools.partial(perception_node, ctx=ctx))
    graph_builder.add_node(node="convergence", action=convergence_node, defer=True)
    graph_builder.add_node("exit_settlement", functools.partial(exit_settlement_node, ctx=ctx))

    ## Linking nodes
    graph_builder.add_edge(START, "planner")
    graph_builder.add_edge("planner", "convergence")
    graph_builder.add_edge("operator", "execution_check")
    graph_builder.add_conditional_edges(
        "execution_check",
        execution_check_edge,
        {
            "review_subgoals": "convergence",
            "execute_decisions": "validator",
        },
    )
    graph_builder.add_edge("validator", "summarizer")
    graph_builder.add_edge("summarizer", "convergence")

    graph_builder.add_conditional_edges(
        source="convergence",
        path=functools.partial(convergence_gate, ctx=ctx),
        path_map={
            "continue": "perception",
            "exit_settlement": "exit_settlement",
            "end": END,
        },
    )
    graph_builder.add_conditional_edges(
        source="exit_settlement",
        path=exit_settlement_gate,
        path_map={
            "continue": "perception",
            "end": END,
        },
    )
    graph_builder.add_edge("perception", "operator")

    return graph_builder.compile()
