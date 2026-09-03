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

import json
from typing import Any
from uuid import uuid4

from langchain_core.messages import SystemMessage, ToolMessage
from langchain_core.tools import BaseTool

from artemis.context import ArtemisContext
from artemis.data_engine.trace import (
    TraceSpan,
    trace,
    trace_langchain_tool,
)
from artemis.graph.state import State
from artemis.graph.visibility import strict_state
from artemis.mcp.action_specs import OPERATOR_SHELL_ORDER, operator_shell_tool
from artemis.services.llm import acomplete, get_llm, invoke_llm_with_timeout_message
from artemis.tools.command_tool import (
    analyze_task_output_wrapper,
    get_adb_task_registry,
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
from artemis.utils.element_hit_test import find_element_at_point
from artemis.utils.logger import get_logger
from artemis.utils.notes import get_note_file_path
from artemis.memory.context_policy import build_history_for
from artemis.utils.task_tree import get_active_subgoal_hashes
from artemis.utils.visualization import format_minimal_list_with_elements

logger = get_logger(__name__)

DEFERRING_TOOLS = {
    "ask_diagnoser",
    "video_analyzer",
    "read_note",
    "list_notes",
    "recall_history",
    "run_adb_command",
    "manage_task",
    "analyze_task_output",
    "ask_explorer",
}

from artemis.agents.operator.prompts import (
    OPERATOR_MAX_TOOL_ITERATIONS,
    PromptBuilder,
    PromptComponent,
    TemplatePromptComponent,
    ObservationPromptComponent,
    PlanRecitationPromptComponent,
    ScreenshotSimilarityPromptComponent,
    HistoricalStateHintPromptComponent,
    FeedbackPromptComponent,
    ExecutionIncidentPromptComponent,
    CheckItemsExplainerPromptComponent,
    BackgroundTasksPromptComponent,
    TaskPlanWarningPromptComponent,
    ToolLimitWarningPromptComponent,
    InjectedInstructionPromptComponent,
    render_transcript_static_system,
)
from artemis.agents.operator.prompts import load_operator_prompts


class OperatorNode:
    def __init__(
        self,
        ctx: ArtemisContext,
        tools: list[BaseTool] | None = None,
        prompt_components: list[PromptComponent] | None = None,
        last_n_detailed: int = 1,
        transcript_config=None,
    ):
        self.ctx = ctx
        self.tools = tools or []
        self.prompt_components = prompt_components or []
        self.last_n_detailed = last_n_detailed
        try:
            self.prompts = load_operator_prompts()
        except (OSError, ValueError, json.JSONDecodeError) as e:
            raise RuntimeError(f"Failed to load operator prompts: {e}") from e

        # M2 transcript flag (agent.memory.transcript.enabled). Off keeps the
        # legacy 2-message prompt path byte-for-byte; an explicit
        # ``transcript_config`` overrides the loaded configuration (tests).
        if transcript_config is not None:
            self._transcript_cfg = transcript_config
        else:
            try:
                from artemis.config import load_agent_config

                self._transcript_cfg = load_agent_config().memory.transcript
            except Exception:
                from artemis.config.agent import MemoryTranscriptConfig

                self._transcript_cfg = MemoryTranscriptConfig()
        # Index into the rendered message list where this turn's tail begins;
        # set by the transcript build and consumed after the tool loop.
        self._transcript_turn_base: int | None = None

    def _available_device_actions(self) -> frozenset[str]:
        """Device actions the installed actuator backend provides.

        Without an explicit ``ctx.actuator`` the full manifest set applies (the
        default AdbActuator implements everything), so behavior is unchanged until a
        partial backend is actually installed.
        """
        from artemis.mcp.action_manifest import (
            OPTIONAL_ACTIONS,
            REQUIRED_ACTIONS,
            available_device_actions,
        )

        actuator = getattr(self.ctx, "actuator", None)
        if actuator is not None and callable(getattr(actuator, "capabilities", None)):
            try:
                extension_names = frozenset(
                    e.name for e in actuator.extensions() if "operator" in e.targets
                )
                return available_device_actions(actuator) | extension_names
            except Exception as e:
                logger.warning(f"Failed to read actuator capabilities: {e}")
        return REQUIRED_ACTIONS | OPTIONAL_ACTIONS

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
        self._transcript_turn_base = None
        if getattr(self._transcript_cfg, "enabled", False):
            try:
                return await self._build_prompt_transcript(
                    state,
                    latest_screenshot_b64,
                    minimal_list,
                    current_step_num,
                    steps,
                    task_plan,
                    active_background_tasks=active_background_tasks,
                    newly_finished_tasks=newly_finished_tasks,
                )
            except Exception as e:
                logger.error(
                    f"Transcript prompt path failed; falling back to the legacy"
                    f" 2-message build for this turn: {e}"
                )
                self._transcript_turn_base = None

        plan_and_history = self._build_plan_and_history(steps, task_plan)

        builder = PromptBuilder()

        # One constant component list: the prompt template is never switched by
        # verification results. Check-related components are append-only and
        # render nothing when there is nothing to say.
        components = self.prompt_components
        if not components:
            components = [
                (
                    TemplatePromptComponent(),
                    {"template_name": "main_template"},
                ),
                (CheckItemsExplainerPromptComponent(), {}),
                (ExecutionIncidentPromptComponent(), {}),
                (ObservationPromptComponent(), {}),
                (ScreenshotSimilarityPromptComponent(), {}),
                (HistoricalStateHintPromptComponent(), {}),
                (InjectedInstructionPromptComponent(), {}),
                (BackgroundTasksPromptComponent(), {}),
                (FeedbackPromptComponent(), {}),
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

    # ------------------------------------------------------------------
    # Transcript prompt path (M2, flag agent.memory.transcript.enabled)
    # ------------------------------------------------------------------

    def _ensure_transcript_ledger(self, state=None):
        """Session ledger on the composition root, created on first use."""
        from artemis.memory import TranscriptLedger, ensure_step_memory

        ledger = getattr(self.ctx, "transcript_ledger", None)
        if isinstance(ledger, TranscriptLedger):
            return ledger
        cfg = self._transcript_cfg
        # Anchor the ``T+mm:ss`` clock to the DataEngine session start so the
        # tail offsets agree with the chunk ledger lines, the restored-history
        # block and the video analyzer's action timeline.
        session_start = getattr(self.ctx.data_engine, "session_start_time", None)
        ledger = TranscriptLedger(
            step_memory=ensure_step_memory(self.ctx),
            image_scrub_depth=getattr(cfg, "image_scrub_depth", 3),
            pending_grace_steps=getattr(cfg, "pending_grace_steps", 3),
            xml_scrub_depth=getattr(cfg, "xml_scrub_depth", 1),
            session_start=session_start if isinstance(session_start, (int, float)) else None,
        )
        # L2/L3 chunk compression (M3) rides the same flag; without a
        # DataEngine there are no step records to chunk, so the ledger simply
        # runs scrub-edge-only.
        if self.ctx.data_engine is not None:
            try:
                from artemis.memory import HistoryChunkManager

                chunking_cfg = None
                try:
                    from artemis.config import load_agent_config

                    chunking_cfg = load_agent_config().memory.chunking
                except Exception as exc:
                    logger.debug(
                        "Chunking config unavailable; using chunker defaults: %s",
                        exc,
                        exc_info=True,
                    )
                ledger.attach_chunker(
                    HistoryChunkManager(
                        engine=self.ctx.data_engine,
                        ctx=self.ctx,
                        chunking_config=chunking_cfg,
                        transcript_config=cfg,
                        goal=getattr(state, "initial_goal", None) if state else None,
                    )
                )
            except Exception as e:
                logger.error(f"History chunk manager unavailable: {e}")
        try:
            self.ctx.transcript_ledger = ledger
        except (AttributeError, TypeError, ValueError):
            pass
        return ledger

    async def _build_prompt_transcript(
        self,
        state: State,
        latest_screenshot_b64: str,
        minimal_list: str,
        current_step_num: int,
        steps: list[dict],
        task_plan: str,
        active_background_tasks: list[dict] = None,
        newly_finished_tasks: list[dict] = None,
    ) -> list:
        """Build ``S + F + A + tail`` from the session transcript ledger.

        The static system message renders once per session (byte-stable S
        region); the previous turn is committed into the append-only active
        region together with its step key and validator result; the fresh tail
        carries the current observation plus the task-plan recitation.
        """
        from langchain_core.messages import HumanMessage

        ledger = self._ensure_transcript_ledger(state)

        # 1. S region: rendered exactly once per session.
        if not ledger.has_static_prefix:
            static_text = render_transcript_static_system(self.prompts, self.ctx, state)
            ledger.set_static_prefix([SystemMessage(content=static_text)])

        # 2. F region cold start: an empty ledger over an existing step record
        # trail means the process restarted — freeze the compiled history once.
        # A staged (not yet committed) turn means this is turn 2+ of a live
        # session, not a cold start — step records already exist then, and
        # seeding would be rejected by the empty-ledger invariant.
        if (
            ledger.turn_count == 0
            and not ledger.has_staged_turn
            and not ledger.has_restored_history
            and steps
        ):
            restored = build_history_for(
                "operator_cold_start",
                task_plan,
                steps,
                self._get_active_subgoal_hash(),
            )
            ledger.set_restored_history(
                "[Restored history] Rebuilt from the step records of this task"
                " after a process restart; step times below are relative to the"
                " original session start.\n"
                f"{restored}"
            )

        # 3. Commit the previous turn (its step id and validator result only
        # exist now). A turn without a terminal action never ran the validator,
        # and ``structured_decisions`` is cleared on planner rejection — the
        # gate keeps stale reports out of the ledger.
        validator_result = (
            getattr(state, "last_execution_result", None)
            if getattr(state, "structured_decisions", None)
            else None
        )
        ledger.commit_staged(
            step_key=getattr(state, "current_step_id", None),
            validator_result=validator_result,
        )

        # 4. Current tail: observation + plan recitation + injected components.
        builder = PromptBuilder()
        builder.add_human_content(f"# CURRENT OBSERVATION [{ledger.elapsed_label()}]")
        components = [
            (PlanRecitationPromptComponent(), {}),
            (CheckItemsExplainerPromptComponent(), {}),
            (ExecutionIncidentPromptComponent(), {}),
            (ObservationPromptComponent(), {}),
            (ScreenshotSimilarityPromptComponent(), {}),
            (HistoricalStateHintPromptComponent(), {}),
            (InjectedInstructionPromptComponent(), {}),
            (BackgroundTasksPromptComponent(), {}),
            (FeedbackPromptComponent(), {}),
            (TaskPlanWarningPromptComponent(), {}),
            (ToolLimitWarningPromptComponent(), {}),
        ]
        for component, extra_kwargs in components:
            kwargs_to_pass = {
                "prompts": self.prompts,
                "task_plan": task_plan,
                "latest_screenshot_b64": latest_screenshot_b64,
                "minimal_list": minimal_list,
                "current_step_num": current_step_num,
                "active_background_tasks": active_background_tasks or [],
                "newly_finished_tasks": newly_finished_tasks or [],
                "steps": steps,
            }
            kwargs_to_pass.update(extra_kwargs)
            await component(builder, state, self.ctx, **kwargs_to_pass)

        tail_content = []
        for part in builder.human_parts:
            if isinstance(part, str):
                tail_content.append({"type": "text", "text": part})
            else:
                tail_content.append(part)
        tail = HumanMessage(content=tail_content)

        messages = ledger.render([tail])
        self._transcript_turn_base = len(messages) - 1
        return messages

    def _stage_transcript_turn(self, messages: list) -> None:
        """Hand this turn's tail + tool-loop products to the ledger."""
        base = self._transcript_turn_base
        self._transcript_turn_base = None
        if base is None:
            return
        try:
            ledger = getattr(self.ctx, "transcript_ledger", None)
            if ledger is not None:
                ledger.stage_turn(messages[base:])
        except Exception as e:
            logger.error(f"Failed to stage transcript turn: {e}")

    async def _invoke_llm_loop(
        self,
        base_llm,
        current_messages: list,
        traced_tools: list,
        new_subagent_calls: list,
        state: State,
    ) -> tuple[list[dict] | None, str | None, str | None, bool]:
        max_iterations = OPERATOR_MAX_TOOL_ITERATIONS
        action_result = None
        raw_thoughts = []
        native_thoughts = []
        tool_limit_exceeded = False

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
            response = await invoke_llm_with_timeout_message(acomplete(bound_llm, current_messages))

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
                and type(additional_kwargs) is dict
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

            # Manifest-driven, so a backend's extension actions are classified as
            # turn-ending device actions without touching this file.
            action_tool_names = sorted(self._available_device_actions())

            def normalize_name(name: str) -> str:
                return name.split(":")[-1] if ":" in name else name

            action_calls = [
                tc for tc in response.tool_calls if normalize_name(tc["name"]) in action_tool_names
            ]
            other_calls = [
                tc
                for tc in response.tool_calls
                if normalize_name(tc["name"]) not in action_tool_names
            ]

            tool_outputs = []
            validation_errors = False

            other_tool_failed = False
            other_tool_failure_msg = ""

            has_terminal_calls = bool(action_calls)

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

                                if isinstance(result_obj, ToolMessage) and result_obj.status:
                                    status = result_obj.status

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

                        action_calls = []

            if action_calls:
                logger.info(
                    f"Operator requested {len(action_calls)} action(s)."
                    " Translating and processing all."
                )
                actions_list = []
                burst_cap = self._max_burst_actions()
                if len(action_calls) > burst_cap:
                    # A multi-action turn is a fast-action burst that the Validator
                    # fires without the safety net; cap its length before it runs.
                    validation_errors = True
                    for tc in action_calls:
                        tool_outputs.append(
                            ToolMessage(
                                tool_call_id=tc["id"],
                                content=(
                                    f"Error: {len(action_calls)} Turn-Ending Actions in one"
                                    " turn exceed the fast-action burst limit of"
                                    f" {burst_cap}. Nothing was executed. Re-issue at most"
                                    f" {burst_cap} actions (a burst is for sub-second"
                                    " sequences on transient UI, not for batching normal"
                                    " steps), or a single vetted action."
                                ),
                                status="error",
                            )
                        )
                    action_calls_to_translate = []
                else:
                    action_calls_to_translate = action_calls
                for tc in action_calls_to_translate:
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

            for tm in tool_outputs:
                current_messages.append(tm)

            if action_calls and not validation_errors:
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
        state = strict_state(state, "operator")
        # 1. Get perception data from state (populated by Perception node)
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
            except (AttributeError, TypeError):
                pass
        minimal_list, elements, labels = format_minimal_list_with_elements(fused_xml, width, height)

        # Save transient indexed_points and indexed_elements on the state instance
        state.indexed_points = [el["center"] for el in elements]
        state.indexed_elements = elements

        current_step_num = len(steps) + 1

        # 5. Prepare Tools. Device-action shells come from the canonical manifest
        # (artemis/mcp/action_specs.py) and are assembled against the installed
        # actuator backend's capabilities: an action the backend does not implement
        # is simply never declared (and the prompt assembly drops its teaching
        # segments in lockstep).
        available_actions = self._available_device_actions()
        all_tools = [
            operator_shell_tool(name) for name in OPERATOR_SHELL_ORDER if name in available_actions
        ] + self.tools

        # 5. Mount the task-output analyzer and collect background ADB task state.
        # The analyzer is always available: the "output truncated, use
        # analyze_task_output" hint arrives mid-turn, and tools are bound once per
        # turn, so a conditional mount would be one turn late.
        if not any(t.name == "analyze_task_output" for t in all_tools):
            all_tools.append(analyze_task_output_wrapper.tool_fn_getter(self.ctx))

        traced_tools = [trace_langchain_tool(t, self.ctx) for t in all_tools]

        adb_registry = get_adb_task_registry(self.ctx)
        active_background_tasks = adb_registry.active_task_summaries()
        newly_finished_tasks = adb_registry.pop_unnotified_finished()

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

        # 8b. Transcript path: hold this turn's messages (observation tail +
        # tool-loop products) for commit at the next build, when the turn's
        # step id and validator result exist.
        self._stage_transcript_turn(messages)

        # 9. Update State
        structured_decisions = json.dumps(action_result) if action_result else None

        return {
            "structured_decisions": structured_decisions,
            "operator_raw_thinking": raw_thinking,
            "operator_native_thinking": native_thinking,
            "indexed_points": state.indexed_points,
            "indexed_elements": state.indexed_elements,
            "current_step_id": state.current_step_id,
            "subagent_calls": ((state.subagent_calls or []) + new_subagent_calls),
            "operator_tool_limit_exceeded": tool_limit_exceeded,
        }

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
        return build_history_for(
            "operator",
            task_plan,
            steps,
            active_subgoal_hash,
            last_n_detailed=self.last_n_detailed,
        )

    def _max_burst_actions(self) -> int:
        """Configured ceiling for actions in one fast-action burst (default 4)."""
        try:
            from artemis.config import load_agent_config

            return int(load_agent_config().pro.execution.max_burst_actions)
        except Exception:
            return 4

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
            except (AttributeError, TypeError):
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
                    return {**indexed_elements[target_int - 1], "label_source": "index"}, None
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
                    # Record-time enrichment: a bare-coordinate target carries no
                    # element semantics, so hit test the pre-action frame's indexed
                    # elements to best-effort recover what sits under the point.
                    hit_el, hit_source = find_element_at_point(
                        getattr(state, "indexed_elements", None), x, y
                    )
                    hit_el = hit_el or {}
                    return {
                        "center": [x, y],
                        "text": hit_el.get("text"),
                        "bounds": hit_el.get("bounds"),
                        "class": hit_el.get("class"),
                        "resource_id": hit_el.get("resource_id"),
                        "is_ocr": bool(hit_el.get("is_ocr")),
                        "label_source": hit_source,
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
                    "target_label_source": el.get("label_source", "none"),
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
                    "target_label_source": el.get("label_source", "none"),
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
                    "target_label_source": el.get("label_source", "none"),
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
                    ui_hierarchy=getattr(state, "latest_ui_hierarchy", None),
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
