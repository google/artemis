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

Executes autonomous reactive mobile workflows across OpenAI GPT-4o/o3,
Anthropic Claude and OpenRouter endpoints.

Conversation shape (shared with the Pro operator, history redesign §3.2):
the prompt is built every turn from a session :class:`TranscriptLedger` —
a byte-stable system prefix, the committed earlier turns (append-only; old UI
lists stripped at depth 1, screenshots resolved to visual summaries at depth
K, long spans chunk-compressed), and a fresh tail carrying the current
observation under a ``# CURRENT OBSERVATION [T+mm:ss]`` header. Actions run
synchronously and their tool messages already carry the outcome; only a turn
whose action failed ends with an ``--- Action Execution Result (T+mm:ss) ---``
message (it carries the error). Every timestamp the model sees is a
session-relative offset — the same clock the video analyzer uses for the
session recording.

Per-turn notices on the tail (the reasoning reminder after a silent turn, the
no-tool-call nudge, the final-turn warning, the user-guidance wrapper) are
marked ephemeral: the ledger's scrub edge deletes them once the message leaves
the recent window, so they never reach the frozen history or a chunk capsule.
The verbatim body of a user instruction is the exception: it stays in the
active window until its turn is chunked, whose user lines then carry it on.
"""

import asyncio
import base64
from collections import deque
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

from artemis.agents.flash.summarizer import VisualStepSummarizer, build_focus_context
from artemis.agents.operator.prompts import REASONING_REMINDER, UserGuidance, render_user_guidance
from artemis.agents.validator.tool_declarations import (
    ASK_EXPLORER_TOOL,
    CLICK_SEQUENCE_TOOL,
    LIST_NOTES_TOOL,
    READ_NOTE_TOOL,
    REMEMBER_INSIGHT_TOOL,
    REPORT_TASK_STATUS_TOOL,
    ToolExecutionResult,
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
from artemis.mcp.observation import observe
from artemis.memory.transcript import PRO_UI_LIST_MARKER, TranscriptLedger, mark_ephemeral
from artemis.mcp.screen_diff import NO_SCREEN_CHANGES, render_diff_block
from artemis.services.llm import (
    acomplete,
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
from PIL import Image

from artemis.utils.slider_value import (
    ADJUSTABLE_CLASSES,
    measure_fill_percent,
    point_at_share,
    share_from_text,
    snap_press_point,
)
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

_NO_TOOL_CALL_NOTICE = (
    "You did not call any tools last turn. Please make progress by calling an"
    " action tool or 'report_task_status'."
)

_FINAL_TURN_WARNING = "[WARNING] This is your final turn; only 'report_task_status' is available."

_REPEAT_OBSERVE = 2
_REPEAT_BLOCK = 3
_REPEAT_STOP = 6
_CAPTURE_FAILURE_LIMIT = 2
_NO_CHANGE_LIMIT = 2
_CLICK_TOOL_NAMES = frozenset({"click", "click_sequence"})
_PRESS_KEY_TOOL_NAMES = frozenset({"press_key"})
_NO_PROGRESS_NOTICE = (
    "Your last {count} actions left the screen unchanged. `click` is withdrawn for this "
    "turn because these coordinates hold no way forward. Change the screen first with "
    "`swipe`, `press_key` or `manage_app`, then look at what appeared."
)
_REPEAT_TEMPERATURE = 0.7
_REPEAT_WINDOW = 12

_GOAL_PERCENT = re.compile(r"(\d{1,3})\s*%")
_GOAL_MAX_WORDS = ("最大", "最高", "maximum", "max")
_GOAL_MIN_WORDS = ("最小", "最低", "minimum", "min")
_PERCENT_IN_LINE = re.compile(r"(\d{1,3})\s*%")


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

        ``None`` unless an action failed: Flash executes synchronously and each
        action's tool message already carries its outcome, so a result message
        would only repeat it. A failure is worth its own message because it
        carries the error the turn ended on.
        """
        for name, status, text in self.actions:
            if status != "success":
                # Executor messages name the action themselves ("Error
                # executing click: ..."); prefix only the ones that don't.
                return {"status": "failed", "error": text if name in text else f"{name}: {text}"}
        return None


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
        # Publish the service on the composition root (ctx.step_memory) so
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
        # Flash has no tool that writes notes and its prompt never teaches
        # them, so the Validator set's note readers are left out here.
        tools = [
            t
            for t in VALIDATOR_TOOLS_DECLARATION
            if t is not READ_NOTE_TOOL and t is not LIST_NOTES_TOOL
        ]
        tools.insert(1, CLICK_SEQUENCE_TOOL)
        tools.append(ASK_EXPLORER_TOOL)
        tools.append(REMEMBER_INSIGHT_TOOL)
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
            image_scrub_depth_relaxed=getattr(cfg, "image_scrub_depth_relaxed", None),
            context_budget_tokens=getattr(cfg, "context_budget_tokens", None),
            start_ratio=getattr(cfg, "start_ratio", None),
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
        return get_llm(self.ctx, name="operator")

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

    async def _read_injected_instruction(
        self, offset_label: str | None = None
    ) -> tuple[str | None, UserGuidance | None]:
        """Consumes this turn's injected instruction: ``(instruction, guidance)``.

        ``instruction`` is the user's text verbatim — what the step record,
        the chunk ledger and the visual-transition lens receive (the same
        shape Pro stamps). ``guidance`` is the shared user-guidance pair
        (:func:`render_user_guidance`, the Pro rendering without a plan to
        edit): the ephemeral ``--- User Guidance ---`` wrapper and the
        persistent verbatim body, both riding on the observation tail.
        ``offset_label`` stamps the body with the session offset. Both are
        None when nothing was injected.
        """
        if not (self.ctx.data_engine and self.ctx.data_engine.base_dir):
            return None, None
        try:
            injected_payload = await asyncio.to_thread(
                _check_injected_instruction_file,
                str(self.ctx.data_engine.base_dir),
            )
        except (OSError, ValueError, AttributeError) as e:
            logger.warning(f"Failed to check injected instruction in FlashRunner: {e}")
            return None, None
        if not (injected_payload and injected_payload.get("instruction")):
            return None, None
        instruction = str(injected_payload["instruction"])
        guidance = render_user_guidance(
            instruction,
            release_loop=bool(injected_payload.get("release_loop")),
            has_plan=False,
            offset_label=offset_label,
        )
        return instruction, guidance

    def _build_tail(
        self,
        ledger: TranscriptLedger,
        turns: int,
        img_bytes,
        xml_list,
        xml_diff: str | None = None,
        *,
        injected: UserGuidance | None = None,
        notices: list[str] | None = None,
        is_final: bool = False,
        previous_turn_silent: bool = False,
    ) -> HumanMessage:
        """Builds this turn's observation tail (Pro observation shape).

        Header, screenshot and UI list carry the same markers as the Pro
        operator's tail so the ledger's scrub edge treats both alike. The
        objective is stated once, in the system prompt; the tail never repeats
        it. Every per-turn notice (nudges, the user-guidance wrapper, the
        final-turn warning, the reasoning reminder after a silent turn) is
        marked ephemeral so it leaves the transcript with the recent window.
        The verbatim body of an injected instruction is the one exception: it
        is a regular block, so a standing instruction stays in the active
        window until the turn is chunked (the chunk's user lines carry it on).
        """
        # ``turns`` is kept for call-site symmetry: the tail has the same shape
        # on every turn.
        blocks: list[dict] = [
            {"type": "text", "text": f"# CURRENT OBSERVATION [{ledger.elapsed_label()}]"}
        ]
        if img_bytes:
            img_b64 = base64.b64encode(img_bytes).decode("utf-8")
            blocks.append({"type": "text", "text": "--- Current Screenshot ---"})
            blocks.append(
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}
            )
        ephemeral: list[int] = []
        if xml_list:
            diff_block = render_diff_block(xml_diff)
            if diff_block is not None:
                ephemeral.append(len(blocks))
                blocks.append({"type": "text", "text": diff_block})
            blocks.append({"type": "text", "text": f"{PRO_UI_LIST_MARKER}\n{xml_list}"})
        # (text, ephemeral) in tail order. Notices ride last: a run whose learned
        # route was offered 11 times at the head of the tail still pressed `back`
        # first, so the instruction sits where the model reads before acting.
        per_turn: list[tuple[str, bool]] = []
        if injected:
            # The wrapper is per-turn; the verbatim body right after it is a
            # regular block that stays until the turn is chunked.
            per_turn.append((injected.wrapper, True))
            per_turn.append((injected.body, False))
        if is_final:
            per_turn.append((_FINAL_TURN_WARNING, True))
        if previous_turn_silent:
            per_turn.append((REASONING_REMINDER, True))
        per_turn.extend((text, True) for text in (notices or []))
        for text, is_ephemeral in per_turn:
            if is_ephemeral:
                ephemeral.append(len(blocks))
            blocks.append({"type": "text", "text": text})
        tail = HumanMessage(content=blocks)
        if ephemeral:
            mark_ephemeral(tail, ephemeral)
        return tail

    def _extract_response_text(self, response) -> str:
        """Extracts the natural-language thought text from the model response."""
        raw_text = response.content if isinstance(response.content, str) else ""
        if isinstance(response.content, list):
            raw_text = "".join(
                b.get("text", "") for b in response.content if isinstance(b, dict) and "text" in b
            )
        return raw_text

    def _extract_response_thinking(self, response) -> str:
        """Extracts the model's native thinking blocks (thought summaries), if any.

        Providers running with ``include_thoughts`` return them as ``thinking``
        content blocks next to the ``text`` blocks; they are recorded on the step
        as ``operator_native_thinking`` and never fed back as reply text.
        """
        if not isinstance(response.content, list):
            return ""
        parts = [
            str(b.get("thinking") or "")
            for b in response.content
            if isinstance(b, dict) and b.get("type") == "thinking"
        ]
        return "\n".join(p for p in parts if p.strip())

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

    def _resolve_token_usage(
        self, response, messages: list[BaseMessage], raw_text: str
    ) -> tuple[dict, bool]:
        """Token usage for the call and whether the provider measured it.

        Falls back to an estimate when the response carries no usage
        metadata; the flag lets the caller keep an estimate out of the
        ledger's chars-per-token calibration (an estimate is itself derived
        from character counts, so calibrating on it would be circular).
        """
        step_token_usage = self._token_usage_from_response(response)
        if step_token_usage and step_token_usage.get("total_tokens", 0) > 0:
            return step_token_usage, True
        return self._estimate_token_usage(messages, raw_text), False

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

    def _report_evidence_problem(self, args: dict, xml_list: str | None) -> str | None:
        """Rejects a completion report that does not quote the value it claims.

        Measured on the brightness task: the model swiped once, stopped at 77% of
        the maximum, and reported completion, three times out of six runs. The
        check compares the quoted line with the observation, so it needs no model
        of what the goal means beyond the number it names.
        """
        if args.get("status") != "completed":
            return None
        goal = self.goal.lower()
        target = _GOAL_PERCENT.search(goal)
        wants_max = any(word in goal for word in _GOAL_MAX_WORDS)
        wants_min = any(word in goal for word in _GOAL_MIN_WORDS)
        if not (target or wants_max or wants_min):
            return None
        observation = xml_list or ""
        evidence = str(args.get("evidence") or "").strip()
        if not evidence:
            return (
                "Not accepted: this task names a value, so a completed report must quote the "
                "value in 'evidence', copied from the current UI element list. Report again "
                "with the value you see, or keep working if it does not reach the target."
            )
        if evidence not in observation:
            return (
                "Not accepted: the text you quoted does not appear in the current UI element "
                "list, so it is not what the screen shows. Quote a line from the list as "
                "'evidence', or keep working."
            )
        seen = _PERCENT_IN_LINE.search(evidence)
        if seen is None:
            return None
        shown = int(seen.group(1))
        if target is not None:
            want = int(target.group(1))
            if abs(shown - want) > 3:
                return (
                    f"Not accepted: the goal asks for {want}% and the screen shows {shown}%. "
                    f"Keep working, and report only once the value is within 3 of {want}."
                )
        if wants_max and shown < 95:
            return (
                f"Not accepted: the goal asks for the maximum and the screen shows {shown}%. "
                "Keep working until it is at least 95%."
            )
        if wants_min and shown > 5:
            return (
                f"Not accepted: the goal asks for the minimum and the screen shows {shown}%. "
                "Keep working until it is at most 5%."
            )
        return None

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
        native_text: str | None = None,
    ) -> dict:
        """Records the final step, acknowledges the tool call, and flushes."""
        problem = self._report_evidence_problem(args, xml_list)
        if problem is not None and getattr(self, "_report_refusals", 0) < 2:
            self._report_refusals = getattr(self, "_report_refusals", 0) + 1
            messages.append(
                ToolMessage(
                    tool_call_id=tc_id,
                    name=name,
                    content=problem,
                    status="error",
                )
            )
            return None
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
                    operator_native_thinking=native_text or None,
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

    def _device_insight_notices(self, xml_list: str | None) -> list[str]:
        """Surfaces a route stored by an earlier run when the screen it starts on is here."""
        if not xml_list:
            return []
        serial = getattr(getattr(self.ctx, "device", None), "device_id", None)
        storage = getattr(getattr(self.ctx, "data_engine", None), "storage", None)
        if not serial or storage is None:
            return []
        try:
            rows = storage.device_insights(serial)
        except Exception as error:
            logger.debug(f"Device insights unavailable: {error}")
            return []
        hits = [row for row in rows if row["screen_key"] and row["screen_key"] in xml_list]
        notices = [
            f"Proven route on this screen, use it: {row['insight']} "
            f"(worked {row['success_count']}x on this device)"
            for row in hits[:5]
        ]
        if notices:
            logger.info(f"Offering {len(notices)} learned route(s) for {serial}")
        return notices

    def _note_screen_change(self, diff: str) -> None:
        """Counts actions that left the screen identical, and clears on any change.

        A panel with no exit control is where the model guessed four coordinates in a
        row on 2026-11-11 (298, 107, 203, 486 at y=850 in the control centre).
        """
        unchanged = not diff.strip() or diff.strip() == NO_SCREEN_CHANGES
        if unchanged:
            self._no_change_streak = getattr(self, "_no_change_streak", 0) + 1
        else:
            self._no_change_streak = 0

    def _tools_without_click(self, tools: list, route_offered: bool = False) -> list:
        """Withdraws actions a stalled run cannot use.

        A stored route withdraws `press_key`: on 2026-11-11 a run that had the
        control-centre route in hand pressed HOME first, which does nothing on
        that screen, and spent the next five steps clicking the stale screen.
        """
        blocked: set[str] = set()
        if getattr(self, "_click_withdrawn", False) or (
            getattr(self, "_no_change_streak", 0) >= _NO_CHANGE_LIMIT
        ):
            blocked |= _CLICK_TOOL_NAMES
        if route_offered:
            blocked |= _PRESS_KEY_TOOL_NAMES
        if not blocked:
            return tools
        return [t for t in tools if getattr(t, "name", "") not in blocked]

    def _note_capture_failure(self, message: str) -> None:
        """Stops the run when the screen cannot be read, instead of letting the model guess.

        On 2026-11-11 a run whose adb link dropped kept acting on a stale frame and
        ended `failed` with "6 identical actions", naming the model as the cause
        when the capture had been failing for every one of those steps.
        """
        self._capture_failures = getattr(self, "_capture_failures", 0) + 1
        if self._capture_failures >= _CAPTURE_FAILURE_LIMIT:
            self._device_stop_reason = (
                f"Stopped after {self._capture_failures} failed screen captures, so no step "
                f"could be judged: {message}"
            )

    async def _capture_post_screenshot(
        self, exec_result, name: str, action_names, state: State | None = None
    ):
        """Returns the post-action screenshot, capturing a fallback if needed.

        When a device action produced no observation (a failed action, an
        unknown tool, ...) the screen is observed through the very same
        pipeline the executor's success path uses (:func:`observe`), so the
        next observation tail carries the indexed minimal UI list and
        ``state`` gets the same refreshed screenshot path and indexed
        elements. Nothing is captured for non-action tools.
        """
        post_img_bytes = exec_result.screenshot_bytes
        if post_img_bytes or name not in action_names:
            return post_img_bytes
        try:
            obs, screenshot_bytes = await observe(self.ctx, self.controller, settle_ms=0)
        except Exception as shot_err:
            self._note_capture_failure(str(shot_err))
            logger.warning(f"Failed to capture fallback screenshot in FlashRunner: {shot_err}")
            return post_img_bytes
        if not obs.ok:
            self._note_capture_failure(obs.message or "screen capture failed")
            logger.warning(f"Failed to capture fallback screenshot in FlashRunner: {obs.message}")
            return post_img_bytes
        self._capture_failures = 0
        post_img_bytes = screenshot_bytes
        if not exec_result.ui_elements_text:
            exec_result.ui_elements_text = obs.elements_text
        if exec_result.ui_elements_diff is None:
            exec_result.ui_elements_diff = obs.diff_text
        if not exec_result.screenshot_path and obs.screenshot_path:
            exec_result.screenshot_path = obs.screenshot_path
        if state is not None:
            if obs.hierarchy_ok:
                state.indexed_points = [el["center"] for el in obs.elements]
                state.indexed_elements = obs.elements
            if obs.screenshot_path:
                state.latest_screenshot = obs.screenshot_path
        return post_img_bytes

    def _extract_normalized_coordinates(self, name: str, args: dict, resolved=None):
        """Extracts and enriches coordinate metadata for the recorded action.

        ``resolved`` is the normalized point the executor sent a point action
        to; it is the coordinate of record when the model addressed an element
        by index (the arguments then carry no point at all).
        """
        norm_coords = None
        norm_start = None
        norm_end = None
        if name in ("click", "tap", "long_press", "input_text") and (
            isinstance(resolved, (list, tuple)) and len(resolved) == 2
        ):
            return [int(resolved[0]), int(resolved[1])], None, None
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

    def _build_action_record(self, name: str, args: dict, exec_result) -> dict:
        """The action as the step record and the visual lens see it.

        Flash records the model's own 0–1000 coordinates verbatim (an index
        target records the point the executor resolved it to); the explicit
        space marker keeps every later normalization pass (agent-friendly
        steps, MCP inspector) a no-op on this record. The tool arguments ride
        along under ``args``, and the target semantics the executor validated
        (observed element fields for an index, the model's own
        ``target_description`` for a point) land top level in the shape Pro
        records.
        """
        metadata = getattr(exec_result, "metadata", None) or {}
        resolved = metadata.get("target_coordinates")
        norm_coords, norm_start, norm_end = self._extract_normalized_coordinates(
            name, args, resolved=resolved
        )
        given = args.get("target") or args.get("coordinates") or args.get("sequence")
        if isinstance(given, (int, float)) and not isinstance(given, bool):
            given = None  # An element index is not a coordinate.
        action_dict = {
            "action": name,
            "coordinates": given or norm_coords,
            COORDINATE_SPACE_KEY: COORDINATE_SPACE_NORMALIZED,
            "args": args,
        }
        if norm_coords:
            action_dict["normalized_coordinates"] = norm_coords
        if norm_start and norm_end:
            action_dict["normalized_start_coordinates"] = norm_start
            action_dict["normalized_end_coordinates"] = norm_end
        action_dict.update(metadata.get("target_semantics") or {})
        return action_dict

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
        native_text: str | None = None,
        action_dict: dict | None = None,
    ):
        """Records telemetry / step in DataEngine; returns the step id or None."""
        recorded_step_id = None
        try:
            if self.ctx.data_engine.current_step_id is None:
                self.ctx.data_engine.allocate_step_id()

            if action_dict is None:
                action_dict = self._build_action_record(name, args, exec_result)

            succeeded = exec_result.status == "success"
            last_execution_result = {
                "status": "dispatched" if succeeded else "failed",
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
                ui_tree=xml_list,
                action_taken=action_dict,
                operator_raw_thinking=raw_text,
                operator_native_thinking=native_text or None,
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

    def _snap_slider_press(
        self, args: dict, index_elements: list[dict] | None, goal: str | None, state=None
    ) -> dict:
        """Puts a drag's press point on the thumb of the control it lands on.

        The release point carries the caller's intent and is left alone. A model
        reads the thumb's position off the screenshot and lands about ten points
        short of it; on 2026-11-11 that turned one slider drag into ten steps.
        """
        start = args.get("start")
        if not (isinstance(start, (list, tuple)) and len(start) == 2):
            share_only = share_from_text(goal or "")
            width_only, height_only = self.executor._screen_size()
            controls_only = self._measurable_controls(index_elements, state)
            if share_only is None or len(controls_only) != 1:
                return args
            bounds, percent = controls_only[0]
            if abs(percent - share_only) <= 3:
                return args
            logger.info(
                "Slider calibration replaced a directional swipe with a drag "
                f"{percent}% -> {share_only}%"
            )
            return {
                **args,
                "direction": None,
                "start": point_at_share(bounds, percent, width_only, height_only),
                "end": point_at_share(bounds, share_only, width_only, height_only),
            }
        width, height = self.executor._screen_size()
        controls = self._measurable_controls(index_elements, state)
        if not controls:
            logger.warning(
                "Slider calibration skipped: no measurable control under this swipe"
            )
            return args
        snapped = snap_press_point(list(start), controls, width, height)
        corrected = dict(args)
        if snapped != list(start):
            corrected["start"] = snapped
        share = share_from_text(goal or "")
        if share is not None:
            target = self._control_under(snapped, controls, width, height)
            if target is not None:
                corrected["end"] = point_at_share(target, share, width, height)
        return corrected

    def _measurable_controls(self, index_elements, state):
        """Reads each adjustable control's thumb straight from the current frame."""
        image = None
        path = getattr(state, "latest_screenshot", None)
        if path:
            try:
                image = Image.open(str(path)).convert("RGB")
            except Exception as e:
                logger.warning(f"Slider calibration could not open {path}: {e}")
        controls = []
        for element in index_elements or []:
            short_class = str(element.get("class") or "").rsplit(".", 1)[-1]
            bounds = element.get("bounds")
            if short_class not in ADJUSTABLE_CLASSES:
                continue
            if not (isinstance(bounds, (list, tuple)) and len(bounds) == 4):
                continue
            percent = element.get("value_percent")
            if percent is None and image is not None:
                percent = measure_fill_percent(image, tuple(bounds))
            if percent is not None:
                controls.append((tuple(bounds), percent))
        return controls

    @staticmethod
    def _control_under(start, controls, width, height):
        px = round(start[0] * width / 1000)
        py = round(start[1] * height / 1000)
        for bounds, _percent in controls:
            left, top, right, bottom = bounds
            if left <= px <= right and top <= py <= bottom:
                return bounds
        return None

    def _repeat_row_key(self, name: str, args: dict) -> tuple[object, ...]:
        coords = args.get("coordinates") or args.get("target")
        if isinstance(coords, (list, tuple)) and len(coords) == 2:
            try:
                return (name, "cell", int(float(coords[0]) // 20), int(float(coords[1]) // 20))
            except (TypeError, ValueError):
                return (name, str(coords))
        start = args.get("start")
        end = args.get("end")
        if isinstance(start, (list, tuple)) and isinstance(end, (list, tuple)):
            return (name, "drag", tuple(start[:2]), tuple(end[:2]))
        return (name, str(args.get("target")), str(args.get("direction")))

    def _repetition_verdict(
        self, name: str, args: dict, action_names: object
    ) -> tuple[str | None, str | None, bool]:
        """Runs the doom-loop ladder: observe at 2, block at 3, stop at 6.

        OpenRouter's doom-loop detection and the H3 entropy-driven curiosity
        pattern both break a stalled loop with a ladder rather than one
        threshold: a cue before the block, and a stop so a model that ignores
        both cannot spend the whole step budget on one control. Warning alone
        did not work here: runs of 40 and 53 identical actions still happened.

        The count is taken over a window of recent actions, not against the
        previous one only, because a model that alternates two actions (a click
        then a swipe, then the click again) resets a consecutive count on every
        step and loops forever (measured: 32 steps, no intervention).
        """
        key = self._repeat_row_key(name, args)
        window = getattr(self, "_repeat_window", None)
        if window is None:
            window = deque(maxlen=_REPEAT_WINDOW)
            self._repeat_window = window
        window.append(key)
        self._repeat_key = key
        self._repeat_count = window.count(key)
        if self._repeat_count <= 1:
            self._click_withdrawn = False
        if name not in action_names:
            return None, None, False
        repeats = self._repeat_count
        if repeats >= _REPEAT_STOP:
            return None, None, True
        if repeats >= _REPEAT_BLOCK:
            self._click_withdrawn = True
            return (
                f"Not executed: this is identical action number {repeats} in the same place "
                "and the control's value has not changed, so repeating it cannot move the "
                "value. Read the value in the current observation. If it already reaches what "
                'the goal asks for, call report_task_status(status="completed"). If it does '
                "not, change the action: different coordinates, a different tool, or another "
                "route. Reporting completion while the value is short is a wrong answer.",
                None,
                False,
            )
        if repeats >= _REPEAT_OBSERVE:
            return (
                None,
                "You have now done this same action twice in the same place and the value "
                "did not change. Do not do it a third time. Read the value in the observation. "
                "If it reaches what the goal asks for, call "
                'report_task_status(status="completed"). Otherwise change the coordinates, '
                "change the tool, or take another route. Reporting completion while the value "
                "is short is a wrong answer.",
                False,
            )
        return None, None, False

    def _sampling_temperature(self) -> float | None:
        """Raises the temperature only while the loop is stalled.

        H3 pairs its detection with a change in sampling, and a fixed
        temperature of 0.0 replays the same action for the same observation.
        Every other step wants the most likely coordinates, so the baseline
        stays 0.0 and the escalated value lasts only until the model changes
        the action.
        """
        escalated = getattr(self, "_repeat_count", 0) >= _REPEAT_BLOCK
        return _REPEAT_TEMPERATURE if escalated else None

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
        xml_diff: str | None,
        action_sequence: int,
        turn: _TurnRecord,
        injected: str | None = None,
        native_text: str | None = None,
        index_elements: list[dict] | None = None,
    ):
        """Executes one tool call, records it, and updates loop state.

        The tool message carries the outcome text only; the post-action
        screenshot and UI list become the next turn's observation tail.
        ``index_elements`` is the turn's element snapshot index targets
        resolve against (see :meth:`_process_tool_calls`); ``None`` resolves
        against the state's current list. Returns the (possibly updated)
        pre_screenshot_bytes, xml_list, xml_diff, and action_sequence for the
        next iteration.
        """
        # Dynamic dispatch set: manifest device actions plus any backend
        # extension tools, so extension steps are recorded like actions.
        action_names = self.executor.action_tool_names
        try:
            refuse, observe, stop = self._repetition_verdict(name, args, action_names)
            if stop:
                self._repeat_stop_reason = (
                    f"Stopped after {self._repeat_count} identical actions in one place: the "
                    "control's value never changed and the model repeated the action instead "
                    "of changing it."
                )
                exec_result = ToolExecutionResult(
                    tool_call_id=tc_id,
                    tool_name=name,
                    status="error",
                    text_summary=self._repeat_stop_reason,
                )
            elif refuse is not None:
                exec_result = ToolExecutionResult(
                    tool_call_id=tc_id,
                    tool_name=name,
                    status="error",
                    text_summary=refuse,
                )
            else:
                if name == "swipe" and isinstance(args, dict):
                    args = self._snap_slider_press(args, index_elements, self.goal, state)
                exec_result = await self.executor.execute(
                    name, args, tc_id, state, index_elements=index_elements
                )
                if observe is not None:
                    exec_result.text_summary = f"{exec_result.text_summary or ''}\n{observe}"

            post_img_bytes = await self._capture_post_screenshot(
                exec_result, name, action_names, state
            )

            # One record shape for the DataEngine step and the visual lens.
            action_dict = (
                self._build_action_record(name, args, exec_result) if name in action_names else None
            )

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
                    native_text=native_text,
                    action_dict=action_dict,
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
                    # The recorded action minus its verb: the same shape the Pro
                    # summarizer hands the lens, so the phrase renders alike.
                    action_args={k: v for k, v in action_dict.items() if k != "action"},
                    pre_img_bytes=pre_screenshot_bytes,
                    post_img_bytes=post_img_bytes,
                    exec_outcome=exec_result.text_summary,
                    action_key=str(tc_id),
                    data_engine_step_id=recorded_step_id,
                    # The model's own reasoning for this turn steers the lens's
                    # attention; the target it named rides on ``args``.
                    focus=build_focus_context(
                        intent=raw_text,
                        goal=self.goal,
                        injected_instruction=injected,
                    ),
                )

            if post_img_bytes:
                pre_screenshot_bytes = post_img_bytes
            if exec_result.ui_elements_text:
                xml_list = exec_result.ui_elements_text
            if exec_result.ui_elements_diff is not None:
                xml_diff = exec_result.ui_elements_diff
                self._note_screen_change(xml_diff)

            # Helper tools may return multimodal blocks (a step screenshot);
            # device actions always report text — their post screenshot is the
            # next observation tail, never a tool-message image. The image
            # carrier follows the model's provider (tool_result_messages).
            raw_blocks = exec_result.raw_result if name not in action_names else None
            content = (
                raw_blocks
                if isinstance(raw_blocks, list) and raw_blocks
                else exec_result.text_summary or f"Action '{name}' dispatched."
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
        return pre_screenshot_bytes, xml_list, xml_diff, action_sequence

    async def _invoke_model(
        self,
        llm,
        current_tools: list,
        messages: list[BaseMessage],
        temperature: float | None = None,
    ):
        """Binds the active tools and invokes the model through the LLM gateway."""
        active_llm = (
            get_llm(self.ctx, name="operator", temperature=temperature)
            if temperature is not None
            else llm
        )
        bound_llm = active_llm.bind_tools(current_tools)

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
        xml_diff: str | None,
        action_sequence: int,
        turn: _TurnRecord,
        injected: str | None = None,
        native_text: str | None = None,
    ):
        """Dispatches the turn's tool calls.

        Every index target of the turn resolves against the element list the
        model decided on: the list of the observation this turn was built
        from, snapshotted here before the first action. Each action still
        refreshes ``state.indexed_elements`` (that refreshed list is what the
        next observation shows), so without the snapshot ``click(5)`` after
        ``click(3)`` would be resolved against the screen after the first
        click.

        The snapshot is the very list object the observation left on the
        state (not a copy), and observations replace the state's list rather
        than mutating it, so ``ask_explorer`` candidates registered before
        any action land in the snapshot by construction. When a helper tool
        appends candidates after an action has already replaced the state's
        list, the explorer numbered them against that replaced list, so the
        snapshot is re-taken from it: indices the explorer just handed out
        resolve correctly, at the cost of the pre-action numbering (mixing
        the two in one turn is the model's error, not a supported case).

        Returns (final_report, pre_screenshot_bytes, xml_list, xml_diff, action_sequence);
        final_report is non-None only when 'report_task_status' was called.
        """
        action_names = self.executor.action_tool_names
        index_elements = self._turn_index_snapshot(state)
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
                    native_text=native_text,
                )
                if final_report is None:
                    continue
                return final_report, pre_screenshot_bytes, xml_list, xml_diff, action_sequence

            elements_before = getattr(state, "indexed_elements", None)
            before_len = len(elements_before) if isinstance(elements_before, list) else 0
            (
                pre_screenshot_bytes,
                xml_list,
                xml_diff,
                action_sequence,
            ) = await self._execute_and_record_action(
                name,
                args,
                tc_id,
                state,
                messages,
                raw_text,
                step_token_usage,
                pre_screenshot_bytes,
                xml_list,
                xml_diff,
                action_sequence,
                turn,
                injected=injected,
                native_text=native_text,
                index_elements=index_elements,
            )
            if getattr(self, "_repeat_stop_reason", None) or getattr(
                self, "_device_stop_reason", None
            ):
                return (
                    {
                        "status": "failed",
                        "explanation": getattr(self, "_repeat_stop_reason", None)
                        or self._device_stop_reason,
                    },
                    pre_screenshot_bytes,
                    xml_list,
                    xml_diff,
                    action_sequence,
                )
            if name not in action_names:
                index_elements = self._extend_index_snapshot(state, index_elements, before_len)
        return None, pre_screenshot_bytes, xml_list, xml_diff, action_sequence

    @staticmethod
    def _turn_index_snapshot(state: State) -> list[dict]:
        """The element list this turn's index targets resolve against.

        The state's list object itself, so in-place appends (explorer
        candidates) before any action join the snapshot; an empty list is
        installed on the state when there is none, for the same reason.
        """
        elements = getattr(state, "indexed_elements", None)
        if not isinstance(elements, list):
            elements = []
            try:
                state.indexed_elements = elements
            except (AttributeError, TypeError, ValueError):
                pass
        return elements

    @staticmethod
    def _extend_index_snapshot(state: State, snapshot: list[dict], before_len: int) -> list[dict]:
        """Re-takes the snapshot after a helper tool appended candidates to a
        list that is no longer the snapshot (an action replaced it first).

        Appends into the snapshot itself need nothing: the explorer numbered
        them against the snapshot. Only growth of a different list object
        moves the snapshot, since that is the list the new numbers refer to.
        """
        current = getattr(state, "indexed_elements", None)
        if not isinstance(current, list) or current is snapshot:
            return snapshot
        if len(current) > before_len:
            return current
        return snapshot

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
        current_xml_diff: str | None = None
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

            # Check for real-time injected instructions: the verbatim text
            # is stamped on the step, the guidance pair goes to the tail.
            injected, injected_guidance = await self._read_injected_instruction(
                ledger.elapsed_label()
            )

            # Tool restriction on the final turn (bounded loops only)
            is_final = limit is not None and turns == limit
            tail = self._build_tail(
                ledger,
                turns,
                current_pre_screenshot_bytes,
                current_xml_list,
                current_xml_diff,
                injected=injected_guidance,
                notices=pending_notices,
                is_final=is_final,
                # The ledger judged the turn it just committed: a bare tool
                # call (or thinking only) earns a one-line reminder, no bounce.
                previous_turn_silent=bool(getattr(ledger, "last_turn_silent", False)),
            )
            pending_notices = []
            learned_routes = self._device_insight_notices(current_xml_list)
            pending_notices.extend(learned_routes)
            if getattr(self, "_no_change_streak", 0) >= _NO_CHANGE_LIMIT:
                pending_notices.append(_NO_PROGRESS_NOTICE.format(count=self._no_change_streak))

            # S + F + A + tail: chunk compression and the scrub edge advance here.
            messages = ledger.render([tail])
            turn_base = len(messages) - 1
            current_tools = (
                report_only_tools
                if is_final
                else self._tools_without_click(
                    tools_declaration, route_offered=bool(learned_routes)
                )
            )

            response = await self._invoke_model(
                llm, current_tools, messages, temperature=self._sampling_temperature()
            )

            if response is None:
                break

            messages.append(response)

            # Extract thought and tool calls
            raw_text = self._extract_response_text(response)
            native_text = self._extract_response_thinking(response)

            # Extract token usage metadata from response
            step_token_usage, measured = self._resolve_token_usage(response, messages, raw_text)
            # The ledger's compaction thresholds read the runner's own prompt
            # size, never the session-wide meter (which any agent overwrites).
            # The prompt as sent (``messages`` minus the just-appended reply)
            # rides along so the ledger can calibrate its chars-per-token
            # ratio — only from a provider measurement, never from the
            # estimate (same as the Pro operator).
            ledger.record_prompt_tokens(
                step_token_usage.get("prompt_tokens"),
                messages=messages[:-1] if measured else None,
            )

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
                current_xml_diff,
                action_sequence,
            ) = await self._process_tool_calls(
                tool_calls,
                state,
                messages,
                raw_text,
                step_token_usage,
                current_pre_screenshot_bytes,
                current_xml_list,
                current_xml_diff,
                action_sequence,
                turn,
                injected=injected,
                native_text=native_text,
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
