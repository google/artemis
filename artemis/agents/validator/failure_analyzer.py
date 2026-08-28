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

"""Universal Multi-Model Failure Analyzer for Artemis.

Analyzes execution failures and performs autonomous self-healing across
Google Gemini, OpenAI GPT-4o/o3, Anthropic Claude 3.5/3.7, and OpenRouter endpoints.
"""

import asyncio
from enum import StrEnum
import json
from pathlib import Path
from typing import Any
import uuid

from jinja2 import Template
from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from artemis.agents.validator.tool_declarations import (
    ASK_EXPLORER_TOOL,
    CLICK_SEQUENCE_TOOL,
    MobileActionExecutor,  # Legacy shims below; the loop itself uses McpActionExecutor.
    ToolDeclaration,
    VALIDATOR_TOOLS_DECLARATION,
    capture_screenshot_and_parse_ui,
    prune_intermediate_screenshots,
)
from artemis.context import ArtemisContext
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.data_engine.trace import trace
from artemis.graph.state import State
from artemis.mcp.action_executor import McpActionExecutor
from artemis.services.llm import get_llm
from artemis.utils.logger import get_logger
from artemis.utils.notes import get_note_file_path
from artemis.utils.ocr_xml_fusion import fuse_ocr_with_xml
from artemis.utils.task_tree import (
    build_plan_and_history,
    format_action_clean,
    get_active_subgoal_hashes,
)
from artemis.utils.visualization import format_minimal_list_with_elements


class ValidationErrorCategory(StrEnum):
    NONE = "none"
    TARGET_DISAPPEARED = "target_disappeared"
    TARGET_SHIFTED = "target_shifted"
    TARGET_OCCUPIED = "target_occupied"
    PIXEL_TARGET_DISAPPEARED = "pixel_target_disappeared"
    PIXEL_BYPASSED = "pixel_bypassed"
    XML_BYPASSED = "xml_bypassed"
    GENERAL = "general"


logger = get_logger(__name__)


# ==============================================================================
# Backward-Compatibility Helper Aliases
# ==============================================================================


async def _capture_screenshot_and_parse_ui(
    ctx: ArtemisContext,
    state: State,
    controller: UnifiedMobileController,
    skip_settling: bool = False,
) -> tuple[str | None, bytes | None, str | None]:
    return await capture_screenshot_and_parse_ui(
        ctx, state, controller, skip_settling=skip_settling
    )


async def _exec_click(
    target: list[int],
    state: State,
    controller: UnifiedMobileController,
    ctx: ArtemisContext,
    times: int = 1,
    delay_ms: int = 100,
) -> tuple[str, bytes | None, str | None, str | None]:
    return await MobileActionExecutor(ctx, controller).exec_click(
        target, state, times=times, delay_ms=delay_ms
    )


async def _exec_click_sequence(
    sequence: list[int | list[int]],
    state: State,
    controller: UnifiedMobileController,
    ctx: ArtemisContext,
) -> tuple[str, bytes | None, str | None, str | None]:
    return await MobileActionExecutor(ctx, controller).exec_click_sequence(sequence, state)


async def _exec_long_press(
    target: list[int],
    state: State,
    controller: UnifiedMobileController,
    ctx: ArtemisContext,
    duration_ms: int = 1000,
    duration: int | None = None,
) -> tuple[str, bytes | None, str | None, str | None]:
    return await MobileActionExecutor(ctx, controller).exec_long_press(
        target, state, duration_ms=duration_ms, duration=duration
    )


async def _exec_input_text(
    text: str,
    target: list[int] | None,
    state: State,
    controller: UnifiedMobileController,
    ctx: ArtemisContext,
    clear_exist: bool = True,
) -> tuple[str, bytes | None, str | None, str | None]:
    return await MobileActionExecutor(ctx, controller).exec_input_text(
        text, target, state, clear_exist=clear_exist
    )


async def _exec_swipe(
    action: str | list[int],
    duration: int = 400,
    state: State | None = None,
    controller: UnifiedMobileController | None = None,
    ctx: ArtemisContext | None = None,
    duration_ms: int | None = None,
) -> tuple[str, bytes | None, str | None, str | None]:
    return await MobileActionExecutor(ctx, controller).exec_swipe(
        action, duration=duration, state=state, duration_ms=duration_ms
    )


async def _exec_press_key(
    key: str,
    state: State,
    controller: UnifiedMobileController,
    ctx: ArtemisContext,
) -> tuple[str, bytes | None, str | None, str | None]:
    return await MobileActionExecutor(ctx, controller).exec_press_key(key, state)


async def _exec_manage_app(
    action: str,
    app_name: str,
    state: State,
    controller: UnifiedMobileController,
    ctx: ArtemisContext,
) -> tuple[str, bytes | None, str | None, str | None]:
    return await MobileActionExecutor(ctx, controller).exec_manage_app(action, app_name, state)


async def _exec_wait_for_delay(
    time_in_ms: int,
    state: State,
    controller: UnifiedMobileController,
    ctx: ArtemisContext,
) -> tuple[str, bytes | None, str | None, str | None]:
    return await MobileActionExecutor(ctx, controller).exec_wait_for_delay(time_in_ms, state)


async def _exec_wait_for_text(
    text: str,
    wait_state: str | None,
    timeout_ms: int | None,
    state: State,
    controller: UnifiedMobileController,
    ctx: ArtemisContext,
) -> tuple[str, bytes | None, str | None, str | None]:
    return await MobileActionExecutor(ctx, controller).exec_wait_for_text(
        text, wait_state, timeout_ms, state
    )


async def _exec_read_note(
    key: str,
    ctx: ArtemisContext,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    return await MobileActionExecutor(ctx).exec_read_note(
        key, start_line=start_line, end_line=end_line
    )


async def _exec_list_notes(ctx: ArtemisContext) -> str:
    return await MobileActionExecutor(ctx).exec_list_notes()


async def _exec_ask_explorer(
    query: str,
    context_feedback: str | None,
    state: State,
    ctx: ArtemisContext,
    version: str = "flash",
) -> str:
    return await MobileActionExecutor(ctx).exec_ask_explorer(
        query, context_feedback, state, version=version
    )


def _get_ask_explorer_tool_declaration() -> ToolDeclaration:
    return ASK_EXPLORER_TOOL


class FailureAnalysisStrategy:
    """Base strategy class for FailureAnalyzer strategies."""

    def get_prompt_template_name(self) -> str:
        return "failure_analyzer.md"

    def get_tools(self) -> list:
        return VALIDATOR_TOOLS_DECLARATION

    def required_tools(self) -> frozenset[str]:
        """Tools this strategy's prompt depends on structurally.

        A strategy whose prompt is built around a tool (rather than merely listing it)
        must declare it here; strategy selection falls back to the base strategy when
        the current backend does not provide every required tool, so the dependent
        prompt is never rendered against a backend that cannot satisfy it.
        """
        return frozenset()

    def get_user_message_suffix(self, pre_screenshot: bool, post_screenshot: bool) -> str:
        suffix = (
            "Your objective is:\n"
            "1. Analyze why the action planned in the failed step failed, using the failed step's"
            " details, the decision screenshot (Screenshot Seen During System Decision), the"
            " latest failed screenshot (Latest Screenshot (Failed State)), and their corresponding"
            " UI Element lists.\n"
            "2. Resolve any timing, state mismatches (e.g., re-triggering disappeared elements), or"
            " environmental obstacles using your execution tools.\n"
            "3. Crucially: You MUST execute the failed action yourself using your execution tools"
            " to completion.\n"
            "4. Once you have successfully executed the repair and the failed action, call the"
            " `report_failure_analysis` tool with status='fixed'.\n"
            "5. If you are not confident or cannot resolve the failure, call"
            " `report_failure_analysis` with status='cannot_fix'."
        )
        if pre_screenshot and post_screenshot:
            suffix += (
                "\nI have attached the screenshot seen when the system made its"
                " decision (Screenshot Seen During System Decision) and the"
                " latest screenshot in the failed state (Latest Screenshot"
                " (Failed State))."
            )
        return suffix


class TargetDisappearedStrategy(FailureAnalysisStrategy):
    """Specialized strategy for target disappeared errors."""

    def get_prompt_template_name(self) -> str:
        return "target_disappeared_analyzer.md"

    def get_tools(self) -> list:
        tools = list(VALIDATOR_TOOLS_DECLARATION)
        tools.insert(1, CLICK_SEQUENCE_TOOL)
        tools.append(ASK_EXPLORER_TOOL)
        return tools

    def required_tools(self) -> frozenset[str]:
        # The whole prompt is built around atomic chained execution to defeat turn
        # latency, which *is* click_sequence.
        return frozenset({"click_sequence"})

    def get_user_message_suffix(self, pre_screenshot: bool, post_screenshot: bool) -> str:
        return ""


class PixelTargetDisappearedStrategy(FailureAnalysisStrategy):
    """Specialized strategy for pixel-level target disappeared errors."""

    def get_prompt_template_name(self) -> str:
        return "pixel_target_disappeared_analyzer.md"

    def get_tools(self) -> list:
        tools = list(VALIDATOR_TOOLS_DECLARATION)
        tools.insert(1, CLICK_SEQUENCE_TOOL)
        tools.append(ASK_EXPLORER_TOOL)
        return tools

    def required_tools(self) -> frozenset[str]:
        return frozenset({"click_sequence"})

    def get_user_message_suffix(self, pre_screenshot: bool, post_screenshot: bool) -> str:
        return ""


class FailureAnalyzer:
    """Universal Multi-Model Failure Analyzer."""

    def __init__(self, ctx: ArtemisContext):
        self.ctx = ctx
        self.max_iterations = 15

    def _select_strategy(
        self,
        error_category: ValidationErrorCategory,
        available_tools: frozenset[str] | None = None,
    ) -> FailureAnalysisStrategy:
        if error_category == ValidationErrorCategory.PIXEL_TARGET_DISAPPEARED:
            logger.info("Routing failure analysis to specialized PixelTargetDisappearedStrategy.")
            strategy: FailureAnalysisStrategy = PixelTargetDisappearedStrategy()
        elif error_category == ValidationErrorCategory.TARGET_DISAPPEARED:
            logger.info("Routing failure analysis to specialized TargetDisappearedStrategy.")
            strategy = TargetDisappearedStrategy()
        else:
            logger.info("Routing failure analysis to DefaultAnalysisStrategy.")
            strategy = FailureAnalysisStrategy()

        # Capability gating: a strategy and its prompt are one unit. If the backend
        # cannot satisfy the tools the prompt is built around, fall back to the base
        # strategy so the dependent prompt is never rendered. `None` = no gating.
        if available_tools is not None:
            missing = strategy.required_tools() - available_tools
            if missing:
                logger.warning(
                    f"Strategy {type(strategy).__name__} requires unavailable tool(s)"
                    f" {sorted(missing)}; falling back to base FailureAnalysisStrategy."
                )
                return FailureAnalysisStrategy()
        return strategy

    def _get_active_subgoal_hash(self) -> str:
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

    def _build_plan_and_history(self, steps: list[dict], task_plan: str) -> str:
        active_subgoal_hash = self._get_active_subgoal_hash()
        return build_plan_and_history(
            task_plan,
            steps,
            active_subgoal_hash,
            last_n_detailed=1,
            strict_milestone_pruning=True,
            recent_window_size=3,
            chronological_last_step=True,
            for_failure_analyzer=True,
        )

    def _prune_intermediate_screenshots(self, messages: list[Any]):
        """Prunes binary screenshot blocks from intermediate messages."""
        prune_intermediate_screenshots(messages)

    @trace(type="agent", name="failure_analyzer")
    async def analyze(
        self,
        state: State,
        failed_action: dict,
        error_msg: str,
        pre_screenshot: str | None = None,
        post_screenshot: str | None = None,
        pre_screenshot_name: str | None = None,
        post_screenshot_name: str | None = None,
        executed_actions: list[dict] | None = None,
        unexecuted_actions: list[dict] | None = None,
        error_category: ValidationErrorCategory = ValidationErrorCategory.GENERAL,
    ) -> dict:
        if not post_screenshot or post_screenshot == "None":
            try:
                controller = UnifiedMobileController(self.ctx)
                post_screenshot = await controller.take_screenshot()
            except Exception as e:
                logger.warning(f"Failed to capture post-action fallback screenshot: {e}")

        if not pre_screenshot or pre_screenshot == "None":
            pre_screenshot = post_screenshot

        if not pre_screenshot:
            raise ValueError("Failure analysis requires valid screenshots.")

        controller = UnifiedMobileController(self.ctx)
        executor = McpActionExecutor(self.ctx, controller)

        strategy = self._select_strategy(
            error_category, available_tools=frozenset(executor.action_tool_names)
        )
        prompt_template_name = strategy.get_prompt_template_name()
        tools_declaration = strategy.get_tools()

        try:
            llm = get_llm(self.ctx, name="validator_failure_analyzer")
        except Exception:
            llm = get_llm(self.ctx, name="validator")

        # Pre-Action and Post-Action XML formatting
        pre_xml_list_str = "Not available."
        if hasattr(state, "latest_ui_hierarchy") and state.latest_ui_hierarchy:
            try:
                width = getattr(self.ctx.device, "device_width", 1080)
                height = getattr(self.ctx.device, "device_height", 2400)
                pre_xml_list_str, _, _ = format_minimal_list_with_elements(
                    state.latest_ui_hierarchy, width, height
                )
            except Exception as e:
                logger.warning(f"Failed to format pre-action XML list: {e}")

        post_xml_list_str = "Not available."
        try:
            xml_hierarchy = await controller.get_ui_elements()
            width = getattr(self.ctx.device, "device_width", 1080)
            height = getattr(self.ctx.device, "device_height", 2400)
            ocr_results = []

            fused_xml = fuse_ocr_with_xml(xml_hierarchy, ocr_results)

            post_xml_list_str, elements, _ = format_minimal_list_with_elements(
                fused_xml, width, height
            )
            state.indexed_points = [el["center"] for el in elements]
            state.indexed_elements = elements
        except Exception as e:
            logger.warning(f"Failed to fetch/format post-action XML list: {e}")

        prompt_path = Path(__file__).parent.joinpath(prompt_template_name)
        prompt_template = prompt_path.read_text(encoding="utf-8")

        steps = []
        task_plan = "No task plan yet."
        if self.ctx.data_engine:
            steps = self.ctx.data_engine.get_agent_friendly_steps()
            task_plan_path = get_note_file_path(self.ctx.data_engine.base_dir, "task_plan")
            if task_plan_path.exists():
                try:
                    task_plan = task_plan_path.read_text(encoding="utf-8")
                except Exception as e:
                    task_plan = f"Error reading task plan: {e}"

        plan_and_history = self._build_plan_and_history(steps, task_plan)
        failed_step_number = steps[-1]["step_number"] if steps else 1
        failed_action_description = (
            format_action_clean(failed_action) if failed_action else "Planned Action"
        )

        system_prompt = Template(prompt_template).render(
            initial_goal=getattr(state, "initial_goal", "Unknown Goal"),
            plan_and_history=plan_and_history,
            failed_step_number=failed_step_number,
            failed_action_description=failed_action_description,
        )

        suffix = strategy.get_user_message_suffix(bool(pre_screenshot), bool(post_screenshot))
        user_msg = (
            suffix.strip()
            if suffix
            else "Please analyze and repair the failure shown in the last step of the history."
        )
        is_pixel_strategy = isinstance(strategy, PixelTargetDisappearedStrategy)

        user_content: list[dict] = [{"type": "text", "text": user_msg}]
        if pre_screenshot:
            user_content.append(
                {"type": "text", "text": "--- Screenshot Seen During System Decision ---"}
            )
            user_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{pre_screenshot}"},
                }
            )
            if not is_pixel_strategy:
                user_content.append(
                    {
                        "type": "text",
                        "text": (
                            "--- UI Element List Seen During System Decision ---\n"
                            f"{pre_xml_list_str}"
                        ),
                    }
                )
        if post_screenshot:
            user_content.append(
                {"type": "text", "text": "--- Latest Screenshot (Failed State) ---"}
            )
            user_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{post_screenshot}"},
                }
            )
            if not is_pixel_strategy:
                user_content.append(
                    {
                        "type": "text",
                        "text": (
                            f"--- Latest UI Element List (Failed State) ---\n{post_xml_list_str}"
                        ),
                    }
                )
        if suffix:
            user_content.append(
                {"type": "text", "text": f"\n\n--- INSTRUCTIONS ---\n{suffix.strip()}"}
            )

        messages: list[BaseMessage] = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_content),
        ]

        max_iterations = getattr(self, "max_iterations", 15)
        iterations = 0
        final_report = None
        intermediate_thoughts = []

        while iterations < max_iterations:
            iterations += 1
            self._prune_intermediate_screenshots(messages)
            logger.info(f"Iteration {iterations}: Invoking Universal LLM for FailureAnalyzer...")

            if iterations == max_iterations:
                messages.append(
                    HumanMessage(
                        content=(
                            "[WARNING] This is your final iteration; only"
                            " 'report_failure_analysis' is available."
                        )
                    )
                )
                current_tools = [
                    t for t in tools_declaration if t.name == "report_failure_analysis"
                ]
            else:
                current_tools = tools_declaration

            bound_llm = llm.bind_tools(current_tools)

            max_retries = 3
            response = None
            for attempt in range(max_retries):
                try:
                    response = await bound_llm.ainvoke(messages)
                    break
                except Exception as e:
                    logger.warning(f"Failure analyzer turn failed on attempt {attempt + 1}: {e}")
                    if attempt == max_retries - 1:
                        raise e
                    await asyncio.sleep(1.0 * (2**attempt))

            if response is None:
                break

            messages.append(response)

            raw_text = response.content if isinstance(response.content, str) else ""
            if isinstance(response.content, list):
                raw_text = "".join(
                    b.get("text", "")
                    for b in response.content
                    if isinstance(b, dict) and "text" in b
                )
            if raw_text:
                intermediate_thoughts.append(raw_text)

            tool_calls = response.tool_calls or []
            if not tool_calls:
                messages.append(
                    HumanMessage(
                        content=(
                            "CRITICAL: You did not call the `report_failure_analysis` tool."
                            " You MUST call an action tool to repair or `report_failure_analysis`"
                            " to finish."
                        )
                    )
                )
                continue

            submit_call = next(
                (tc for tc in tool_calls if tc["name"].split(":")[-1] == "report_failure_analysis"),
                None,
            )

            if submit_call:
                args = submit_call.get("args") or {}
                actions = args.get("new_remaining_actions", [])
                final_report = {
                    "status": args.get("status", "cannot_fix"),
                    "analysis": args.get("analysis", ""),
                    "new_remaining_actions": actions,
                    "thoughts": intermediate_thoughts,
                }
                messages.append(
                    ToolMessage(
                        tool_call_id=submit_call.get("id") or str(uuid.uuid4()),
                        name=submit_call["name"],
                        content=json.dumps({"status": "acknowledged"}),
                        status="success",
                    )
                )
                break

            # Execute other tool calls via MobileActionExecutor
            for tc in tool_calls:
                name = tc["name"].split(":")[-1] if ":" in tc["name"] else tc["name"]
                args = tc.get("args") or {}
                tc_id = tc.get("id") or str(uuid.uuid4())
                logger.info(f"Failure analyzer executing tool '{name}'...")

                try:
                    exec_result = await executor.execute(name, args, tc_id, state)
                    messages.append(exec_result.to_langchain_tool_message())
                except Exception as e:
                    logger.error(f"Error executing tool {name}: {e}")
                    messages.append(
                        ToolMessage(
                            tool_call_id=tc_id,
                            name=name,
                            content=f"Error executing tool {name}: {e}",
                            status="error",
                        )
                    )

        return final_report or {
            "status": "cannot_fix",
            "analysis": "Max iterations reached without resolution.",
        }
