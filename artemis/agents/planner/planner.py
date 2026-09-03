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

import base64
import json
from pathlib import Path

from jinja2 import Template
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from artemis.context import ArtemisContext
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.data_engine.trace import CURRENT_TRACE_ID, TraceSpan, trace
from artemis.graph.state import State
from artemis.graph.visibility import strict_state
from artemis.services.llm import (
    acomplete,
    get_llm,
    invoke_llm_with_timeout_message,
    with_fallback,
)
from artemis.tools.index import get_tool_by_name, get_tools_from_wrappers
from artemis.tools.scratchpad import (
    list_notes_wrapper,
    read_note_wrapper,
    save_note_wrapper,
    update_note_wrapper,
)
from artemis.tools.tool_wrapper import (
    get_tool_result_content,
    invoke_tool_with_injection,
)
from artemis.utils.decorators import wrap_with_callbacks
from artemis.utils.logger import get_logger
from artemis.utils.notes import get_note_file_path
from artemis.utils.plan_grammar import (
    CHECKBOX_LINE_RE,
    STATUS_CHARS,
    parse_plan,
    render_plan_grammar_spec,
)
from artemis.memory.context_policy import build_history_for

logger = get_logger(__name__)


def validate_plan_format(content: str) -> tuple[bool, str]:
    """Validates a freshly written plan against the machine-channel grammar.

    Single-sourced from :mod:`artemis.utils.plan_grammar`: any status character
    the grammar accepts (including in-progress ``[/]``) is legal here too, so
    the Planner can never reject a line the rest of the harness would parse.
    """
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("- [") and not CHECKBOX_LINE_RE.match(stripped):
            allowed = ", ".join(f"'[{c}]'" for c in STATUS_CHARS)
            return (
                False,
                f"Invalid task status in line: {line}. Must be one of {allowed}.",
            )

    if not parse_plan(content).items:
        return (
            False,
            "Plan must contain at least one subgoal starting with '- [ ]'.",
        )

    return True, ""


def _checks_enabled(ctx: ArtemisContext) -> bool:
    """Config-driven prompt assembly gate: with both check gates disabled, no
    check-related instruction reaches any planner prompt."""
    setup = getattr(ctx, "execution_setup", None)
    return bool(setup and getattr(setup, "checks_enabled", False))


def build_planner_system_blocks(prompts_data: dict, mode: str, include_checks: bool) -> list[str]:
    """Assembly point for the planner prompt's component list (§1.3): the
    check-related block mounts only while the checking feature is active."""
    blocks = list(prompts_data["modes"][mode]["system"])
    if include_checks:
        extra = "check_generation" if mode == "initial_plan" else "check_audit"
        if extra in prompts_data.get("blocks", {}):
            blocks.append(extra)
    return blocks


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


class ValidationResult(BaseModel):
    model_config = {"ignored_types": (CyFunctionDetector,)}
    is_approved: bool = Field(
        description=(
            "True if the new plan still serves the initial goal and is coherent."
            " False if you have a concrete concern (goal drift, broken loop"
            " contract, weakened check standard, logical dead end)."
        )
    )
    feedback: str = Field(
        description=(
            "If not approved: one or two sentences naming the concern and the"
            " suggested correction. This is advisory feedback shown to the"
            " Operator, not a rejection — the plan change stays applied."
            " If approved, leave empty."
        )
    )


async def run_async_planner_validation(
    ctx: ArtemisContext,
    initial_goal: str,
    content_before: str,
    content_after: str,
    operator_raw_thinking: str | None = None,
    operator_native_thinking: str | None = None,
) -> dict:
    """Asynchronously validates a top-level plan change proposed by the Operator."""
    logger.info("Starting Async Planner Validation...")
    try:
        prompts_path = Path(__file__).parent / "planner.json"
        with open(prompts_path, encoding="utf-8") as f:
            prompts_data = json.load(f)

        include_checks = _checks_enabled(ctx)
        mode_config = prompts_data["modes"]["validator"]
        system_blocks = build_planner_system_blocks(prompts_data, "validator", include_checks)
        system_content = "\n\n".join(prompts_data["blocks"][block] for block in system_blocks)
        system_message = Template(system_content).render(
            plan_grammar=render_plan_grammar_spec(include_checks)
        )

        history_str = "No history available."
        if ctx.data_engine:
            try:
                steps = ctx.data_engine.get_agent_friendly_steps()
                history_str = build_history_for(
                    "planner",
                    "",  # Pass empty string to avoid rendering the task plan in the history
                    steps,
                    "default",
                )

            except Exception as e:
                logger.error(f"Failed to fetch execution history for validator: {e}")

        human_content = mode_config["human"]
        human_message = Template(human_content).render(
            initial_goal=initial_goal,
            content_before=content_before,
            content_after=content_after,
            operator_raw_thinking=operator_raw_thinking,
            operator_native_thinking=operator_native_thinking,
            history_str=history_str,
        )

        messages = [
            SystemMessage(content=system_message),
            HumanMessage(content=human_message),
        ]

        # Advisory review on the lightweight judge node (shared default with
        # the pixel safety net), never the heavyweight Planner model.
        llm = get_llm(ctx=ctx, name="planner_validation").with_structured_output(ValidationResult)

        result: ValidationResult = await invoke_llm_with_timeout_message(llm.ainvoke(messages))

        return {
            "status": "success" if result.is_approved else "failed",
            "feedback": result.feedback,
        }
    except Exception as e:
        logger.error(f"Planner async validation failed: {e}")
        # In case of error, we fallback to approving so we don't block the agent unnecessarily
        return {
            "status": "success",
            "feedback": f"Validation failed with error: {e}",
        }


class PlannerNode:
    def __init__(self, ctx: ArtemisContext, tools: list[BaseTool] | None = None):
        self.ctx = ctx
        self.tools = tools or []

    @wrap_with_callbacks(
        before=lambda: logger.info("Starting Planner Agent..."),
        on_success=lambda _: logger.success("Planner Agent"),
        on_failure=lambda _: logger.error("Planner Agent"),
    )
    @trace(type="agent", name="planner")
    async def __call__(self, state: State):
        state = strict_state(state, "planner")
        prompts_path = Path(__file__).parent / "planner.json"
        with open(prompts_path, encoding="utf-8") as f:
            prompts_data = json.load(f)

        mode = "initial_plan"
        mode_config = prompts_data["modes"][mode]

        include_checks = _checks_enabled(self.ctx)
        # Assembly point (not in-template conditionals): check-generation
        # instructions only mount while the checking feature is active, so a
        # fully-disabled configuration yields a byte-identical planner prompt.
        system_blocks = build_planner_system_blocks(prompts_data, mode, include_checks)
        system_content = "\n\n".join(prompts_data["blocks"][block] for block in system_blocks)

        system_message = Template(system_content).render(
            plan_grammar=render_plan_grammar_spec(include_checks)
        )

        # Get screenshot
        screenshot_b64 = None
        screenshot_path = state.latest_screenshot if hasattr(state, "latest_screenshot") else None

        if screenshot_path and Path(screenshot_path).exists():
            try:
                with open(screenshot_path, "rb") as f:
                    screenshot_b64 = base64.b64encode(f.read()).decode("utf-8")
                logger.info(f"Planner loaded screenshot from file: {screenshot_path}")
            except Exception as e:
                logger.error(f"Failed to read screenshot from {screenshot_path}: {e}")

        # Fallback for Initial Plan: if no screenshot in state, take one on the spot
        if not screenshot_b64:
            logger.info("No screenshot found in state. Taking screenshot for Initial Plan...")
            try:
                controller = UnifiedMobileController(self.ctx)
                screenshot_b64 = await controller.take_screenshot()

                # Save to Data Engine if available to keep trace
                if self.ctx.data_engine:
                    img_bytes = base64.b64decode(screenshot_b64)
                    image_name = self.ctx.data_engine.get_or_create_image(img_bytes)
                    saved_path = str(self.ctx.data_engine.get_image_path(image_name))
                    # Update local state object so it can be used later if needed
                    state.latest_screenshot = saved_path
                    logger.info(f"Saved initial plan screenshot to {saved_path}")
            except Exception as e:
                logger.error(f"Failed to take screenshot for Initial Plan: {e}")

        if not screenshot_b64:
            logger.error("Planner failed to obtain a screenshot.")
            raise ValueError("Planner failed to obtain a screenshot.")

        human_content = mode_config["human"]
        render_kwargs = {
            "initial_goal": state.initial_goal,
        }

        human_message = Template(human_content).render(**render_kwargs)

        human_message_content = [{"type": "text", "text": human_message}]
        if screenshot_b64:
            human_message_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}"},
                }
            )
        messages = [
            SystemMessage(content=system_message),
            HumanMessage(content=human_message_content),
        ]

        llm = get_llm(ctx=self.ctx, name="planner")
        llm_fallback = get_llm(ctx=self.ctx, name="planner", use_fallback=True)

        # Ensure tools are populated (fallback if not passed in __init__, e.g. in tests)
        if not self.tools:
            note_wrappers = [
                save_note_wrapper,
                read_note_wrapper,
                update_note_wrapper,
                list_notes_wrapper,
            ]
            self.tools = get_tools_from_wrappers(self.ctx, note_wrappers)

        all_tools = list(self.tools)

        if all_tools:
            llm = llm.bind_tools(all_tools)
            llm_fallback = llm_fallback.bind_tools(all_tools)

        current_messages = messages
        max_validation_attempts = 3
        max_iterations = 10

        for validation_attempt in range(max_validation_attempts):
            tool_called = False

            for _ in range(max_iterations):
                response = await with_fallback(
                    main_call=lambda: invoke_llm_with_timeout_message(
                        acomplete(llm, current_messages)
                    ),
                    fallback_call=lambda: invoke_llm_with_timeout_message(
                        acomplete(llm_fallback, current_messages)
                    ),
                )

                if response is None or not response.tool_calls:
                    break

                current_messages.append(response)

                for tc in response.tool_calls:
                    tool_name = tc["name"]
                    logger.info(f"Planner called tool: {tool_name} with args: {tc['args']}")
                    if ":" in tool_name:
                        tool_to_run = get_tool_by_name(tool_name, all_tools)
                    else:
                        tool_to_run = next((t for t in all_tools if t.name == tool_name), None)

                    if tool_to_run and tool_to_run.name in [
                        "save_note",
                        "update_note",
                    ]:
                        tool_called = True
                    if tool_to_run:
                        args = dict(tc["args"])
                        with TraceSpan(name=tool_name, ctx=self.ctx) as span:
                            span.payload = {"args": args}
                            try:
                                logger.info(
                                    f"Tool {tool_name} expected args:"
                                    f" {list(tool_to_run.args.keys())}"
                                )

                                logger.info(f"Invoking {tool_name} with args: {list(args.keys())}")
                                result_obj = await invoke_tool_with_injection(
                                    tool=tool_to_run,
                                    args=args,
                                    tool_call_id=tc["id"],
                                    state=state,
                                )
                                result_content = get_tool_result_content(result_obj)
                                span.result = result_content

                                current_messages.append(
                                    ToolMessage(
                                        tool_call_id=tc["id"],
                                        content=result_content,
                                    )
                                )
                            except Exception as e:
                                span.status = "failed"
                                span.error = str(e)
                                current_messages.append(
                                    ToolMessage(
                                        tool_call_id=tc["id"],
                                        content=f"Error: {e}",
                                        status="error",
                                    )
                                )
                    else:
                        current_messages.append(
                            ToolMessage(
                                tool_call_id=tc["id"],
                                content=f"Error: Tool {tool_name} not found",
                                status="error",
                            )
                        )

            # Post-execution validation
            plan_exists = False
            plan_valid = False
            error_message = ""

            if not tool_called:
                error_message = (
                    "You did not call `save_note` or `update_note` to submit/update the plan."
                )
            elif self.ctx.data_engine:
                task_plan_path = get_note_file_path(self.ctx.data_engine.base_dir, "task_plan")
                if task_plan_path.exists():
                    plan_exists = True
                    try:
                        content = task_plan_path.read_text(encoding="utf-8")
                        plan_valid, error_message = validate_plan_format(content)
                    except Exception as e:
                        error_message = f"Failed to read task plan for validation: {e}"
                else:
                    error_message = (
                        "Task plan under key 'task_plan' does not exist despite tool call."
                    )

            if tool_called and plan_exists and plan_valid:
                logger.info("Plan submitted and validated successfully.")
                break

            logger.warning(
                f"Plan validation failed (attempt {validation_attempt + 1}): {error_message}"
            )

            # Feedback to model
            current_messages.append(
                HumanMessage(
                    content=(
                        f"Plan validation failed: {error_message}\nPlease"
                        " correct the plan and try again by calling the"
                        " appropriate tool."
                    )
                )
            )

        else:
            logger.error(
                f"Failed to generate a valid plan after {max_validation_attempts} attempts."
            )

        return {}
