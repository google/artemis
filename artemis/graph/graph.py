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
import difflib
import functools
import json
import re
import shutil
from typing import Annotated, Literal

from langchain_core.messages import SystemMessage
from langchain_core.tools import StructuredTool
from langchain_core.tools.base import InjectedToolCallId
from langgraph.constants import END, START
from langgraph.graph import StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from pydantic import BaseModel, Field

from artemis.agents.checker.checker import run_async_check
from artemis.agents.operator.operator import OperatorNode
from artemis.agents.planner.planner import (
    PlannerNode,
    run_async_planner_validation,
)
from artemis.agents.summarizer.summarizer import SummarizerNode
from artemis.agents.validator.validator import ValidatorNode
from artemis.constants import VALIDATOR_MESSAGES_KEY
from artemis.context import ArtemisContext
from artemis.graph.perception import perception_node
from artemis.graph.state import State
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
from artemis.utils.file import restore_snapshot
from artemis.utils.logger import get_logger
from artemis.utils.notes import (
    SAVE_NOTE_ARG_CONTENT_DESC,
    SAVE_NOTE_ARG_KEY_DESC,
    UPDATE_NOTE_ARG_KEY_DESC,
    UPDATE_NOTE_ARG_REPLACEMENT_DESC,
    UPDATE_NOTE_ARG_TARGET_DESC,
    get_note_file_path,
    get_notes_dir,
)
from artemis.utils.task_tree import get_active_subgoal_hashes

logger = get_logger(__name__)


def convergence_node(state: State):
    """Convergence point for parallel execution paths."""
    return {}


async def execution_check_node(state: State, ctx: ArtemisContext):
    logger.info("Starting execution_check_node")

    if not ctx.data_engine:
        return {"checker_success": True}

    checker_success = True
    disable_checker = ctx.execution_setup and ctx.execution_setup.disable_checker

    if disable_checker:
        logger.info("Checker is disabled via flag. Skipping checker invocation.")
    else:
        if (
            (not hasattr(ctx, "checker_task") or not ctx.checker_task)
            and ctx.data_engine
            and getattr(state, "operator_replied", False)
        ):
            subgoal_hash, _ = _get_active_subgoal_hashes(ctx)
            notes_dir = get_notes_dir(ctx.data_engine.base_dir)
            chat_path = notes_dir / f"verification_chat_{subgoal_hash}.json"

            if chat_path.exists():
                subgoal_text = "Unknown subgoal"
                task_plan_path = get_note_file_path(ctx.data_engine.base_dir, "task_plan")
                if task_plan_path.exists():
                    try:
                        content = task_plan_path.read_text(encoding="utf-8")
                        lines = content.split("\n")
                        for line in lines:
                            if line.strip().startswith("- [/]"):
                                subgoal_text = line.strip()[5:].strip()
                                break
                    except Exception as e:
                        logger.error(f"Failed to parse active subgoal text: {e}")

                raw_perception = getattr(state, "operator_raw_data", None)
                ui_hierarchy = getattr(state, "latest_ui_hierarchy", None)

                ctx.checker_task = asyncio.create_task(
                    run_async_check(
                        ctx,
                        subgoal_text,
                        subgoal_hash=subgoal_hash,
                        raw_perception_data=raw_perception,
                        latest_ui_hierarchy=ui_hierarchy,
                    )
                )

    if hasattr(ctx, "checker_task") and ctx.checker_task:
        logger.info("Waiting for checker task to complete...")
        try:
            result = await ctx.checker_task
            ctx.checker_task = None  # Clear it

            if result:
                status = result.get("status")
                if status == "success":
                    logger.info("Checker succeeded.")
                    checker_success = True
                elif status == "failed":
                    logger.warning("Checker failed.")
                    checker_success = False
            else:
                logger.warning("Checker task returned None.")
                checker_success = False
        except Exception as e:
            logger.error(f"Checker task failed: {e}")
            checker_success = False

    if hasattr(ctx, "planner_task") and ctx.planner_task:
        logger.info("Waiting for planner validation task to complete...")
        try:
            planner_result = await ctx.planner_task
            ctx.planner_task = None

            if planner_result and planner_result.get("status") == "failed":
                logger.warning("Planner rejected the task plan changes.")

                # Targeted rollback: restore task_plan.md only
                if hasattr(ctx, "task_plan_content_before"):
                    task_plan_path = get_note_file_path(ctx.data_engine.base_dir, "task_plan")
                    if task_plan_path.exists():
                        task_plan_path.write_text(ctx.task_plan_content_before, encoding="utf-8")
                        logger.info("Rolled back task_plan.md to its original state.")

                # Intercept action and inject message
                feedback = planner_result.get("feedback", "Your task plan changes were rejected.")

                system_msg = SystemMessage(
                    content=(
                        "Your recent modification to the top-level task plan"
                        " was rejected by the Planner. You should view this"
                        f" feedback critically.\n\nReason: {feedback}\n\nThe"
                        " task plan has been rolled back to its previous"
                        " state. Any terminal actions you outputted were not"
                        " executed. Please review the task goal again and"
                        " consider what the optimal strategy is now."
                    )
                )

                return {
                    "checker_success": False,
                    "operator_replied": False,
                    VALIDATOR_MESSAGES_KEY: [system_msg],
                    "structured_decisions": "",
                }

            else:
                logger.info("Planner approved the task plan changes.")
        except Exception as e:
            logger.error(f"Planner validation task failed: {e}")

    current_step_id = None
    if checker_success:
        raw_data = state.operator_raw_data
        if raw_data:
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

                subgoal_hash, sub_subgoal_hash = _get_active_subgoal_hashes(ctx)
                step_id = ctx.data_engine.record_step(
                    pre_screenshot_bytes=screenshot_bytes,
                    ui_tree=xml_hierarchy,
                    ocr_result=ocr_results,
                    action_taken=action_taken,
                    operator_raw_thinking=getattr(state, "operator_raw_thinking", None),
                    operator_native_thinking=getattr(state, "operator_native_thinking", None),
                    extra_metadata={
                        "subgoal_hash": subgoal_hash,
                        "sub_subgoal_hash": sub_subgoal_hash,
                        "width": raw_data.get("width"),
                        "height": raw_data.get("height"),
                    },
                )
                current_step_id = str(step_id)
                logger.info(f"Recorded step in DataEngine: {current_step_id}")

            except Exception as e:
                logger.error(f"Failed to record step in DataEngine: {e}")

    # Handle optimistic execution rollback/commit
    if ctx.data_engine and getattr(ctx, "task_plan_snapshot", None):
        snapshot_dir = ctx.task_plan_snapshot
        notes_dir = get_notes_dir(ctx.data_engine.base_dir)

        if checker_success:
            logger.info("Checker succeeded. Committing optimistic changes (cleaning up snapshot).")
            if snapshot_dir.exists():
                await asyncio.to_thread(shutil.rmtree, snapshot_dir)
                logger.info(f"Offloaded snapshot deletion at {snapshot_dir} via asyncio.to_thread")
        else:
            logger.warning("Checker failed. Rolling back optimistic changes.")
            if snapshot_dir.exists():

                def _restore_and_preserve():
                    chat_contents = {}
                    for p in notes_dir.glob("verification_chat_*.json"):
                        chat_contents[p.name] = p.read_text(encoding="utf-8")
                    restore_snapshot(snapshot_dir, notes_dir)
                    for name, content in chat_contents.items():
                        (notes_dir / name).write_text(content, encoding="utf-8")
                        logger.info(f"Restored verification chat file: {name}")

                await asyncio.to_thread(_restore_and_preserve)
                logger.info(f"Restored notes from snapshot {snapshot_dir}")
            else:
                raise FileNotFoundError(
                    f"Snapshot directory {snapshot_dir} not found for rollback!"
                )

    update = {
        "checker_success": checker_success,
        "current_step_id": current_step_id,
        "operator_replied": False,
    }
    if checker_success:
        update["subagent_calls"] = []
    return update


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


def validate_milestones(
    content_before: str, content_after: str, similarity_threshold: float = 0.85
) -> bool:
    """Validates top-level milestones to see if async validation is needed.

    Returns True if structural changes or major wording changes (<similarity_threshold ratio)
    are detected.
    """

    milestone_pattern = re.compile(r"^-\s*\[([\sx!/])\]\s*(.*)$", re.MULTILINE)
    before_milestones = [text.strip() for status, text in milestone_pattern.findall(content_before)]
    after_milestones = [text.strip() for status, text in milestone_pattern.findall(content_after)]

    if len(before_milestones) != len(after_milestones):
        return True

    if before_milestones == after_milestones:
        return False

    def normalize(s: str) -> str:
        cleaned = re.sub(r"\W+", " ", s.lower()).strip()
        return re.sub(r"\s+", " ", cleaned)

    for b, a in zip(before_milestones, after_milestones):
        if b == a:
            continue

        norm_b = normalize(b)
        norm_a = normalize(a)

        ratio = difflib.SequenceMatcher(None, norm_b, norm_a).ratio()
        if ratio < similarity_threshold:
            return True

    return False


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
    """Checks if a task plan modification violates system safety or loop integrity rules."""
    lines_after = content_after.splitlines()
    top_level_after = [line for line in lines_after if line.startswith("- [")]

    # 1. Incomplete nested subgoals check
    if top_level_after and all(line.startswith("- [x]") for line in top_level_after):
        has_incomplete = any(re.match(r"^\s*-\s*\[[ /]\]", line) for line in lines_after)
        if has_incomplete:
            return (
                "Your changes to the task plan were not applied. Your changes would cause the"
                " task to end completely, but there seem to be goals in the task plan"
                " that have not been marked as completed. If you believe these goals"
                " are already completed, please update their statuses."
            )

    # 2. Continuous monitoring / Loop milestone protection check
    lines_before = content_before.splitlines()
    top_level_before = [line for line in lines_before if line.startswith("- [")]

    continuous_pattern = re.compile(r"\[Loop\].*?(?:continuous|until\s*manual)", re.IGNORECASE)
    has_continuous_before = any(continuous_pattern.search(line) for line in top_level_before)

    injected = getattr(state, "injected_instruction", None) if state else None
    user_stopped = bool(
        injected
        and any(
            w in str(injected).lower()
            for w in ["stop", "finish", "end", "停止", "结束", "退出", "quit"]
        )
    )

    if has_continuous_before and not user_stopped:
        has_continuous_after = any(continuous_pattern.search(line) for line in top_level_after)
        if not has_continuous_after:
            return (
                "Your changes to the task plan were rejected. You cannot delete an active [Loop] "
                "continuous monitoring milestone or remove its [Loop] tag. Please keep the [Loop] "
                "milestone active (in progress [/]) and record each check cycle as an indented "
                "subtask."
            )
        for line in top_level_after:
            if continuous_pattern.search(line) and line.startswith("- [x]"):
                return (
                    "Your changes to the task plan were rejected. Ongoing continuous monitoring "
                    "milestones (exit condition: 'until manually stopped') must remain in progress "
                    "([/]) and cannot be unilaterally marked as completed [x]. Please keep it "
                    "active ([/]) and record each polling check as an indented subtask."
                )

    return None


class NoteArgs(BaseModel):
    model_config = {"ignored_types": (CyFunctionDetector,)}
    key: str = Field(..., description=SAVE_NOTE_ARG_KEY_DESC)
    content: str = Field(..., description=SAVE_NOTE_ARG_CONTENT_DESC)


def wrap_note_tool(ctx: ArtemisContext, original_tool):

    async def wrapped_note_tool(
        key: str,
        content: str,
        tool_call_id: Annotated[str, InjectedToolCallId] = None,
        state: Annotated[State, InjectedState] = None,
    ):
        """Wrapped tool to intercept task plan updates."""
        if key == "task_plan" and ctx.data_engine:
            task_plan_path = get_note_file_path(ctx.data_engine.base_dir, "task_plan")

            content_before = ""
            if task_plan_path.exists():
                content_before = task_plan_path.read_text(encoding="utf-8")

            result = await invoke_tool_with_injection(
                original_tool,
                {"key": key, "content": content},
                tool_call_id,
                state,
                record_trace=False,
            )

            content_after = content
            if task_plan_path.exists():
                content_after = task_plan_path.read_text(encoding="utf-8")

            rejection_message = check_plan_mutation_rejections(content_before, content_after, state)
            if rejection_message:
                logger.info(f"Rejected task plan update: {rejection_message}")
                if task_plan_path.exists():
                    task_plan_path.write_text(content_before, encoding="utf-8")

                return Command(
                    update={VALIDATOR_MESSAGES_KEY: [SystemMessage(content=rejection_message)]}
                )

            threshold = (
                ctx.execution_setup.planner_validation_threshold
                if ctx.execution_setup
                and hasattr(ctx.execution_setup, "planner_validation_threshold")
                else 0.85
            )
            needs_validation = validate_milestones(
                content_before, content_after, similarity_threshold=threshold
            )
            disable_planner_validation = (
                ctx.execution_setup and ctx.execution_setup.disable_planner_validation
            )

            if needs_validation and not disable_planner_validation:
                logger.info(
                    "Detected significant top-level task plan modification."
                    " Triggering async Planner Validation."
                )

                initial_goal = (
                    state.initial_goal
                    if state and hasattr(state, "initial_goal")
                    else "Unknown Goal"
                )
                ctx.task_plan_content_before = content_before

                ctx.planner_task = asyncio.create_task(
                    run_async_planner_validation(
                        ctx,
                        initial_goal,
                        content_before,
                        content_after,
                        getattr(state, "operator_raw_thinking", None),
                        getattr(state, "operator_native_thinking", None),
                    )
                )

                if isinstance(result, Command) and VALIDATOR_MESSAGES_KEY in result.update:
                    msgs = result.update[VALIDATOR_MESSAGES_KEY]
                    if msgs and hasattr(msgs[0], "content"):
                        msgs[0].content = (
                            f"{msgs[0].content}\n\nWe have applied your changes"
                            " to the task plan. A background verification task"
                            " is currently reviewing these changes. If there"
                            " are any issues with your modifications, you will"
                            " be notified shortly."
                        )

            # Only match top-level subgoals, intentionally ignoring indented sub-subgoals
            disable_checker = ctx.execution_setup and ctx.execution_setup.disable_checker
            pattern = re.compile(r"^-\s*\[x\]\s*(.*)$", re.MULTILINE)

            before_matches = set(pattern.findall(content_before))
            after_matches = set(pattern.findall(content_after))

            new_completions = after_matches - before_matches

            if new_completions and not disable_checker:
                logger.info(f"Detected new top-level completions: {new_completions}.")

                all_matches_in_order = pattern.findall(content_after)
                latest_completion = None
                for match in reversed(all_matches_in_order):
                    if match in new_completions:
                        latest_completion = match
                        break

                if latest_completion:
                    logger.info(f"Triggering Checker for latest completion: {latest_completion}")
                    raw_perception = getattr(state, "operator_raw_data", None)
                    ui_hierarchy = getattr(state, "latest_ui_hierarchy", None)
                    ctx.checker_task = asyncio.create_task(
                        run_async_check(
                            ctx,
                            latest_completion,
                            raw_perception_data=raw_perception,
                            latest_ui_hierarchy=ui_hierarchy,
                        )
                    )

            return result
        else:
            return await invoke_tool_with_injection(
                original_tool,
                {"key": key, "content": content},
                tool_call_id,
                state,
                record_trace=False,
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
        if key == "task_plan" and ctx.data_engine:
            task_plan_path = get_note_file_path(ctx.data_engine.base_dir, "task_plan")

            content_before = ""
            if task_plan_path.exists():
                content_before = task_plan_path.read_text(encoding="utf-8")

            result = await invoke_tool_with_injection(
                original_tool,
                {"key": key, "target": target, "replacement": replacement},
                tool_call_id,
                state,
                record_trace=False,
            )

            content_after = ""
            if task_plan_path.exists():
                content_after = task_plan_path.read_text(encoding="utf-8")

            rejection_message = check_plan_mutation_rejections(content_before, content_after, state)
            if rejection_message:
                logger.info(f"Rejected task plan update via update_note: {rejection_message}")
                if task_plan_path.exists():
                    task_plan_path.write_text(content_before, encoding="utf-8")

                return Command(
                    update={VALIDATOR_MESSAGES_KEY: [SystemMessage(content=rejection_message)]}
                )

            threshold = (
                ctx.execution_setup.planner_validation_threshold
                if ctx.execution_setup
                and hasattr(ctx.execution_setup, "planner_validation_threshold")
                else 0.85
            )
            needs_validation = validate_milestones(
                content_before, content_after, similarity_threshold=threshold
            )
            disable_planner_validation = (
                ctx.execution_setup and ctx.execution_setup.disable_planner_validation
            )

            if needs_validation and not disable_planner_validation:
                logger.info(
                    "Detected significant top-level task plan modification."
                    " Triggering async Planner Validation."
                )

                initial_goal = (
                    state.initial_goal
                    if state and hasattr(state, "initial_goal")
                    else "Unknown Goal"
                )
                ctx.task_plan_content_before = content_before

                ctx.planner_task = asyncio.create_task(
                    run_async_planner_validation(
                        ctx,
                        initial_goal,
                        content_before,
                        content_after,
                        getattr(state, "operator_raw_thinking", None),
                        getattr(state, "operator_native_thinking", None),
                    )
                )

                if isinstance(result, Command) and VALIDATOR_MESSAGES_KEY in result.update:
                    msgs = result.update[VALIDATOR_MESSAGES_KEY]
                    if msgs and hasattr(msgs[0], "content"):
                        msgs[0].content = (
                            f"{msgs[0].content}\n\nWe have applied your changes"
                            " to the task plan. A background verification task"
                            " is currently reviewing these changes. If there"
                            " are any issues with your modifications, you will"
                            " be notified shortly."
                        )

            disable_checker = ctx.execution_setup and ctx.execution_setup.disable_checker
            pattern = re.compile(r"^-\s*\[x\]\s*(.*)$", re.MULTILINE)
            before_matches = set(pattern.findall(content_before))
            after_matches = set(pattern.findall(content_after))
            new_completions = after_matches - before_matches

            if new_completions and not disable_checker:
                logger.info(f"Detected new top-level completions: {new_completions}.")
                all_matches_in_order = pattern.findall(content_after)
                latest_completion = None
                for match in reversed(all_matches_in_order):
                    if match in new_completions:
                        latest_completion = match
                        break

                if latest_completion:
                    logger.info(f"Triggering Checker for latest completion: {latest_completion}")
                    raw_perception = getattr(state, "operator_raw_data", None)
                    ui_hierarchy = getattr(state, "latest_ui_hierarchy", None)
                    ctx.checker_task = asyncio.create_task(
                        run_async_check(
                            ctx,
                            latest_completion,
                            raw_perception_data=raw_perception,
                            latest_ui_hierarchy=ui_hierarchy,
                        )
                    )

            return result
        else:
            return await invoke_tool_with_injection(
                original_tool,
                {"key": key, "target": target, "replacement": replacement},
                tool_call_id,
                state,
                record_trace=False,
            )

    return StructuredTool.from_function(
        func=lambda *a, **kw: None,
        coroutine=wrapped_update_note,
        name=original_tool.name,
        description=original_tool.description,
        args_schema=UpdateNoteArgs,
    )


async def get_graph(ctx: ArtemisContext) -> CompiledStateGraph:
    graph_builder = StateGraph(State)

    def convergence_gate(state: State) -> Literal["continue", "end"]:
        logger.info("Starting convergence_gate")

        # Check checker result
        if hasattr(state, "checker_success") and not state.checker_success:
            logger.info("Checker failed, returning to operator for retry.")
            return "continue"

        task_plan_content = ""
        if ctx.data_engine:
            file_path = get_note_file_path(ctx.data_engine.base_dir, "task_plan")
            if file_path.exists():
                try:
                    task_plan_content = file_path.read_text(encoding="utf-8")
                except Exception as e:
                    logger.error(f"Failed to read task plan: {e}")
                    return "continue"
            else:
                logger.warning(f"Task plan file not found at {file_path}")
                return "continue"

        lines = task_plan_content.splitlines()

        # Check for continuous monitoring loop that should not terminate prematurely
        continuous_pattern = re.compile(r"\[Loop\].*?(?:continuous|until\s*manual)", re.IGNORECASE)
        has_continuous_monitoring = any(continuous_pattern.search(line) for line in lines)
        injected = getattr(state, "injected_instruction", None) if state else None
        user_stopped = bool(
            injected
            and any(
                w in str(injected).lower()
                for w in ["stop", "finish", "end", "停止", "结束", "退出", "quit"]
            )
        )

        # Check for all completed
        top_level_lines = [line for line in lines if line.startswith("- [")]

        if not top_level_lines:
            logger.info("No subgoals found in file, ending")
            return "end"

        all_done = all(line.startswith("- [x]") for line in top_level_lines)
        if all_done:
            if has_continuous_monitoring and not user_stopped:
                logger.warning(
                    "All subgoals marked [x] but task plan has active continuous monitoring"
                    " without user stop signal. Continuing."
                )
                return "continue"
            logger.info("All subgoals are completed, ending the goal")
            return "end"

        return "continue"

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
        path=convergence_gate,
        path_map={
            "continue": "perception",
            "end": END,
        },
    )
    graph_builder.add_edge("perception", "operator")

    return graph_builder.compile()
