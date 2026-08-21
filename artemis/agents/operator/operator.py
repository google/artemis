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
import json
from pathlib import Path
import re
from typing import Annotated, Any, Literal
from uuid import uuid4

from langchain_core.messages import SystemMessage, ToolMessage
from langchain_core.tools import BaseTool, tool
from langgraph.types import Command

from artemis.constants import (
    OPERATOR_MAX_CHAT_ROUNDS,
    VALIDATOR_MESSAGES_KEY,
)
from artemis.context import ArtemisContext
from artemis.data_engine.trace import (
    TraceSpan,
    trace,
    trace_langchain_tool,
)
from artemis.graph.state import State
from artemis.services.llm import get_llm, invoke_llm_with_timeout_message
from artemis.tools.command_tool import (
    _BACKGROUND_TASKS,
    _FINISHED_TASKS_LOGS,
    _is_output_long,
    analyze_task_output_wrapper,
)
from artemis.tools.index import get_tool_by_name
from artemis.tools.tool_wrapper import (
    get_tool_result_content,
    invoke_tool_with_injection,
)
from artemis.utils.coordinates import (
    compute_smart_swipe_coordinates,
    parse_swipe_parameters,
)
from artemis.utils.decorators import wrap_with_callbacks
from artemis.utils.file import create_snapshot
from artemis.utils.logger import get_logger
from artemis.utils.notes import get_note_file_path, get_notes_dir
from artemis.utils.task_tree import (
    build_plan_and_history,
    get_active_subgoal_hashes,
)
from artemis.utils.verification import (
    append_verification_chat,
    get_verification_chat_path,
    get_verification_chat_rounds,
    read_verification_chat,
)
from artemis.utils.visualization import format_minimal_list_with_elements

logger = get_logger(__name__)

DEFERRING_TOOLS = {
    "ask_diagnoser",
    "video_analyzer",
    "read_note",
    "list_notes",
    "save_note",
    "run_adb_command",
    "manage_task",
    "analyze_task_output",
    "ask_explorer",
}

from artemis.agents.operator.prompts import (
    PromptBuilder,
    PromptComponent,
    TemplatePromptComponent,
    ObservationPromptComponent,
    ScreenshotSimilarityPromptComponent,
    CheckerFeedbackPromptComponent,
    BackgroundTasksPromptComponent,
    ShortTermMemoryPromptComponent,
    TaskPlanWarningPromptComponent,
    ToolLimitWarningPromptComponent,
    InjectedInstructionPromptComponent,
)
from artemis.agents.operator.prompt_builder import load_operator_prompts


class OperatorNode:
    def __init__(
        self,
        ctx: ArtemisContext,
        tools: list[BaseTool] | None = None,
        prompt_components: list[PromptComponent] | None = None,
        last_n_detailed: int = 1,
    ):
        self.ctx = ctx
        self.tools = tools or []
        self.prompt_components = prompt_components or []
        self.last_n_detailed = last_n_detailed
        try:
            self.prompts = load_operator_prompts()
        except (OSError, ValueError, json.JSONDecodeError) as e:
            logger.error(f"Failed to load operator prompts: {e}")
            self.prompts = {}

    def _check_infinite_loop(self, state: State):
        subagent_calls = state.subagent_calls or []
        if len(subagent_calls) >= 3:
            last_three = subagent_calls[-3:]
            if len(set(last_three)) == 1:
                logger.error(
                    f"Infinite loop detected: Sub-agent {last_three[0]} called"
                    " 3 times consecutively."
                )
                raise RuntimeError(
                    f"Infinite loop detected: Sub-agent {last_three[0]} called"
                    " 3 times consecutively."
                )

    def _get_history_and_plan(self) -> tuple[list[dict], str]:
        steps = []
        if self.ctx.data_engine:
            steps = self.ctx.data_engine.get_agent_friendly_steps()

        task_plan = "No task plan yet."
        if self.ctx.data_engine:
            task_plan_path = get_note_file_path(self.ctx.data_engine.base_dir, "task_plan")

            if task_plan_path.exists():
                try:
                    task_plan = task_plan_path.read_text(encoding="utf-8")
                except Exception as e:
                    task_plan = f"Error reading task plan: {e}"

        return steps, task_plan

    def _get_verification_chat_rounds(self) -> tuple[int, int]:
        if not self.ctx.data_engine:
            return 0, 0

        subgoal_hash = self._get_active_subgoal_hash()
        chat_path = get_verification_chat_path(self.ctx.data_engine.base_dir, subgoal_hash)
        turns = read_verification_chat(chat_path)
        return get_verification_chat_rounds(turns)

    def _has_checker_feedback(self) -> bool:
        if not self.ctx.data_engine:
            return False

        subgoal_hash = self._get_active_subgoal_hash()
        chat_path = get_verification_chat_path(self.ctx.data_engine.base_dir, subgoal_hash)
        return chat_path.exists()

    def _get_reply_to_checker_tool(self) -> BaseTool:
        @tool
        def reply_to_checker(
            reasoning: Annotated[
                str,
                "Your reasoning or observations regarding the verification feedback.",
            ],
        ):
            """[TERMINAL] Use this tool to reply to the verification feedback with your reasoning or observations.

            This ends your turn.
            """

            if self.ctx.data_engine:
                subgoal_hash = self._get_active_subgoal_hash()
                chat_path = get_verification_chat_path(self.ctx.data_engine.base_dir, subgoal_hash)

                turns = read_verification_chat(chat_path)
                max_op, max_chk = get_verification_chat_rounds(turns)

                round_num = max(max_op + 1, max_chk)
                if round_num == 0:
                    round_num = 1

                if append_verification_chat(chat_path, "operator", reasoning, round_num):
                    return "Successfully recorded your reply to the Checker."
                else:
                    return "Failed to save reply."
            else:
                return "Error: DataEngine not available."

        return reply_to_checker

    async def _build_prompt(
        self,
        state: State,
        latest_screenshot_b64: str,
        fused_xml: list[dict],
        minimal_list: str,
        current_step_num: int,
        steps: list[dict],
        task_plan: str,
        active_background_tasks: list[dict] = None,
        newly_finished_tasks: list[dict] = None,
    ) -> list:
        plan_and_history = self._build_plan_and_history(steps, task_plan)

        builder = PromptBuilder()

        has_feedback = self._has_checker_feedback()

        components = self.prompt_components
        if not components:
            if has_feedback:
                components = [
                    (
                        TemplatePromptComponent(),
                        {"template_name": "troubleshooter_template"},
                    ),
                    (ObservationPromptComponent(), {}),
                    (ScreenshotSimilarityPromptComponent(), {}),
                    (InjectedInstructionPromptComponent(), {}),
                    (ShortTermMemoryPromptComponent(), {}),
                    (BackgroundTasksPromptComponent(), {}),
                    (CheckerFeedbackPromptComponent(), {}),
                    (TaskPlanWarningPromptComponent(), {}),
                    (ToolLimitWarningPromptComponent(), {}),
                ]
            else:
                components = [
                    (
                        TemplatePromptComponent(),
                        {"template_name": "main_template"},
                    ),
                    (ObservationPromptComponent(), {}),
                    (ScreenshotSimilarityPromptComponent(), {}),
                    (InjectedInstructionPromptComponent(), {}),
                    (ShortTermMemoryPromptComponent(), {}),
                    (BackgroundTasksPromptComponent(), {}),
                    (TaskPlanWarningPromptComponent(), {}),
                    (ToolLimitWarningPromptComponent(), {}),
                ]
        else:
            components = [(c, {}) for c in components] + [(ToolLimitWarningPromptComponent(), {})]

        # Active and finished tasks are now passed as arguments
        active_tasks = active_background_tasks or []
        newly_finished = newly_finished_tasks or []

        for component, extra_kwargs in components:
            kwargs_to_pass = {
                "prompts": self.prompts,
                "plan_and_history": plan_and_history,
                "latest_screenshot_b64": latest_screenshot_b64,
                "minimal_list": minimal_list,
                "current_step_num": current_step_num,
                "active_background_tasks": active_tasks,
                "newly_finished_tasks": newly_finished,
                "steps": steps,
            }
            kwargs_to_pass.update(extra_kwargs)
            await component(builder, state, self.ctx, **kwargs_to_pass)

        return builder.build()

    async def _invoke_llm_loop(
        self,
        base_llm,
        current_messages: list,
        traced_tools: list,
        new_subagent_calls: list,
        state: State,
    ) -> tuple[list[dict] | None, bool, str | None, str | None, bool]:
        max_iterations = 20
        action_result = None
        requested_argue = False
        raw_thoughts = []
        native_thoughts = []
        tool_limit_exceeded = False

        async def read_stream_and_accumulate(target_llm, msgs):
            try:
                full_response = None
                has_chunks = False
                async for chunk in target_llm.astream(msgs):
                    has_chunks = True
                    if full_response is None:
                        full_response = chunk
                    else:
                        full_response += chunk
                if has_chunks and full_response is not None:
                    return full_response
            except Exception as e:
                logger.debug(f"astream failed or not supported: {e}. Falling back to ainvoke.")

            return await target_llm.ainvoke(msgs)

        for iteration in range(max_iterations):
            if iteration == max_iterations - 1:
                logger.warning(
                    "Operator has reached the maximum number of tool calls. Adding reminder."
                )
                current_messages.append(
                    SystemMessage(
                        content=(
                            "You have reached the maximum number of tool calls"
                            " for this turn. You must make a final action, or"
                            " save your current research results via notes to"
                            " continue later."
                        )
                    )
                )

            bound_llm = base_llm.bind_tools(tools=traced_tools)
            response = await invoke_llm_with_timeout_message(
                read_stream_and_accumulate(bound_llm, current_messages)
            )

            if hasattr(response, "response_metadata") and response.response_metadata:
                usage = (
                    response.response_metadata.get("usage_metadata")
                    or response.response_metadata.get("token_usage")
                    or {}
                )
                prompt_tokens = usage.get("prompt_token_count") or usage.get("prompt_tokens")
                cached_tokens = usage.get("cached_content_token_count") or usage.get(
                    "cached_tokens", 0
                )
                completion_tokens = usage.get("candidates_token_count") or usage.get(
                    "completion_tokens"
                )
                if prompt_tokens is not None:
                    logger.info(
                        f"LLM usage: prompt_tokens={prompt_tokens}"
                        f" (cached={cached_tokens}),"
                        f" completion_tokens={completion_tokens}"
                    )

            if response.content:
                if isinstance(response.content, str):
                    raw_thoughts.append(response.content)
                elif isinstance(response.content, list):
                    for item in response.content:
                        if isinstance(item, dict):
                            if item.get("type") == "text":
                                raw_thoughts.append(item["text"])
                            elif item.get("type") == "thinking":
                                native_thoughts.append(item["thinking"])

            additional_kwargs = getattr(response, "additional_kwargs", None)
            if (
                not response.tool_calls
                and isinstance(additional_kwargs, dict)
                and not hasattr(additional_kwargs, "assert_called_with")  # Filter out mocks
                and additional_kwargs.get("function_call")
            ):
                fc = additional_kwargs["function_call"]
                try:
                    arguments_str = fc.get("arguments", "{}")
                    if isinstance(arguments_str, str):
                        arguments = json.loads(arguments_str)
                    else:
                        arguments = arguments_str

                    tool_call = {
                        "name": fc["name"],
                        "args": arguments,
                        "id": f"call_{uuid4().hex[:8]}",
                        "type": "tool_call",
                    }
                    response.tool_calls = [tool_call]
                    logger.info(
                        "Fallback: Manually parsed function_call from"
                        f" additional_kwargs: {tool_call}"
                    )
                except Exception as e:
                    logger.error(f"Failed to parse function_call fallback: {e}")

            if not response.tool_calls:
                logger.warning("LLM stopped without calling any tool. Encouraging action.")
                break

            current_messages.append(response)

            action_tool_names = [
                "click",
                "input_text",
                "swipe",
                "press_key",
                "manage_app",
                "wait_for_delay",
                "long_press",
            ]

            def normalize_name(name: str) -> str:
                return name.split(":")[-1] if ":" in name else name

            action_calls = [
                tc for tc in response.tool_calls if normalize_name(tc["name"]) in action_tool_names
            ]
            reply_to_checker_calls = [
                tc for tc in response.tool_calls if normalize_name(tc["name"]) == "reply_to_checker"
            ]
            other_calls = [
                tc
                for tc in response.tool_calls
                if normalize_name(tc["name"]) not in action_tool_names + ["reply_to_checker"]
            ]

            tool_outputs = []
            validation_errors = False
            requested_argue = False

            other_tool_failed = False
            other_tool_failure_msg = ""

            has_terminal_calls = bool(action_calls or reply_to_checker_calls)

            if other_calls:
                results = []
                for tc in other_calls:
                    tool_name = tc["name"]
                    logger.info(f"Operator requested tool: {tool_name}")
                    if tool_name in ["ask_diagnoser", "video_analyzer"]:
                        new_subagent_calls.append(tool_name)

                    if ":" in tool_name:
                        tool_to_run = get_tool_by_name(tool_name, traced_tools)
                    else:
                        tool_to_run = next(
                            (t for t in traced_tools if t.name == tool_name),
                            None,
                        )

                    if tool_to_run:
                        args = dict(tc["args"])
                        span = TraceSpan(name=tool_name, trace_type="tool", ctx=self.ctx)
                        span.payload = {"args": args}

                        with span:
                            try:
                                state.operator_raw_thinking = (
                                    "\n".join(raw_thoughts) if raw_thoughts else None
                                )
                                state.operator_native_thinking = (
                                    "\n".join(native_thoughts) if native_thoughts else None
                                )

                                result_obj = await invoke_tool_with_injection(
                                    tool=tool_to_run,
                                    args=args,
                                    tool_call_id=tc["id"],
                                    state=state,
                                )

                                content = get_tool_result_content(result_obj)
                                status = "success"

                                if isinstance(result_obj, Command):
                                    updates = result_obj.update
                                    if VALIDATOR_MESSAGES_KEY in updates:
                                        msgs = updates[VALIDATOR_MESSAGES_KEY]
                                        if msgs and hasattr(msgs[0], "status"):
                                            status = msgs[0].status

                                is_err = status == "error"
                                if not is_err:
                                    if isinstance(content, str):
                                        if content.startswith(
                                            "Failed"
                                        ) or content.lower().startswith("error"):
                                            is_err = True
                                    elif isinstance(content, list):
                                        for block in content:
                                            if (
                                                isinstance(block, dict)
                                                and block.get("type") == "text"
                                            ):
                                                text = block.get("text", "")
                                                if text.startswith(
                                                    "Failed"
                                                ) or text.lower().startswith("error"):
                                                    is_err = True
                                                    break

                                if is_err:
                                    other_tool_failed = True
                                    other_tool_failure_msg = str(content)
                                    status = "error"

                                    span.status = "failed"
                                    span.error = str(content)
                                else:
                                    span.result = content

                                results.append((content, status))
                            except Exception as e:
                                other_tool_failed = True
                                other_tool_failure_msg = str(e)

                                span.status = "failed"
                                span.error = str(e)

                                results.append(
                                    (
                                        f"Error running tool {tool_name}: {e}",
                                        "error",
                                    )
                                )
                    else:
                        other_tool_failed = True
                        other_tool_failure_msg = f"Tool {tool_name} not supported"
                        results.append((f"Error: Tool {tool_name} not supported", "error"))

                for tc, (content, status) in zip(other_calls, results):
                    tool_outputs.append(
                        ToolMessage(
                            tool_call_id=tc["id"],
                            content=content,
                            status=status,
                        )
                    )

                if other_tool_failed:
                    if action_calls:
                        logger.warning(
                            "Scratchpad/helper tool call failed. Deferring"
                            f" screen actions: {other_tool_failure_msg}"
                        )
                        for tc in action_calls:
                            tool_outputs.append(
                                ToolMessage(
                                    tool_call_id=tc["id"],
                                    content=(
                                        "This screen action was rejected"
                                        " because an accompanying tool call"
                                        " failed. Error:"
                                        f" {other_tool_failure_msg}. Currently,"
                                        " no screen actions have been"
                                        " executed. Please correct the error"
                                        " and try again."
                                    ),
                                    status="error",
                                )
                            )
                        action_calls = []

                    if reply_to_checker_calls:
                        logger.warning(
                            "Scratchpad/helper tool call failed. Deferring"
                            f" reply to checker: {other_tool_failure_msg}"
                        )
                        for tc in reply_to_checker_calls:
                            tool_outputs.append(
                                ToolMessage(
                                    tool_call_id=tc["id"],
                                    content=(
                                        "Your reply to the checker was"
                                        " rejected because an accompanying"
                                        " tool call failed. Error:"
                                        f" {other_tool_failure_msg}. Currently,"
                                        " no response has been sent to the"
                                        " checker. Please correct the error"
                                        " and try again."
                                    ),
                                    status="error",
                                )
                            )
                        reply_to_checker_calls = []

                elif has_terminal_calls:
                    has_deferring_calls = any(
                        normalize_name(tc["name"]) in DEFERRING_TOOLS for tc in other_calls
                    )
                    if has_deferring_calls:
                        logger.info(
                            "Operator mixed pre-decision tools"
                            " (memory/exploration) and terminal action calls."
                            " Deferring terminal actions."
                        )

                        for tc in action_calls:
                            tool_outputs.append(
                                ToolMessage(
                                    tool_call_id=tc["id"],
                                    content=(
                                        "Your pre-decision tools have been"
                                        " successfully processed. However, the"
                                        " screen action you attempted to"
                                        " execute was rejected because you are"
                                        " not allowed to simultaneously"
                                        " use result-dependent pre-decision"
                                        " tools and execute physical actions in"
                                        " the same turn."
                                        " Currently, no screen actions have"
                                        " been executed. Please review your"
                                        " updated context and re-output your"
                                        " intended screen action."
                                    ),
                                    status="success",
                                )
                            )

                        for tc in reply_to_checker_calls:
                            tool_outputs.append(
                                ToolMessage(
                                    tool_call_id=tc["id"],
                                    content=(
                                        "Your pre-decision tools have been"
                                        " successfully processed. However, your"
                                        " response to the checker was rejected"
                                        " because you are not allowed to"
                                        " simultaneously use result-dependent"
                                        " pre-decision tools and reply to the"
                                        " checker. Currently, no response has been"
                                        " sent to the checker. Please review"
                                        " your updated context and re-output"
                                        " your reply."
                                    ),
                                    status="success",
                                )
                            )

                        action_calls = []
                        reply_to_checker_calls = []

            if action_calls:
                logger.info(
                    f"Operator requested {len(action_calls)} action(s)."
                    " Translating and processing all."
                )
                actions_list = []
                for tc in action_calls:
                    translated_actions, error = self._translate_and_validate_tool(tc, state)
                    if error:
                        validation_errors = True
                        tool_outputs.append(
                            ToolMessage(
                                tool_call_id=tc["id"],
                                content=error,
                                status="error",
                            )
                        )
                    else:
                        actions_list.extend(translated_actions)
                        tool_outputs.append(
                            ToolMessage(
                                tool_call_id=tc["id"],
                                content="Action Recorded",
                                status="success",
                            )
                        )

                if not validation_errors:
                    action_result = actions_list

            if reply_to_checker_calls:
                logger.info("Operator replied to checker.")
                tc = reply_to_checker_calls[0]
                requested_argue = True

                # Execute it immediately
                result = await self._run_other_tool(
                    tc,
                    traced_tools,
                    new_subagent_calls,
                    state,
                    raw_thoughts,
                    native_thoughts,
                )
                tool_outputs.append(
                    ToolMessage(
                        tool_call_id=tc["id"],
                        content=result,
                        status="success" if not result.startswith("Error") else "error",
                    )
                )

            for tm in tool_outputs:
                current_messages.append(tm)

            if (action_calls and not validation_errors) or requested_argue:
                break

            if validation_errors:
                logger.warning("Some action calls failed validation. Feeding back to LLM.")
                continue
        else:
            tool_limit_exceeded = True

        raw_thinking = "\n".join(raw_thoughts) if raw_thoughts else None
        native_thinking = "\n".join(native_thoughts) if native_thoughts else None
        return (
            action_result,
            requested_argue,
            raw_thinking,
            native_thinking,
            tool_limit_exceeded,
        )

    async def _run_other_tool(
        self,
        tc,
        traced_tools,
        new_subagent_calls,
        state: State,
        raw_thoughts: list,
        native_thoughts: list,
    ) -> str:
        tool_name = tc["name"]
        logger.info(f"Operator requested tool: {tool_name}")
        if tool_name in ["ask_diagnoser", "video_analyzer"]:
            new_subagent_calls.append(tool_name)
        if ":" in tool_name:
            tool_to_run = get_tool_by_name(tool_name, traced_tools)
        else:
            tool_to_run = next((t for t in traced_tools if t.name == tool_name), None)
        if tool_to_run:
            try:
                args = dict(tc["args"])

                state.operator_raw_thinking = "\n".join(raw_thoughts) if raw_thoughts else None
                state.operator_native_thinking = (
                    "\n".join(native_thoughts) if native_thoughts else None
                )

                result_obj = await invoke_tool_with_injection(
                    tool=tool_to_run,
                    args=args,
                    tool_call_id=tc["id"],
                    state=state,
                )
                return get_tool_result_content(result_obj)
            except Exception as e:
                return f"Error running tool {tool_name}: {e}"
        else:
            return f"Error: Tool {tool_name} not supported"

    @wrap_with_callbacks(
        before=lambda: logger.info("Starting Operator Agent..."),
        on_success=lambda _: logger.success("Operator Agent"),
        on_failure=lambda _: logger.error("Operator Agent"),
    )
    @trace(type="agent", name="operator")
    async def __call__(self, state: State):
        # 1. Check for infinite loops
        self._check_infinite_loop(state)

        # Create snapshot for optimistic execution
        if self.ctx.data_engine:
            notes_dir = get_notes_dir(self.ctx.data_engine.base_dir)
            if notes_dir.exists():
                snapshot_dir = notes_dir.with_name("notes_snapshot")
                await asyncio.to_thread(create_snapshot, notes_dir, snapshot_dir)
                self.ctx.task_plan_snapshot = snapshot_dir
                logger.info(f"Created snapshot of notes at {snapshot_dir}")

        # 2. Get perception data from state (populated by Perception node)
        operator_raw_data = getattr(state, "operator_raw_data", {}) or {}
        latest_screenshot_b64 = operator_raw_data.get("screenshot_b64")
        xml_hierarchy = operator_raw_data.get("xml_hierarchy")
        operator_raw_data.get("ocr_results")
        fused_xml = state.latest_ui_hierarchy if hasattr(state, "latest_ui_hierarchy") else []
        state.latest_screenshot if hasattr(state, "latest_screenshot") else None

        if not latest_screenshot_b64 or xml_hierarchy is None:
            logger.error(
                "Operator requires perception data in state (populated by Perception node)"
            )
            raise ValueError("Operator requires perception data in state")

        # 3. Get history and plan
        steps, task_plan = self._get_history_and_plan()

        # 4. Format minimal list

        width = 1080
        height = 2400
        w_raw = operator_raw_data.get("width")
        h_raw = operator_raw_data.get("height")
        if isinstance(w_raw, int) and isinstance(h_raw, int):
            width = w_raw
            height = h_raw
        else:
            try:
                if self.ctx and hasattr(self.ctx, "device") and self.ctx.device:
                    w = getattr(self.ctx.device, "device_width", 1080)
                    h = getattr(self.ctx.device, "device_height", 2400)
                    if isinstance(w, int):
                        width = w
                    if isinstance(h, int):
                        height = h
            except Exception:
                pass
        minimal_list, elements, labels = format_minimal_list_with_elements(fused_xml, width, height)

        # Save transient indexed_points and indexed_elements on the state instance
        state.indexed_points = [el["center"] for el in elements]
        state.indexed_elements = elements

        current_step_num = len(steps) + 1

        # 5. Prepare Tools
        click_tool = self._get_click_tool()
        input_text_tool = self._get_input_text_tool()
        swipe_tool = self._get_swipe_tool()
        press_key_tool = self._get_press_key_tool()
        manage_app_tool = self._get_manage_app_tool()
        wait_for_delay_tool = self._get_wait_for_delay_tool()
        long_press_tool = self._get_long_press_tool()

        all_tools = [
            click_tool,
            input_text_tool,
            swipe_tool,
            press_key_tool,
            manage_app_tool,
            wait_for_delay_tool,
            long_press_tool,
        ] + self.tools

        if self._has_checker_feedback():
            max_op, max_chk = self._get_verification_chat_rounds()
            max_rounds = (
                getattr(
                    getattr(self.ctx, "execution_setup", None),
                    "checker_max_chat_rounds",
                    OPERATOR_MAX_CHAT_ROUNDS,
                )
                if self.ctx and getattr(self.ctx, "execution_setup", None)
                else OPERATOR_MAX_CHAT_ROUNDS
            )
            if max_op < max_rounds:
                reply_to_checker_tool = self._get_reply_to_checker_tool()
                all_tools.append(reply_to_checker_tool)
            else:
                logger.info(
                    f"Maximum chat rounds ({max_rounds}) reached. Removing reply_to_checker tool."
                )

        # 5. Evaluate dynamic tools and prepare background tasks
        has_long_output = False
        for tinfo in _FINISHED_TASKS_LOGS.values():
            if _is_output_long(tinfo.get("output", "")):
                has_long_output = True
                break
        if not has_long_output:
            for t in _BACKGROUND_TASKS.values():
                if _is_output_long("".join(t.stdout_log)):
                    has_long_output = True
                    break

        if has_long_output and not any(t.name == "analyze_task_output" for t in all_tools):
            analyze_tool_fn = analyze_task_output_wrapper.tool_fn_getter(self.ctx)
            all_tools.append(analyze_tool_fn)

        traced_tools = [trace_langchain_tool(t, self.ctx) for t in all_tools]

        active_background_tasks = []
        for tid, t in _BACKGROUND_TASKS.items():
            active_background_tasks.append(
                {
                    "task_id": tid,
                    "command": t.command,
                    "cwd": t.cwd,
                    "terminal_id": t.terminal_id,
                    "output_line_count": len(t.stdout_log),
                }
            )

        newly_finished_tasks = []
        for tid, tinfo in _FINISHED_TASKS_LOGS.items():
            if not tinfo.get("notified", False):
                newly_finished_tasks.append(
                    {
                        "task_id": tid,
                        "command": tinfo.get("command", ""),
                        "status": tinfo.get("status", "completed"),
                        "output_text": tinfo.get("output", ""),
                    }
                )
                tinfo["notified"] = True  # Mark as read

        # 6. Prepare LLM
        llm = get_llm(ctx=self.ctx, name="operator")

        # 7. Build Prompt
        messages = await self._build_prompt(
            state,
            latest_screenshot_b64,
            fused_xml,
            minimal_list,
            current_step_num,
            steps,
            task_plan,
            active_background_tasks=active_background_tasks,
            newly_finished_tasks=newly_finished_tasks,
        )

        # 8. Invoke LLM Loop
        new_subagent_calls = []
        (
            action_result,
            requested_argue,
            raw_thinking,
            native_thinking,
            tool_limit_exceeded,
        ) = await self._invoke_llm_loop(
            base_llm=llm,
            current_messages=messages,
            traced_tools=traced_tools,
            new_subagent_calls=new_subagent_calls,
            state=state,
        )

        # 9. Update State
        structured_decisions = json.dumps(action_result) if action_result else None

        short_term_memory = None
        combined_thinking = ""
        if raw_thinking:
            combined_thinking += raw_thinking + "\n"
        if native_thinking:
            combined_thinking += native_thinking

        if combined_thinking:
            stm_match = re.search(
                r"<short_term_memory>(.*?)</short_term_memory>",
                combined_thinking,
                re.DOTALL,
            )
            if stm_match:
                short_term_memory = stm_match.group(1).strip()

        return await state.asanitize_update(
            ctx=self.ctx,
            update={
                "structured_decisions": structured_decisions,
                "operator_raw_thinking": raw_thinking,
                "operator_native_thinking": native_thinking,
                "short_term_memory": short_term_memory,
                "indexed_points": state.indexed_points,
                "complete_subgoals_by_ids": [],
                "current_step_id": state.current_step_id,
                "subagent_calls": ((state.subagent_calls or []) + new_subagent_calls),
                "operator_replied": requested_argue,
                "operator_tool_limit_exceeded": tool_limit_exceeded,
            },
            agent="operator",
        )

    def _get_active_subgoal_hash(self) -> str:
        """Parses task_plan.md to find the active top-level subgoal hash."""

        if not self.ctx.data_engine:
            return "default"

        task_plan_path = get_note_file_path(self.ctx.data_engine.base_dir, "task_plan")

        if not task_plan_path.exists():
            return "default"

        try:
            content = task_plan_path.read_text(encoding="utf-8")

            parent_hash, _ = get_active_subgoal_hashes(content)
            return parent_hash
        except Exception as e:
            logger.error(f"Failed to parse active subgoal: {e}")

        return "default"

    def _build_plan_and_history(self, steps: list[dict[str, Any]], task_plan: str) -> str:
        """Builds a plan checklist and separate execution history."""

        active_subgoal_hash = self._get_active_subgoal_hash()
        return build_plan_and_history(
            task_plan,
            steps,
            active_subgoal_hash,
            last_n_detailed=self.last_n_detailed,
            strict_milestone_pruning=True,
            recent_window_size=3,
            chronological_last_step=True,
        )

    def _get_long_press_tool(self) -> BaseTool:
        @tool
        def long_press(
            target: Annotated[
                int | list[int],
                "Long press target. Can be an element index number (int, e.g."
                " 3) OR normalized coordinates (list of 2 integers, e.g. [500,"
                " 600]).",
            ],
            duration: Annotated[int, "Long press duration in milliseconds (default 1000)."] = 1000,
        ):
            """[ACTION] Long press on the target location on the screen (supports element index or absolute normalized coordinates)."""
            return "Action Recorded"

        return long_press

    def _get_click_tool(self) -> BaseTool:
        @tool
        def click(
            target: Annotated[
                int | list[int],
                "Click target. Can be an element index number (int, e.g. 3) OR"
                " normalized coordinates (list of 2 integers, e.g. [500,"
                " 600]).",
            ],
            times: Annotated[
                int,
                "Number of consecutive clicks on this target. Use this for"
                " double-clicks or multi-clicks (e.g. 7 to enter developer"
                " mode). Default is 1.",
            ] = 1,
            delay_ms: Annotated[
                int,
                "Delay in milliseconds between consecutive clicks. Default is 100.",
            ] = 100,
        ):
            """[ACTION] Click on the target location on the screen (supports element index or absolute normalized coordinates)."""
            return "Action Recorded"

        return click

    def _get_input_text_tool(self) -> BaseTool:
        @tool
        def input_text(
            text: Annotated[
                str,
                "The text content to input. Supports multi-line content with '\\n'.",
            ],
            target: Annotated[
                int | list[int],
                "Input target field. Can be an input box element index number"
                " (int, e.g. 3) OR normalized coordinates (list of 2 integers,"
                " e.g. [500, 600]).",
            ],
            clear_exist: Annotated[
                bool,
                "Whether to clear existing text before typing. True (default):"
                " clear/replace entire text. False: append at the end of"
                " existing content.",
            ] = True,
        ):
            """[ACTION] Type text into the target input field (supports replacing whole text or appending to the end, and multi-line strings with '\\n')."""
            return "Action Recorded"

        return input_text

    def _get_swipe_tool(self) -> BaseTool:
        @tool
        def swipe(
            direction: Annotated[
                Literal["up", "down", "left", "right"] | None,
                "Direction for scrolling and swiping: 'up' (drags bottom-to-top, scrolling down to reveal content below),"
                " 'down' (drags top-to-bottom, scrolling up to reveal content above),"
                " 'left' (drags right-to-left, scrolling right),"
                " 'right' (drags left-to-right, scrolling left).",
            ] = None,
            start: Annotated[
                list[int] | None,
                "Start normalized coordinates [start_x, start_y] in 0-1000 scale for precise,"
                " local interactions (e.g. adjusting sliders, SeekBars, fine range selection, or drag-and-drop).",
            ] = None,
            end: Annotated[
                list[int] | None,
                "End normalized coordinates [end_x, end_y] in 0-1000 scale for precise,"
                " local interactions (e.g. adjusting sliders, SeekBars, fine range selection, or drag-and-drop).",
            ] = None,
            target: Annotated[
                int | list[int] | str | None,
                "Optional target element index (e.g. 2) or container bounds [left, top, right, bottom] to scope the directional swipe within.",
            ] = None,
            gesture: Annotated[
                Literal["up", "down", "left", "right"] | list[int] | None,
                "Backward-compatible swipe gesture: smart direction string ('up', 'down', 'left', 'right')"
                " OR precise custom coordinates [start_x, start_y, end_x, end_y] in 0-1000 scale.",
            ] = None,
            duration: Annotated[
                int | None,
                "Optional swipe/drag duration in milliseconds (default 800). For drag-and-drop,"
                " list reordering, or sliding/adjusting sliders (e.g., volume, brightness, SeekBars),"
                " set duration >= 1000 (e.g. 1500). If omitted for directional swipe, duration is computed automatically.",
            ] = None,
        ):
            """[ACTION] Perform a swipe, drag, or slider-adjustment gesture on the screen.

            • Directional Scrolling ('direction'): Recommended for general browsing and standard page scrolling in most scenarios. Automatically computes safe swipe vectors and adaptive duration, retains a ~40% visual overlap anchor for zero-omission traversal, and prevents inertial flings. Supports scoping to a sub-container via 'target'. If it fails on certain custom layouts, fall back to specifying exact coordinates ('start' and 'end') directly.
            • Precise Coordinate Gestures ('start', 'end'): Best for local, fine-grained interactions such as adjusting sliders/SeekBars (e.g., volume, brightness, progress bars), drag-and-drop / list reordering, or as a reliable fallback when directional scrolling fails on specific containers. Always drag slightly PAST the target position to overcome touch slop and reliably trigger the update. When setting a slider to Maximum (100%) or Minimum (0%), swipe fully to the extreme boundary.

            Args:
                direction: Smart directional scrolling ('up', 'down', 'left', 'right'). Automatically computes safe swipe vectors, retaining 40% visual overlap: 'up' (reveal content below), 'down' (reveal content above), 'left', 'right'.
                start: Start normalized coordinates [start_x, start_y] in 0-1000 scale.
                end: End normalized coordinates [end_x, end_y] in 0-1000 scale.
                target: Optional target element index (e.g. 2) or container bounds [left, top, right, bottom] to scope the directional swipe within.
                gesture: Backward-compatible parameter: direction string OR custom coordinates list [start_x, start_y, end_x, end_y] in 0-1000 scale.
                duration: Optional gesture duration in milliseconds (default 800).
            """
            return "Action Recorded"

        return swipe

    def _get_press_key_tool(self) -> BaseTool:
        @tool
        def press_key(
            key: Annotated[
                Literal["ENTER", "BACK", "HOME", "APP_SWITCH"],
                "Standard Android system button name (ENTER, BACK, HOME, APP_SWITCH).",
            ],
        ):
            """[ACTION] Press a physical or virtual system button (e.g.

            ENTER, BACK, HOME, APP_SWITCH).
            """
            return "Action Recorded"

        return press_key

    def _get_manage_app_tool(self) -> BaseTool:
        @tool
        def manage_app(
            action: Annotated[Literal["launch", "stop"], "The action type."],
            app_name: Annotated[str, "Display name or package name of the application."],
        ):
            """[ACTION] Launch or force stop a specified application."""
            return "Action Recorded"

        return manage_app

    def _get_wait_for_delay_tool(self) -> BaseTool:
        @tool
        def wait_for_delay(
            time_in_ms: Annotated[
                int,
                "The exact duration to wait in milliseconds. Accurately convert the"
                " required time duration into milliseconds based on your objective"
                " or plan (e.g., 2000 for 2s, 5000 for 5s, 60000 for 1 minute,"
                " 180000 for 3 minutes, 300000 for 5 minutes).",
            ],
        ):
            """[ACTION] Pause execution and wait for a specified duration in milliseconds.

            Use this whenever you need time to elapse—whether for UI loading, animations,
            screen transitions, or longer scheduled delays and intervals specified in the task.
            """
            return "Action Recorded"

        return wait_for_delay

    def _get_wait_for_text_tool(self) -> BaseTool:
        @tool
        def wait_for_text(
            text: Annotated[
                str,
                "A simple, distinct keyword (e.g., 'Success', 'Done',"
                " 'Loading') to watch on the screen. Try to pass a clean word"
                " without trailing punctuation (e.g. 'Loading' instead of"
                " 'Loading...') for optimal matching reliability.",
            ],
            state: Annotated[
                Literal["visible", "hidden"],
                "Wait for the keyword to appear ('visible') or disappear"
                " ('hidden') from the screen.",
            ] = "visible",
            timeout_ms: Annotated[int, "Maximum timeout in milliseconds (default 5000)."] = 5000,
        ):
            """[ACTION] Pause execution and wait intelligently for a specific, distinct text or keyword to appear or disappear on the screen.

            Use this when waiting for a specific loading screen to finish or a
            specific success/confirmation state to appear.
            """
            return "Action Recorded"

        return wait_for_text

    def _translate_and_validate_tool(self, tc: dict, state: State) -> tuple[list[dict], str | None]:
        actions, err = self._translate_and_validate_tool_inner(tc, state)
        if err:
            return [], err
        return actions, None

    def _translate_and_validate_tool_inner(
        self, tc: dict, state: State
    ) -> tuple[list[dict], str | None]:
        tool_name = tc["name"].split(":")[-1] if ":" in tc["name"] else tc["name"]
        args = tc["args"]

        # Get device resolution fallbacks
        width = 1080
        height = 2400
        operator_raw_data = getattr(state, "operator_raw_data", {}) or {}
        w_raw = operator_raw_data.get("width")
        h_raw = operator_raw_data.get("height")
        if isinstance(w_raw, int) and isinstance(h_raw, int):
            width = w_raw
            height = h_raw
        else:
            try:
                if self.ctx and hasattr(self.ctx, "device") and self.ctx.device:
                    w = getattr(self.ctx.device, "device_width", 1080)
                    h = getattr(self.ctx.device, "device_height", 2400)
                    if isinstance(w, int):
                        width = w
                    if isinstance(h, int):
                        height = h
            except Exception:
                pass

        def resolve_target_element(
            target: Any,
        ) -> tuple[dict | None, str | None]:
            if isinstance(target, (int, float)):
                try:
                    target_int = int(target)
                except (ValueError, TypeError):
                    return None, f"Error: Invalid target index {target}."
                indexed_elements = getattr(state, "indexed_elements", None) or []
                if 1 <= target_int <= len(indexed_elements):
                    return indexed_elements[target_int - 1], None
                return (
                    None,
                    (
                        f"Error: Invalid target index {target_int}. Active"
                        f" index range is 1 to {len(indexed_elements)}."
                    ),
                )
            elif isinstance(target, list) and len(target) == 2:
                try:
                    nx, ny = map(float, target)
                    x = int(max(0, min(width - 1, nx * width / 1000)))
                    y = int(max(0, min(height - 1, ny * height / 1000)))
                    return {
                        "center": [x, y],
                        "text": None,
                        "bounds": None,
                        "class": None,
                        "resource_id": None,
                        "is_ocr": False,
                    }, None
                except (ValueError, TypeError):
                    return (
                        None,
                        (f"Error: Invalid coordinates {target}. Must be list of 2 numbers."),
                    )
            return (
                None,
                (
                    f"Error: Invalid target parameter {target}. Must be index"
                    " (int/float) or coordinates [x, y]."
                ),
            )

        if tool_name == "click":
            target = args.get("target")
            times = args.get("times", 1)
            delay_ms = args.get("delay_ms", 100)
            el, err = resolve_target_element(target)
            if err:
                return [], err
            norm_c = [
                int(round(el["center"][0] * 1000.0 / width)),
                int(round(el["center"][1] * 1000.0 / height)),
            ]
            return [
                {
                    "action": "tap",
                    "coordinates": el["center"],
                    "normalized_coordinates": norm_c,
                    "times": times,
                    "delay_ms": delay_ms,
                    "target_text": el.get("text"),
                    "target_bounds": el.get("bounds"),
                    "target_resource_id": el.get("resource_id"),
                    "target_class": el.get("class"),
                }
            ], None

        elif tool_name == "long_press":
            target = args.get("target")
            duration = args.get("duration", 1000)
            el, err = resolve_target_element(target)
            if err:
                return [], err
            norm_c = [
                int(round(el["center"][0] * 1000.0 / width)),
                int(round(el["center"][1] * 1000.0 / height)),
            ]
            return [
                {
                    "action": "long_press_on",
                    "coordinates": el["center"],
                    "normalized_coordinates": norm_c,
                    "duration": duration,
                    "target_text": el.get("text"),
                    "target_bounds": el.get("bounds"),
                    "target_resource_id": el.get("resource_id"),
                    "target_class": el.get("class"),
                }
            ], None

        elif tool_name == "input_text":
            text = args.get("text")
            target = args.get("target")
            clear_exist = args.get("clear_exist", True)

            if text is None:
                return [], "Error: Missing required argument 'text'."
            if not isinstance(text, str):
                return (
                    [],
                    (f"Error: 'text' must be a string, got {type(text).__name__}."),
                )

            el, err = resolve_target_element(target)
            if err:
                return [], err

            norm_c = [
                int(round(el["center"][0] * 1000.0 / width)),
                int(round(el["center"][1] * 1000.0 / height)),
            ]
            actions = []
            actions.append(
                {
                    "action": "focus_and_input_text",
                    "coordinates": el["center"],
                    "normalized_coordinates": norm_c,
                    "text": text,
                    "clear_before_input": clear_exist,
                    "target_text": el.get("text"),
                    "target_bounds": el.get("bounds"),
                    "target_resource_id": el.get("resource_id"),
                    "target_class": el.get("class"),
                }
            )
            return actions, None

        elif tool_name == "swipe":
            kind, target, parsed_duration = parse_swipe_parameters(args, default_duration=None)
            duration = args.get("duration")

            if duration is not None:
                if not isinstance(duration, (int, float)):
                    try:
                        duration = int(duration)
                    except (ValueError, TypeError):
                        return (
                            [],
                            (f"Error: 'duration' must be a number, got {type(duration).__name__}."),
                        )
                else:
                    duration = int(duration)
            else:
                duration = parsed_duration

            if kind == "direction":
                x1, y1, x2, y2, smart_dur = compute_smart_swipe_coordinates(
                    direction=target,
                    target=args.get("target"),
                    indexed_elements=getattr(state, "indexed_elements", None),
                    ui_hierarchy=getattr(state, "ui_tree", None),
                    width=width,
                    height=height,
                    duration=duration,
                )
                if duration is None:
                    duration = smart_dur
            elif kind == "coords" and isinstance(target, list) and len(target) == 4:
                try:
                    nx1, ny1, nx2, ny2 = map(float, target)
                    x1 = int(max(0, min(width - 1, nx1 * width / 1000)))
                    y1 = int(max(0, min(height - 1, ny1 * height / 1000)))
                    x2 = int(max(0, min(width - 1, nx2 * width / 1000)))
                    y2 = int(max(0, min(height - 1, ny2 * height / 1000)))
                except (ValueError, TypeError):
                    return (
                        [],
                        f"Error: Invalid custom swipe coordinates {target}.",
                    )
            else:
                return (
                    [],
                    (
                        f"Error: Invalid swipe gesture parameters {args}. Must be"
                        " direction string or custom coordinates."
                    ),
                )

            # Calculate duration automatically if not provided (safe range 350-900ms to eliminate Android inertial fling)
            if duration is None:
                dist = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
                if dist <= 300:
                    duration = 350
                elif dist <= 600:
                    duration = 550
                elif dist <= 1000:
                    duration = 800
                else:
                    duration = 900

            nx1, ny1 = int(round(x1 * 1000.0 / width)), int(round(y1 * 1000.0 / height))
            nx2, ny2 = int(round(x2 * 1000.0 / width)), int(round(y2 * 1000.0 / height))
            return [
                {
                    "action": "swipe",
                    "coordinates": [x1, y1, x2, y2],
                    "normalized_coordinates": [nx1, ny1, nx2, ny2],
                    "normalized_start_coordinates": [nx1, ny1],
                    "normalized_end_coordinates": [nx2, ny2],
                    "duration": duration,
                }
            ], None

        elif tool_name == "press_key":
            key = args.get("key") or args.get("keycode")
            if not key:
                return [], "Error: Missing required argument 'key'."
            if not isinstance(key, str):
                return (
                    [],
                    f"Error: 'key' must be a string, got {type(key).__name__}.",
                )

            normalized_key = key[8:] if key.startswith("KEYCODE_") else key
            if normalized_key not in ["ENTER", "BACK", "HOME", "APP_SWITCH"]:
                return [], f"Error: Unsupported key '{key}'."
            return [{"action": "press_key", "keycode": f"KEYCODE_{normalized_key}"}], None

        elif tool_name == "manage_app":
            action = args.get("action")
            app_name = args.get("app_name")
            if action not in ["launch", "stop"]:
                return (
                    [],
                    f"Error: Unsupported app management action '{action}'.",
                )
            if not app_name or not isinstance(app_name, str):
                return [], "Error: 'app_name' must be a valid non-empty string."
            action_str = "launch_app" if action == "launch" else "stop_app"
            return [{"action": action_str, "app_name": app_name}], None

        elif tool_name == "wait_for_delay":
            time_in_ms = args.get("time_in_ms")
            if time_in_ms is None:
                return [], "Error: Missing required argument 'time_in_ms'."
            try:
                time_in_ms = int(time_in_ms)
            except (ValueError, TypeError):
                return [], f"Error: Invalid time_in_ms value {time_in_ms}."
            return [{"action": "wait_for_delay", "time_in_ms": time_in_ms}], None

        return [], f"Error: Unsupported tool name '{tool_name}'."
