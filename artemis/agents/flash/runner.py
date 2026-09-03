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

"""Universal Multi-Model FlashRunner for Artemis.

Executes autonomous reactive mobile workflows across Google Gemini,
OpenAI GPT-4o/o3, Anthropic Claude 3.5/3.7, and OpenRouter endpoints.

Conversation shape (shared with the Pro operator, history redesign §3.2):
the prompt is built every turn from a session :class:`TranscriptLedger` —
a byte-stable system prefix, the committed earlier turns (append-only; old UI
lists stripped at depth 1, screenshots resolved to visual summaries at depth
K, long spans chunk-compressed), and a fresh tail carrying the current
observation under a ``# CURRENT OBSERVATION [T+mm:ss]`` header. Each
committed turn ends with an ``--- Action Execution Result (T+mm:ss) ---``
message, so every timestamp the model sees is a session-relative offset — the
same clock the video analyzer uses for the session recording.
"""

import asyncio
import base64
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import uuid

from jinja2 import Template
from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from artemis.agents.flash.summarizer import VisualStepSummarizer
from artemis.agents.validator.tool_declarations import (
    ASK_EXPLORER_TOOL,
    CLICK_SEQUENCE_TOOL,
    REPORT_TASK_STATUS_TOOL,
    VALIDATOR_TOOLS_DECLARATION,
    capture_screenshot_and_parse_ui,
    prune_intermediate_screenshots,
)
from artemis.config import (
    MemoryRuntimeConfig,
    MemoryTranscriptConfig,
    StepSummarizerConfig,
    load_agent_config,
)
from artemis.context import ArtemisContext
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.data_engine.trace import trace
from artemis.graph.perception import _check_injected_instruction_file
from artemis.graph.state import State
from artemis.llm.structured import ParseFailure, parse_structured
from artemis.mcp.action_executor import McpActionExecutor
from artemis.memory.transcript import PRO_UI_LIST_MARKER, TranscriptLedger
from artemis.services.llm import (
    RobustChatModelWrapper,
    acomplete,
    get_google_llm,
    get_llm,
    invoke_llm_with_timeout_message,
)
from artemis.tools.history import history_tool_declarations
from artemis.tools.tool_wrapper import tool_result_messages
from artemis.utils.coordinates import (
    COORDINATE_SPACE_KEY,
    COORDINATE_SPACE_NORMALIZED,
    parse_swipe_parameters,
)
from artemis.utils.logger import get_logger

logger = get_logger(__name__)

#: Reminder carried by the first observation only (the system prompt states the rule).
_REASONING_FIRST_REMINDER = (
    "CRITICAL RULE: In every single turn, you MUST FIRST output a natural"
    " language reasoning/explanation paragraph BEFORE invoking any tool call."
)

_NO_TOOL_CALL_NOTICE = (
    "You did not call any tools last turn. Please make progress by calling an"
    " action tool or 'report_task_status'."
)

_FINAL_TURN_WARNING = "[WARNING] This is your final turn; only 'report_task_status' is available."


@dataclass
class _TurnRecord:
    """What one reactive turn produced, for committing it into the ledger.

    ``step_keys`` are the DataEngine step ids recorded this turn (one per
    executed action; the tool_call_id stands in without a DataEngine) — the
    first keys the observation screenshot to its visual-transition summary,
    all of them feed the chunk ledger. ``actions`` collects the outcome of
    every device action so the turn's execution result can be rendered.
    """

    step_keys: list[str] = field(default_factory=list)
    actions: list[tuple[str, str, str]] = field(default_factory=list)

    def result(self) -> dict | None:
        """The turn's execution result in the validator-report shape.

        ``None`` when no device action ran (helper-only turns have no result
        message, as in Pro).
        """
        if not self.actions:
            return None
        for name, status, text in self.actions:
            if status != "success":
                return {"status": "failed", "error": f"{name}: {text}"}
        return {"status": "success"}


class FlashRunner:
    """Reactive, ultra-fast agent execution loop supporting all VLM providers."""

    def __init__(self, ctx: ArtemisContext, goal: str, max_turns: int | None = None):
        self.ctx = ctx
        self.goal = goal
        try:
            cfg = load_agent_config()
            self.max_turns = max_turns if max_turns is not None else cfg.flash.max_turns
            self.step_summarizer_cfg = cfg.flash.step_summarizer
            self.memory_runtime_cfg = cfg.memory.runtime
            self.transcript_cfg = cfg.memory.transcript
            self.chunking_cfg = cfg.memory.chunking
        except Exception:
            self.max_turns = max_turns if max_turns is not None else 0
            self.step_summarizer_cfg = StepSummarizerConfig()
            self.memory_runtime_cfg = MemoryRuntimeConfig()
            self.transcript_cfg = MemoryTranscriptConfig()
            self.chunking_cfg = None

        self.controller = UnifiedMobileController(ctx)
        # ``agent_name="flash"`` makes ask_explorer follow ``explorer.flash_mode``
        # (the Flash profile knob) instead of the Pro profile's tier.
        self.executor = McpActionExecutor(ctx, self.controller, agent_name="flash")
        self.summarizer = (
            VisualStepSummarizer(
                ctx,
                model_name=self.step_summarizer_cfg.model,
                retry_limit=self.memory_runtime_cfg.retry_limit,
                max_concurrency=self.memory_runtime_cfg.max_concurrency,
                flush_timeout_s=self.memory_runtime_cfg.flush_timeout_s,
            )
            if self.step_summarizer_cfg.enabled
            else None
        )
        # Publish the service on the composition root (ctx.step_memory, M2) so
        # any co-resident consumer shares this run's summary runtime.
        if self.summarizer is not None and getattr(ctx, "step_memory", None) is None:
            try:
                ctx.step_memory = self.summarizer
            except (AttributeError, TypeError, ValueError):
                pass

    @property
    def turn_limit(self) -> int | None:
        """The reactive turn cap, or ``None`` when the loop is unbounded."""
        try:
            limit = int(self.max_turns) if self.max_turns is not None else 0
        except (TypeError, ValueError):
            return None
        return limit if limit > 0 else None

    def _video_tools_enabled(self) -> bool:
        """Same gate the Pro graph applies before binding ``video_analyzer``."""
        setup = getattr(self.ctx, "execution_setup", None)
        return getattr(setup, "video_recording_tools_enabled", False) is True

    def _get_tools(self) -> list:
        tools = list(VALIDATOR_TOOLS_DECLARATION)
        tools.insert(1, CLICK_SEQUENCE_TOOL)
        tools.append(ASK_EXPLORER_TOOL)
        # Helper tools shared with the Pro operator: the history tools are
        # declared from the very same args schemas the LangChain tools bind
        # (same availability gates: a DataEngine session, recall config).
        tools.extend(history_tool_declarations(self.ctx))
        if self._video_tools_enabled():
            from artemis.tools.video_tool import VIDEO_ANALYZER_TOOL

            tools.append(VIDEO_ANALYZER_TOOL)
        tools.append(REPORT_TASK_STATUS_TOOL)
        # With an actuator installed, drop declarations for device actions the
        # backend does not implement (and append its extension tools); without
        # one, the full historical declaration set is kept.
        actuator = getattr(self.ctx, "actuator", None)
        if actuator is not None:
            from artemis.mcp.action_manifest import filter_declarations

            tools = filter_declarations(tools, actuator, "flash")
        return tools

    def _prune_intermediate_screenshots(self, messages: list[BaseMessage]) -> None:
        """Prunes binary screenshot blocks from intermediate observation messages."""
        prune_intermediate_screenshots(messages)

    # ------------------------------------------------------------------
    # run() setup helpers
    # ------------------------------------------------------------------

    def _build_ledger(self) -> TranscriptLedger:
        """Builds the session transcript ledger with the shared Pro history policy.

        The ``T+mm:ss`` clock is anchored to the DataEngine session start so
        the observation headers, the chunk ledger lines and the video
        analyzer's action timeline share one origin. L2/L3 chunk compression
        needs DataEngine step records; without an engine the ledger runs
        scrub-edge-only.
        """
        engine = getattr(self.ctx, "data_engine", None)
        session_start = getattr(engine, "session_start_time", None) if engine else None
        cfg = self.transcript_cfg
        ledger = TranscriptLedger(
            step_memory=self.summarizer,
            prune_history_xml=self.step_summarizer_cfg.prune_history_xml,
            image_scrub_depth=getattr(cfg, "image_scrub_depth", 3),
            pending_grace_steps=getattr(cfg, "pending_grace_steps", 3),
            xml_scrub_depth=getattr(cfg, "xml_scrub_depth", 1),
            session_start=session_start if isinstance(session_start, (int, float)) else None,
        )
        if engine is not None:
            try:
                from artemis.memory import HistoryChunkManager

                ledger.attach_chunker(
                    HistoryChunkManager(
                        engine=engine,
                        ctx=self.ctx,
                        chunking_config=self.chunking_cfg,
                        transcript_config=cfg,
                        goal=self.goal,
                    )
                )
            except Exception as e:
                logger.error(f"History chunk manager unavailable for FlashRunner: {e}")
        try:
            self.ctx.transcript_ledger = ledger
        except (AttributeError, TypeError, ValueError):
            pass
        return ledger

    #: The model of the current run; decides how screenshot tool results travel.
    _llm = None

    def _init_llm(self):
        """Initializes the Universal LLM via the Service Layer."""
        try:
            return get_llm(self.ctx, name="operator")
        except Exception as e:
            logger.warning(f"Failed to get operator LLM from config, using default: {e}")

            return RobustChatModelWrapper(get_google_llm(model_name="gemini-2.5-flash"), self.ctx)

    def _render_system_prompt(self, tools_declaration: list) -> str:
        """Renders the system prompt from the flash_runner.md template.

        Tool-teaching segments are gated on the available tool set so an absent
        tool leaves no trace in the prompt; until an actuator is wired in, the
        full manifest set reproduces the historical prompt.
        """
        prompt_path = Path(__file__).parent / "flash_runner.md"
        prompt_template = prompt_path.read_text(encoding="utf-8")
        available_tools = frozenset(t.name for t in tools_declaration)
        return Template(prompt_template).render(goal=self.goal, available_tools=available_tools)

    # ------------------------------------------------------------------
    # Per-turn helpers (observe / think)
    # ------------------------------------------------------------------

    async def _read_injected_instruction(self) -> str | None:
        """Returns the real-time injected instruction text for this turn, if any."""
        if not (self.ctx.data_engine and self.ctx.data_engine.base_dir):
            return None
        try:
            injected_payload = await asyncio.to_thread(
                _check_injected_instruction_file,
                str(self.ctx.data_engine.base_dir),
            )
        except (OSError, ValueError, AttributeError) as e:
            logger.warning(f"Failed to check injected instruction in FlashRunner: {e}")
            return None
        if not (injected_payload and injected_payload.get("instruction")):
            return None
        injected_text = (
            "[REAL-TIME INJECTED INSTRUCTION from user]:"
            f" {injected_payload['instruction']}\nYou MUST immediately"
            " follow this instruction and adjust your plan/actions."
        )
        if injected_payload.get("release_loop"):
            injected_text += (
                "\nThe user has explicitly authorized stopping any"
                " ongoing monitoring loop; you may now wrap up and"
                " complete the task."
            )
        return injected_text

    def _build_tail(
        self,
        ledger: TranscriptLedger,
        turns: int,
        img_bytes,
        xml_list,
        *,
        injected: str | None = None,
        notices: list[str] | None = None,
        is_final: bool = False,
    ) -> HumanMessage:
        """Builds this turn's observation tail (Pro observation shape).

        Header, screenshot and UI list carry the same markers as the Pro
        operator's tail so the ledger's scrub edge treats both alike.
        """
        blocks: list[dict] = []
        if turns == 1:
            blocks.append({"type": "text", "text": f"Your objective is: {self.goal}"})
        blocks.append({"type": "text", "text": f"# CURRENT OBSERVATION [{ledger.elapsed_label()}]"})
        if img_bytes:
            img_b64 = base64.b64encode(img_bytes).decode("utf-8")
            blocks.append({"type": "text", "text": "--- Current Screenshot ---"})
            blocks.append(
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}
            )
        if xml_list:
            blocks.append({"type": "text", "text": f"{PRO_UI_LIST_MARKER}\n{xml_list}"})
        for notice in notices or []:
            blocks.append({"type": "text", "text": notice})
        if injected:
            blocks.append({"type": "text", "text": injected})
        if is_final:
            blocks.append({"type": "text", "text": _FINAL_TURN_WARNING})
        if turns == 1:
            blocks.append({"type": "text", "text": _REASONING_FIRST_REMINDER})
        return HumanMessage(content=blocks)

    def _extract_response_text(self, response) -> str:
        """Extracts the natural-language thought text from the model response."""
        raw_text = response.content if isinstance(response.content, str) else ""
        if isinstance(response.content, list):
            raw_text = "".join(
                b.get("text", "") for b in response.content if isinstance(b, dict) and "text" in b
            )
        return raw_text

    def _token_usage_from_response(self, response) -> dict | None:
        """Extracts token usage metadata from the model response, if present."""
        step_token_usage = None
        if hasattr(response, "usage_metadata") and response.usage_metadata:
            u = response.usage_metadata
            pr = u.get("input_tokens") or u.get("prompt_tokens") or 0
            co = u.get("output_tokens") or u.get("completion_tokens") or 0
            to = u.get("total_tokens") or (pr + co)
            if to > 0:
                step_token_usage = {
                    "prompt_tokens": int(pr),
                    "completion_tokens": int(co),
                    "total_tokens": int(to),
                }
        elif hasattr(response, "response_metadata") and isinstance(
            response.response_metadata, dict
        ):
            u = response.response_metadata.get("usage_metadata") or response.response_metadata.get(
                "token_usage"
            )
            if isinstance(u, dict):
                pr = (
                    u.get("input_tokens")
                    or u.get("prompt_tokens")
                    or u.get("prompt_token_count")
                    or 0
                )
                co = (
                    u.get("output_tokens")
                    or u.get("completion_tokens")
                    or u.get("candidates_token_count")
                    or 0
                )
                to = u.get("total_tokens") or u.get("total_token_count") or (pr + co)
                if to > 0:
                    step_token_usage = {
                        "prompt_tokens": int(pr),
                        "completion_tokens": int(co),
                        "total_tokens": int(to),
                    }
        return step_token_usage

    def _estimate_token_usage(self, messages: list[BaseMessage], raw_text: str) -> dict:
        """Approximates token usage from message sizes when metadata is absent."""
        calc_prompt_chars = 0
        calc_images = 0
        for msg in messages:
            c = getattr(msg, "content", "")
            if isinstance(c, str):
                calc_prompt_chars += len(c)
            elif isinstance(c, list):
                for block in c:
                    if isinstance(block, dict):
                        if block.get("type") == "image_url" or "image_url" in block:
                            calc_images += 1
                        else:
                            calc_prompt_chars += len(str(block.get("text", "")))
                    else:
                        calc_prompt_chars += len(str(block))
        prompt_tokens = (calc_prompt_chars // 4) + (calc_images * 258)
        completion_tokens = len(raw_text) // 4
        return {
            "prompt_tokens": max(1, prompt_tokens),
            "completion_tokens": max(1, completion_tokens),
            "total_tokens": max(1, prompt_tokens + completion_tokens),
        }

    def _resolve_token_usage(self, response, messages: list[BaseMessage], raw_text: str) -> dict:
        """Extracts token usage from response metadata, estimating as fallback."""
        step_token_usage = self._token_usage_from_response(response)
        if not step_token_usage or step_token_usage.get("total_tokens", 0) <= 0:
            step_token_usage = self._estimate_token_usage(messages, raw_text)
        return step_token_usage

    def _record_llm_trace(self, step_token_usage: dict, raw_text: str) -> None:
        """Records the llm_call trace for this turn in the DataEngine."""
        if self.ctx.data_engine:
            current_step_id = getattr(self.ctx.data_engine, "current_step_id", None)
            self.ctx.data_engine.record_trace(
                type="llm_call",
                name="FlashRunner",
                payload={"token_usage": step_token_usage, "response": raw_text},
                step_id=current_step_id,
                status="success",
            )

    def _resolve_tool_calls(self, response, raw_text: str) -> list:
        """Returns native tool calls, falling back to text parsing if absent."""
        tool_calls = response.tool_calls or []

        # Fallback text parsing if tool_calls not parsed natively
        if not tool_calls and "```json" in raw_text:
            parsed = parse_structured(raw_text)
            if isinstance(parsed, ParseFailure):
                logger.warning(
                    "FlashRunner response contained a JSON block that could"
                    f" not be parsed: {parsed.error}"
                )
            elif isinstance(parsed, dict) and "name" in parsed:
                tool_calls = [
                    {
                        "name": parsed["name"],
                        "args": parsed.get("args", {}),
                        "id": str(uuid.uuid4()),
                    }
                ]
        return tool_calls

    # ------------------------------------------------------------------
    # Per-tool-call helpers (act / record / report)
    # ------------------------------------------------------------------

    async def _finalize_task_report(
        self,
        name: str,
        args: dict,
        tc_id: str,
        raw_text: str,
        step_token_usage: dict,
        pre_screenshot_bytes,
        xml_list,
        messages: list[BaseMessage],
    ) -> dict:
        """Records the final step, acknowledges the tool call, and flushes."""
        final_report = args
        if self.ctx.data_engine:
            try:
                if self.ctx.data_engine.current_step_id is None:
                    self.ctx.data_engine.allocate_step_id()
                self.ctx.data_engine.record_step(
                    pre_screenshot_bytes=pre_screenshot_bytes,
                    ui_tree=xml_list,
                    action_taken={"action": "report_task_status", "args": args},
                    operator_raw_thinking=raw_text,
                    last_execution_result={"result": "Task completed with final report."},
                    extra_metadata={"token_usage": step_token_usage},
                )
            except Exception as step_err:
                logger.warning(f"Error recording final step in FlashRunner: {step_err}")
        messages.append(
            ToolMessage(
                tool_call_id=tc_id,
                name=name,
                content=json.dumps({"status": "acknowledged"}),
                status="success",
            )
        )
        if self.summarizer:
            await self.summarizer.flush()
        return final_report

    async def _capture_post_screenshot(self, exec_result, name: str, action_names):
        """Returns the post-action screenshot, capturing a fallback if needed."""
        post_img_bytes = exec_result.screenshot_bytes
        if not post_img_bytes and name in action_names:
            try:
                controller = UnifiedMobileController(self.ctx)
                screen_data = await controller.get_screen_data()
                post_img_bytes = base64.b64decode(screen_data.base64)
                if not exec_result.ui_elements_text:
                    exec_result.ui_elements_text = screen_data.elements
            except Exception as shot_err:
                logger.warning(f"Failed to capture fallback screenshot in FlashRunner: {shot_err}")
        return post_img_bytes

    def _extract_normalized_coordinates(self, name: str, args: dict):
        """Extracts and enriches coordinate metadata for the recorded action."""
        norm_coords = None
        norm_start = None
        norm_end = None
        if name == "swipe":
            kind, target_val, _ = parse_swipe_parameters(args)
            if kind == "coords" and isinstance(target_val, list) and len(target_val) == 4:
                norm_coords = target_val
                norm_start = target_val[:2]
                norm_end = target_val[2:]
            elif kind == "direction" and isinstance(target_val, str):
                g_lower = target_val.lower()
                if "up" in g_lower:
                    norm_coords = [600, 700, 600, 300]
                elif "down" in g_lower:
                    norm_coords = [600, 300, 600, 700]
                elif "left" in g_lower:
                    norm_coords = [750, 500, 250, 500]
                elif "right" in g_lower:
                    norm_coords = [250, 500, 750, 500]
                if norm_coords:
                    norm_start = norm_coords[:2]
                    norm_end = norm_coords[2:]
        elif name in ("click", "tap", "long_press", "input_text"):
            target = args.get("target") or args.get("coordinates")
            if isinstance(target, (list, tuple)) and len(target) == 2:
                norm_coords = list(target)
            elif isinstance(target, str):
                nums = re.findall(r"-?\d+(?:\.\d+)?", target)
                if len(nums) == 2:
                    norm_coords = [int(float(nums[0])), int(float(nums[1]))]
        return norm_coords, norm_start, norm_end

    def _record_action_step(
        self,
        name: str,
        args: dict,
        exec_result,
        raw_text: str,
        step_token_usage: dict,
        pre_screenshot_bytes,
        xml_list,
        post_img_bytes,
        injected: str | None = None,
    ):
        """Records telemetry / step in DataEngine; returns the step id or None."""
        recorded_step_id = None
        try:
            if self.ctx.data_engine.current_step_id is None:
                self.ctx.data_engine.allocate_step_id()

            norm_coords, norm_start, norm_end = self._extract_normalized_coordinates(name, args)

            # Flash records the model's own 0–1000 coordinates verbatim; the
            # explicit space marker keeps every later normalization pass
            # (agent-friendly steps, MCP inspector) a no-op on this record.
            action_dict = {
                "action": name,
                "coordinates": (
                    args.get("target")
                    or args.get("coordinates")
                    or args.get("sequence")
                    or norm_coords
                ),
                COORDINATE_SPACE_KEY: COORDINATE_SPACE_NORMALIZED,
                "args": args,
            }
            if norm_coords:
                action_dict["normalized_coordinates"] = norm_coords
            if norm_start and norm_end:
                action_dict["normalized_start_coordinates"] = norm_start
                action_dict["normalized_end_coordinates"] = norm_end

            # Record-time enrichment computed by the executor from
            # the pre-action frame (target_text / target_class /
            # target_resource_id / target_label_source).
            target_semantics = (exec_result.metadata or {}).get("target_semantics")
            if isinstance(target_semantics, dict):
                action_dict.update(target_semantics)

            succeeded = exec_result.status == "success"
            last_execution_result = {
                "status": "success" if succeeded else "failed",
                "result": exec_result.text_summary,
            }
            if not succeeded:
                last_execution_result["error"] = exec_result.text_summary

            extra_metadata: dict = {"token_usage": step_token_usage}
            if injected:
                # Stamped verbatim on the step it reached: the chunk ledger
                # keeps it as a never-evicted line at every compression level.
                extra_metadata["injected_instruction"] = injected

            recorded_step_id = self.ctx.data_engine.record_step(
                pre_screenshot_bytes=pre_screenshot_bytes,
                post_screenshot_bytes=post_img_bytes,
                ui_tree=(exec_result.ui_elements_text or xml_list),
                action_taken=action_dict,
                operator_raw_thinking=raw_text,
                last_execution_result=last_execution_result,
                extra_metadata=extra_metadata,
            )
            self._notify_history_chunker(recorded_step_id)
        except Exception as step_err:
            logger.warning(f"Error recording step in FlashRunner: {step_err}")
        return recorded_step_id

    def _notify_history_chunker(self, step_id) -> None:
        """Stamps the recorded step for the chunk manager (single segment, no plan)."""
        ledger = getattr(self.ctx, "transcript_ledger", None)
        chunker = getattr(ledger, "chunker", None) if ledger is not None else None
        if chunker is None or step_id is None:
            return
        try:
            chunker.on_step_stamped(str(step_id), None)
        except Exception as e:
            logger.warning(f"History chunker step stamp failed: {e}")

    async def _execute_and_record_action(
        self,
        name: str,
        args: dict,
        tc_id: str,
        state: State,
        messages: list[BaseMessage],
        raw_text: str,
        step_token_usage: dict,
        pre_screenshot_bytes,
        xml_list,
        action_sequence: int,
        turn: _TurnRecord,
        injected: str | None = None,
    ):
        """Executes one tool call, records it, and updates loop state.

        The tool message carries the outcome text only; the post-action
        screenshot and UI list become the next turn's observation tail.
        Returns the (possibly updated) pre_screenshot_bytes, xml_list, and
        action_sequence for the next iteration.
        """
        # Dynamic dispatch set: manifest device actions plus any backend
        # extension tools, so extension steps are recorded like actions.
        action_names = self.executor.action_tool_names
        try:
            exec_result = await self.executor.execute(name, args, tc_id, state)

            post_img_bytes = await self._capture_post_screenshot(exec_result, name, action_names)

            # Record telemetry / step in DataEngine
            recorded_step_id = None
            if self.ctx.data_engine and name in action_names:
                recorded_step_id = self._record_action_step(
                    name,
                    args,
                    exec_result,
                    raw_text,
                    step_token_usage,
                    pre_screenshot_bytes,
                    xml_list,
                    post_img_bytes,
                    injected=injected,
                )

            if name in action_names:
                turn.step_keys.append(str(recorded_step_id) if recorded_step_id else str(tc_id))
                turn.actions.append((name, exec_result.status, exec_result.text_summary))

            # ⚡ Non-blocking dispatch of objective visual transition summarizer
            if self.summarizer and name in action_names:
                action_sequence += 1
                self.summarizer.dispatch(
                    step_number=action_sequence,
                    action_name=name,
                    action_args=args,
                    pre_img_bytes=pre_screenshot_bytes,
                    post_img_bytes=post_img_bytes,
                    exec_outcome=exec_result.text_summary,
                    action_key=str(tc_id),
                    data_engine_step_id=recorded_step_id,
                )

            if post_img_bytes:
                pre_screenshot_bytes = post_img_bytes
            if exec_result.ui_elements_text:
                xml_list = exec_result.ui_elements_text

            # Helper tools may return multimodal blocks (a step screenshot);
            # device actions always report text — their post screenshot is the
            # next observation tail, never a tool-message image. The image
            # carrier follows the model's provider (tool_result_messages).
            raw_blocks = exec_result.raw_result if name not in action_names else None
            content = (
                raw_blocks
                if isinstance(raw_blocks, list) and raw_blocks
                else exec_result.text_summary or f"Action '{name}' completed."
            )
            messages.extend(
                tool_result_messages(
                    tc_id, content, name=name, status=exec_result.status, llm=self._llm
                )
            )

        except Exception as e:
            logger.error(f"Error executing tool {name}: {e}")
            if name in action_names:
                turn.step_keys.append(str(tc_id))
                turn.actions.append((name, "error", f"Error executing tool {name}: {e}"))
            messages.append(
                ToolMessage(
                    tool_call_id=tc_id,
                    name=name,
                    content=f"Error executing tool {name}: {e}",
                    status="error",
                )
            )
        return pre_screenshot_bytes, xml_list, action_sequence

    async def _invoke_model(self, llm, current_tools: list, messages: list[BaseMessage]):
        """Binds the active tools and invokes the model through the LLM gateway."""
        # Bind active tools
        bound_llm = llm.bind_tools(current_tools)

        # Invoke Model. Streaming, live-token UI deltas, classified
        # retries, and pause/resume are all owned by the LLM gateway
        # (acomplete); a typed LLMCallError propagates if it gives up.
        return await invoke_llm_with_timeout_message(
            acomplete(bound_llm, messages), timeout_seconds=10, hard_timeout=180
        )

    async def _process_tool_calls(
        self,
        tool_calls: list,
        state: State,
        messages: list[BaseMessage],
        raw_text: str,
        step_token_usage: dict,
        pre_screenshot_bytes,
        xml_list,
        action_sequence: int,
        turn: _TurnRecord,
        injected: str | None = None,
    ):
        """Dispatches the turn's tool calls.

        Returns (final_report, pre_screenshot_bytes, xml_list, action_sequence);
        final_report is non-None only when 'report_task_status' was called.
        """
        for tc in tool_calls:
            name = tc["name"].split(":")[-1] if ":" in tc["name"] else tc["name"]
            args = tc.get("args") or {}
            tc_id = tc.get("id") or str(uuid.uuid4())
            logger.info(f"Executing Flash tool: {name}({args})")

            if name == "report_task_status":
                final_report = await self._finalize_task_report(
                    name,
                    args,
                    tc_id,
                    raw_text,
                    step_token_usage,
                    pre_screenshot_bytes,
                    xml_list,
                    messages,
                )
                return final_report, pre_screenshot_bytes, xml_list, action_sequence

            pre_screenshot_bytes, xml_list, action_sequence = await self._execute_and_record_action(
                name,
                args,
                tc_id,
                state,
                messages,
                raw_text,
                step_token_usage,
                pre_screenshot_bytes,
                xml_list,
                action_sequence,
                turn,
                injected=injected,
            )
        return None, pre_screenshot_bytes, xml_list, action_sequence

    async def _prepare_conversation(self, state: State, tools_declaration: list):
        """Installs the static prefix and captures the initial device state."""
        ledger = self._build_ledger()
        ledger.set_static_prefix(
            [SystemMessage(content=self._render_system_prompt(tools_declaration))]
        )

        # Capture Initial State (Screenshot + UI Tree)
        shot_path, img_bytes, xml_list = await capture_screenshot_and_parse_ui(
            self.ctx, state, self.controller, skip_settling=False
        )
        state.latest_screenshot = shot_path
        return ledger, img_bytes, xml_list

    @staticmethod
    def _commit_turn(ledger: TranscriptLedger, turn: _TurnRecord | None) -> None:
        """Commits the previous turn once its step ids and outcomes are known."""
        if turn is None:
            return
        ledger.commit_staged(
            step_key=turn.step_keys[0] if turn.step_keys else None,
            validator_result=turn.result(),
            extra_step_keys=turn.step_keys[1:],
        )

    @trace(type="agent", name="FlashRunner")
    async def run(self, state: State) -> dict:
        limit = self.turn_limit
        logger.info(
            f"Starting Artemis Flash reactive loop for goal: {self.goal}"
            f" (turn limit: {limit if limit else 'unlimited'})"
        )

        # 1. Initialize Universal LLM via Service Layer
        llm = self._init_llm()
        self._llm = llm

        tools_declaration = self._get_tools()
        report_only_tools = [t for t in tools_declaration if t.name == "report_task_status"]

        ledger, img_bytes, xml_list = await self._prepare_conversation(state, tools_declaration)

        turns = 0
        action_sequence = 0
        final_report = None
        current_pre_screenshot_bytes = img_bytes
        current_xml_list = xml_list
        previous_turn: _TurnRecord | None = None
        pending_notices: list[str] = []

        while limit is None or turns < limit:
            turns += 1
            logger.info(f"--- Artemis Flash Turn {turns}{f'/{limit}' if limit else ''} ---")

            if self.ctx.data_engine:
                self.ctx.data_engine.allocate_step_id()

            # Commit the previous turn: its step ids and outcomes exist now.
            self._commit_turn(ledger, previous_turn)
            previous_turn = None

            # Check for real-time injected instructions
            injected = await self._read_injected_instruction()

            # Tool restriction on the final turn (bounded loops only)
            is_final = limit is not None and turns == limit
            tail = self._build_tail(
                ledger,
                turns,
                current_pre_screenshot_bytes,
                current_xml_list,
                injected=injected,
                notices=pending_notices,
                is_final=is_final,
            )
            pending_notices = []

            # S + F + A + tail: chunk compression and the scrub edge advance here.
            messages = ledger.render([tail])
            turn_base = len(messages) - 1
            current_tools = report_only_tools if is_final else tools_declaration

            response = await self._invoke_model(llm, current_tools, messages)

            if response is None:
                break

            messages.append(response)

            # Extract thought and tool calls
            raw_text = self._extract_response_text(response)

            # Extract token usage metadata from response
            step_token_usage = self._resolve_token_usage(response, messages, raw_text)

            self._record_llm_trace(step_token_usage, raw_text)

            tool_calls = self._resolve_tool_calls(response, raw_text)

            if not tool_calls:
                logger.info(
                    f"FlashRunner received response without tool calls at turn {turns}:"
                    f" {raw_text[:100]}..."
                )
                if is_final:
                    final_report = {"status": "failed", "explanation": raw_text}
                    break
                pending_notices.append(_NO_TOOL_CALL_NOTICE)
                ledger.stage_turn(messages[turn_base:])
                previous_turn = _TurnRecord()
                continue

            # Process tool calls
            turn = _TurnRecord()
            (
                final_report_from_calls,
                current_pre_screenshot_bytes,
                current_xml_list,
                action_sequence,
            ) = await self._process_tool_calls(
                tool_calls,
                state,
                messages,
                raw_text,
                step_token_usage,
                current_pre_screenshot_bytes,
                current_xml_list,
                action_sequence,
                turn,
                injected=injected,
            )
            ledger.stage_turn(messages[turn_base:])
            previous_turn = turn
            if final_report_from_calls is not None:
                return final_report_from_calls

        if self.summarizer:
            await self.summarizer.flush()

        if final_report is None:
            explanation = (
                "Max turns reached without final status report."
                if limit is not None and turns >= limit
                else "The model returned no response; the reactive loop stopped."
            )
            final_report = {"status": "failed", "explanation": explanation}
        return final_report
