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

"""MCP-backed action executor for the FlashRunner.

Device actions use the in-process MCP session. Argument normalization,
element-index resolution, coordinate descriptions, smart swipes, post-action
observations, state updates, tracing, and helper tools are handled here on the
agent side.

Flash binds the same agent dialect as the Pro Operator: a ``click``,
``long_press`` or ``input_text`` target is either an element index into the
indexed ``--- Visible UI Elements ---`` list (resolved here to the element's
center, its observed text/bounds/id recorded) or a normalized ``[x, y]`` pair
carrying the model's own ``target_description``. The wire only ever sees
coordinates.

Device action status comes from ``ActionResult.ok``; helper tools report failures
through ``ToolFailure`` or an explicit status.
"""

import ast
import hashlib
import json
from pathlib import Path
from typing import Any

from artemis.core.tool_failure import ToolFailure, is_tool_failure
from artemis.context import ArtemisContext
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.data_engine.trace import TraceSpan
from artemis.mcp.action_manifest import OPTIONAL_ACTIONS, REQUIRED_ACTIONS
from artemis.mcp.action_specs import exception_prefix
from artemis.mcp.action_session import ActionSession, get_action_session
from artemis.mcp.action_types import ActionCode, ActionResult
from artemis.mcp.actuators.adb import AdbActuator
from artemis.agents.validator.tool_declarations import (
    ToolExecutionResult,
    normalize_coordinate_target,
)
from artemis.tools.history import HISTORY_TOOL_NAMES, history_tool_by_name
from artemis.tools.tool_wrapper import split_multimodal_result
from artemis.utils.coordinates import (
    compute_smart_swipe_coordinates,
    parse_swipe_parameters,
)
from artemis.utils.logger import get_logger
from artemis.utils.notes import (
    format_list_notes_failure,
    format_list_notes_success,
    format_read_note_failure,
    list_notes_info,
    read_note_content,
)

logger = get_logger(__name__)

__all__ = ["McpActionExecutor"]


#: Non-device tools executed agent-side (never behind the action server).
AGENT_TOOL_NAMES: frozenset[str] = (
    frozenset({"read_note", "list_notes", "ask_explorer", "video_analyzer"}) | HISTORY_TOOL_NAMES
)

#: Actions whose ``target`` is a single point: an element index or an [x, y] pair.
_POINT_TARGET_ACTIONS: frozenset[str] = frozenset({"click", "long_press", "input_text"})


class _ArgError(ValueError):
    """Argument-translation failure whose message is already fully formatted."""


class McpActionExecutor:
    """Routes agent tool calls through the unified action MCP session."""

    def __init__(
        self,
        ctx: ArtemisContext,
        controller: UnifiedMobileController | None = None,
        actuator: AdbActuator | None = None,
        agent_name: str = "validator",
    ):
        self.ctx = ctx
        self.actuator = actuator or getattr(ctx, "actuator", None) or AdbActuator(ctx, controller)
        self.controller = self.actuator.controller
        # Identifies the calling agent / profile to the Explorer tier resolver
        # (``explorer.flash_mode`` vs ``explorer.pro_mode``); the agent itself
        # never sees or chooses the tier.
        self.agent_name = agent_name
        self._session: ActionSession | None = None

    @property
    def action_tool_names(self) -> frozenset[str]:
        """Device actions plus backend extension names -- the dynamic dispatch set."""
        return (REQUIRED_ACTIONS | OPTIONAL_ACTIONS) | {e.name for e in self.actuator.extensions()}

    async def _session_or_start(self) -> ActionSession:
        if self._session is None or not self._session.started:
            self._session = await get_action_session(self.ctx, actuator=self.actuator)
        return self._session

    # --- Public entry ----------------------------------------------------------------

    async def execute(
        self,
        name: str,
        args: dict[str, Any],
        tool_call_id: str,
        state: Any,
        *,
        index_elements: list[dict[str, Any]] | None = None,
    ) -> ToolExecutionResult:
        """Executes the named tool and wraps the outcome for the calling agent.

        ``index_elements`` is the element list an index target (``click(3)``)
        resolves against; ``None`` means ``state.indexed_elements`` at call
        time. A multi-action turn passes the list from the observation the
        model decided on, so its later indices are not resolved against the
        list refreshed by its earlier actions (``state.indexed_elements`` is
        still refreshed after every action: it is what the next observation
        shows).
        """
        raw_name = name.split(":")[-1] if ":" in name else name

        if raw_name in AGENT_TOOL_NAMES:
            return await self._execute_agent_tool(raw_name, name, args, tool_call_id, state)

        if raw_name in self.action_tool_names:
            return await self._execute_device_action(
                raw_name, name, args, tool_call_id, state, index_elements=index_elements
            )

        return ToolExecutionResult(
            tool_call_id=tool_call_id,
            tool_name=name,
            status="error",
            text_summary=f"Error: Tool '{name}' not supported.",
        )

    # --- Device actions --------------------------------------------------------------

    async def _execute_device_action(
        self,
        raw_name: str,
        name: str,
        args: dict[str, Any],
        tool_call_id: str,
        state: Any,
        *,
        index_elements: list[dict[str, Any]] | None = None,
    ) -> ToolExecutionResult:
        span = TraceSpan(name=raw_name, trace_type="action", ctx=self.ctx)
        span.payload = {"args": args}

        message = ""
        res: ActionResult | None = None
        img_bytes: bytes | None = None
        shot_path: str | None = None
        xml_list: str | None = None
        target_semantics: dict[str, Any] = {}
        target_coordinates: list[int] | None = None
        with span:
            try:
                session = await self._session_or_start()
                wire_name, wire_args, finalize, target_semantics = self._translate(
                    raw_name, args, state, index_elements=index_elements
                )
                if raw_name in _POINT_TARGET_ACTIONS:
                    # The normalized point the action was sent to: the recorded
                    # coordinates of an index target (the model named an index,
                    # not a point).
                    target_coordinates = wire_args.get("target")
                extension = raw_name not in (REQUIRED_ACTIONS | OPTIONAL_ACTIONS)

                if extension:
                    raw_result = await session.call_raw(wire_name, wire_args)
                    message = next(
                        (b.text for b in raw_result.content if getattr(b, "type", "") == "text"),
                        f"Extension tool '{raw_name}' completed.",
                    )
                    res = ActionResult.success(raw_name, message)
                else:
                    res = await session.call(wire_name, wire_args)
                    message = finalize(res) if finalize else res.message

                if res.ok or self._observe_despite_failure(raw_name, res):
                    obs = await session.observe(settle_ms=400)
                    if obs.ok:
                        shot_path = obs.screenshot_path
                        xml_list = obs.elements_text
                        if obs.hierarchy_ok:
                            state.indexed_points = [el["center"] for el in obs.elements]
                            state.indexed_elements = obs.elements
                        if shot_path:
                            try:
                                img_bytes = Path(shot_path).read_bytes()
                            except Exception as read_err:
                                logger.warning(f"Failed to read observed screenshot: {read_err}")
            except _ArgError as e:
                res = ActionResult.failure(raw_name, str(e), code=ActionCode.INVALID_ARGS)
                message = str(e)
            except Exception as e:
                message = f"{exception_prefix(raw_name)}: {e}"
                res = ActionResult.failure(raw_name, message, detail=repr(e))

            if shot_path:
                state.latest_screenshot = shot_path

            status = "success" if res.ok else "error"
            post_image_name = None
            if shot_path:
                post_image_name = Path(shot_path).stem
            elif img_bytes:
                post_image_name = hashlib.sha256(img_bytes).hexdigest()
            span.result = {
                "outcome": message,
                "post_image_name": post_image_name,
                "has_xml": bool(xml_list),
                "status": status,
            }
            if status == "error":
                span.status = "failed"
                span.error = message

        return ToolExecutionResult(
            tool_call_id=tool_call_id,
            tool_name=name,
            status=status,
            text_summary=message,
            screenshot_bytes=img_bytes,
            screenshot_path=shot_path,
            ui_elements_text=xml_list,
            metadata={
                "code": res.code.value,
                "normalized_coordinates": res.normalized_coordinates,
                "target_semantics": target_semantics,
                "target_coordinates": target_coordinates,
            },
        )

    @staticmethod
    def _observe_despite_failure(raw_name: str, res: ActionResult) -> bool:
        """Capture the screen after launch failures and wait_for_text timeouts.

        Argument and package lookup errors do not require an observation.
        """
        if raw_name == "manage_app":
            return res.code not in (ActionCode.PACKAGE_NOT_FOUND, ActionCode.INVALID_ARGS)
        if raw_name == "wait_for_text":
            return res.code is ActionCode.TIMEOUT
        return False

    # --- Argument translation --------------------------------------------------------

    def _translate(
        self,
        raw_name: str,
        args: dict[str, Any],
        state: Any,
        *,
        index_elements: list[dict[str, Any]] | None = None,
    ) -> tuple[str, dict[str, Any], Any, dict[str, Any]]:
        """Maps agent-facing args to wire args; returns (name, args, finalize, recorded).

        ``finalize(res)`` adds client-side context to swipe result messages.
        ``recorded`` holds the target semantics in the Pro Operator's record
        format: the observed element fields of an index target, or the validated
        ``target_description`` of a coordinate target. Focused input and
        directional swipes have no target, so they record nothing.
        ``index_elements`` overrides the list index targets resolve against
        (see :meth:`execute`).
        """
        if raw_name == "click":
            target, recorded = self._resolve_target(
                args.get("target") or args.get("coordinates"),
                "click",
                args,
                state,
                index_elements=index_elements,
            )
            return (
                "click",
                {
                    "target": target,
                    "times": args.get("times", 1),
                    "delay_ms": args.get("delay_ms", 100),
                },
                None,
                recorded,
            )

        if raw_name == "long_press":
            target, recorded = self._resolve_target(
                args.get("target") or args.get("coordinates"),
                "long press",
                args,
                state,
                index_elements=index_elements,
            )
            return (
                "long_press",
                {
                    "target": target,
                    "duration_ms": args.get("duration_ms", args.get("duration", 1000)),
                },
                None,
                recorded,
            )

        if raw_name == "input_text":
            raw_target = args.get("target") or args.get("coordinates")
            target = None
            recorded = {}
            if raw_target:
                target, recorded = self._resolve_target(
                    raw_target, "input text", args, state, index_elements=index_elements
                )
            return (
                "input_text",
                {
                    "text": args.get("text", ""),
                    "target": target,
                    "clear_exist": args.get("clear_exist", True),
                },
                None,
                recorded,
            )

        if raw_name == "click_sequence":
            sequence = self._resolve_sequence(args.get("sequence") or [])
            recorded = self._require_descriptions(args, len(sequence))
            return (
                "click_sequence",
                {
                    "sequence": sequence,
                    "delay_ms": args.get("delay_ms", 50),
                },
                None,
                recorded,
            )

        if raw_name == "swipe":
            return self._translate_swipe(args, state, index_elements=index_elements)

        if raw_name == "press_key":
            return "press_key", {"key": args.get("key", "BACK")}, None, {}

        if raw_name == "manage_app":
            return (
                "manage_app",
                {
                    "action": args.get("action", "launch"),
                    "app_name": args.get("app_name", ""),
                },
                None,
                {},
            )

        if raw_name == "wait_for_delay":
            return "wait_for_delay", {"time_in_ms": args.get("time_in_ms", 1000)}, None, {}

        if raw_name == "wait_for_text":
            return (
                "wait_for_text",
                {
                    "text": args.get("text", ""),
                    "wait_state": args.get("wait_state") or "appear",
                    "timeout_ms": args.get("timeout_ms", 5000),
                },
                None,
                {},
            )

        # Backend extension: pass the arguments straight through.
        return raw_name, dict(args), None, {}

    def _screen_size(self) -> tuple[int, int]:
        """The device resolution the indexed element list was built against."""
        device = getattr(self.ctx, "device", None)
        width = getattr(device, "device_width", None) if device else None
        height = getattr(device, "device_height", None) if device else None
        return (
            width if isinstance(width, int) and width > 0 else 1080,
            height if isinstance(height, int) and height > 0 else 2400,
        )

    def _resolve_target(
        self,
        raw: Any,
        label: str,
        args: dict[str, Any],
        state: Any,
        *,
        index_elements: list[dict[str, Any]] | None = None,
    ) -> tuple[list[int], dict[str, Any]]:
        """Resolves a point target to ``([x, y] normalized, recorded semantics)``.

        An element index carries observed semantics (text, bounds, id, class
        straight from the indexed list); a coordinate pair carries only the
        model's own ``target_description``. The two never mix (Pro parity).
        """
        target = normalize_coordinate_target(raw)
        if isinstance(target, bool):
            target = None
        if isinstance(target, (int, float)):
            return self._resolve_index(int(target), label, state, index_elements=index_elements)
        if isinstance(target, (list, tuple)) and len(target) == 2:
            recorded = self._require_description(args, label)
            return [int(target[0]), int(target[1])], recorded
        raise _ArgError(
            f"Error during {label}: Invalid target format: {raw!r}. Use an element"
            " index from the Visible UI Elements list (e.g. 3) or normalized [x, y]"
            " coordinates with a target_description."
        )

    def _resolve_index(
        self,
        index: int,
        label: str,
        state: Any,
        *,
        index_elements: list[dict[str, Any]] | None = None,
    ) -> tuple[list[int], dict[str, Any]]:
        """Maps an element index onto the element's center (normalized 0-1000).

        The recorded fields are what the Operator records for an index target,
        with bounds normalized like every other Flash coordinate. The index is
        resolved against ``index_elements`` when given, else against the
        state's current list.
        """
        elements = self._index_elements(state, index_elements)
        if not 1 <= index <= len(elements):
            hint = (
                " The list is empty on this screen."
                if not elements
                else f" Active index range is 1 to {len(elements)}."
            )
            raise _ArgError(
                f"Error during {label}: Invalid target index {index}.{hint} Use an index"
                " shown in the Visible UI Elements list, or normalized [x, y]"
                " coordinates with a target_description (ask_explorer can locate an"
                " element that is visible but not listed)."
            )
        element = elements[index - 1]
        width, height = self._screen_size()

        def _norm_x(v: Any) -> int:
            return int(max(0, min(1000, round(float(v) * 1000 / max(1, width)))))

        def _norm_y(v: Any) -> int:
            return int(max(0, min(1000, round(float(v) * 1000 / max(1, height)))))

        center = element.get("center")
        if not (isinstance(center, (list, tuple)) and len(center) == 2):
            raise _ArgError(f"Error during {label}: element [{index}] has no usable center.")
        bounds = element.get("bounds")
        norm_bounds = None
        if isinstance(bounds, (list, tuple)) and len(bounds) == 4:
            norm_bounds = [
                _norm_x(bounds[0]),
                _norm_y(bounds[1]),
                _norm_x(bounds[2]),
                _norm_y(bounds[3]),
            ]
        recorded = {
            "target_text": element.get("text"),
            "target_bounds": norm_bounds,
            "target_resource_id": element.get("resource_id"),
            "target_class": element.get("class"),
        }
        return [_norm_x(center[0]), _norm_y(center[1])], recorded

    @staticmethod
    def _require_description(args: dict[str, Any], label: str) -> dict[str, Any]:
        """Validate and trim the coordinate target description for the action record."""
        description = args.get("target_description")
        if not (isinstance(description, str) and description.strip()):
            raise _ArgError(
                f"Error during {label}: 'target_description' is required (what the"
                " target is, in a few words)."
            )
        return {"target_description": description.strip()}

    @staticmethod
    def _require_descriptions(args: dict[str, Any], count: int) -> dict[str, Any]:
        """``click_sequence``: one description per sequence entry, in order.

        Returns the cleaned statements as the recorded ``target_descriptions`` field.
        """
        descriptions = args.get("target_descriptions")
        if isinstance(descriptions, str):
            descriptions = [descriptions]
        if (
            not isinstance(descriptions, (list, tuple))
            or len(descriptions) != count
            or not all(isinstance(d, str) and d.strip() for d in descriptions)
        ):
            raise _ArgError(
                "Error during click sequence: 'target_descriptions' is required, one"
                f" non-empty entry per sequence entry ({count} expected)."
            )
        return {"target_descriptions": [d.strip() for d in descriptions]}

    @staticmethod
    def _resolve_sequence(sequence: Any) -> list[list[int]]:
        """Normalize click_sequence entries to coordinate pairs; reject element indices."""
        if isinstance(sequence, str):
            sequence_str = sequence.strip()
            try:
                sequence = json.loads(sequence_str)
            except ValueError:
                try:
                    sequence = ast.literal_eval(sequence_str)
                except (ValueError, SyntaxError, TypeError, RecursionError):
                    pass

        if not isinstance(sequence, (list, tuple)):
            raise _ArgError(f"Error during click sequence: Invalid sequence format: {sequence}")

        resolved: list[list[int]] = []
        for position, raw_target in enumerate(sequence, start=1):
            target = normalize_coordinate_target(raw_target)
            if isinstance(target, (int, float)) and not isinstance(target, bool):
                raise _ArgError(
                    f"Error during click sequence: entry {position} ({raw_target!r}) is an"
                    " element index; click_sequence takes normalized [x, y] coordinate"
                    " pairs only. Use the element's coordinates from the element list,"
                    " or ask_explorer to locate it."
                )
            if isinstance(target, (list, tuple)) and len(target) == 2:
                nx, ny = int(target[0]), int(target[1])
            else:
                raise _ArgError(f"Error during click sequence: Invalid target format: {raw_target}")
            resolved.append([nx, ny])
        return resolved

    @staticmethod
    def _index_elements(
        state: Any, index_elements: list[dict[str, Any]] | None
    ) -> list[dict[str, Any]]:
        """The element list index targets resolve against (``None`` -> state)."""
        if index_elements is not None:
            return index_elements
        return getattr(state, "indexed_elements", None) or []

    def _translate_swipe(
        self,
        args: dict[str, Any],
        state: Any,
        *,
        index_elements: list[dict[str, Any]] | None = None,
    ) -> tuple[str, dict[str, Any], Any, dict[str, Any]]:
        width, height = self._screen_size()
        default_duration = args.get("duration", 400)
        kind, target, final_duration = parse_swipe_parameters(
            dict(args), default_duration=default_duration
        )

        if kind == "direction":
            dir_name = target
            x1, y1, x2, y2, smart_dur = compute_smart_swipe_coordinates(
                direction=target,
                target=args.get("target"),
                indexed_elements=self._index_elements(state, index_elements) if state else None,
                ui_hierarchy=getattr(state, "latest_ui_hierarchy", None) if state else None,
                width=width,
                height=height,
                duration=final_duration,
            )

            def _norm(v: float, size: int) -> int:
                return int(max(0, min(1000, round(v * 1000 / max(1, size)))))

            def finalize(res: ActionResult) -> str:
                if not res.ok:
                    return f"Error swiping {dir_name}: {res.detail}"
                return f"Swiped {dir_name}."

            return (
                "swipe",
                {
                    "start": [_norm(x1, width), _norm(y1, height)],
                    "end": [_norm(x2, width), _norm(y2, height)],
                    "duration_ms": smart_dur,
                },
                finalize,
                {},
            )

        if kind == "coords":
            x1, y1, x2, y2 = target
            recorded = self._require_description(args, "swipe")

            def finalize(res: ActionResult) -> str:
                if not res.ok:
                    return f"Error dragging: {res.detail}"
                return f"Swiped from [{x1}, {y1}] to [{x2}, {y2}] (normalized)."

            return (
                "swipe",
                {
                    "start": [int(x1), int(y1)],
                    "end": [int(x2), int(y2)],
                    "duration_ms": final_duration,
                },
                finalize,
                recorded,
            )

        raise _ArgError(f"Error during swipe: Invalid direction: {args}")

    # --- Non-device tools (stay agent-side) ------------------------------------------

    async def _execute_agent_tool(
        self,
        raw_name: str,
        name: str,
        args: dict[str, Any],
        tool_call_id: str,
        state: Any,
    ) -> ToolExecutionResult:
        span = TraceSpan(name=raw_name, trace_type="tool", ctx=self.ctx)
        span.payload = {"args": args}
        ok: bool | None = None
        blocks: list[dict[str, Any]] | None = None
        with span:
            if raw_name == "read_note":
                text = await self._read_note(
                    args.get("key", ""), args.get("start_line"), args.get("end_line")
                )
            elif raw_name == "list_notes":
                text = await self._list_notes()
            elif raw_name == "video_analyzer":
                text, ok = await self._video_analyzer(
                    args.get("time_description") or args.get("TimeDescription") or "",
                    args.get("purpose") or args.get("Purpose") or "",
                )
            elif raw_name in HISTORY_TOOL_NAMES:
                text, blocks = await self._history_tool(raw_name, args)
            else:
                text, ok = await self._ask_explorer(
                    # ``task_description`` is the pre-contract argument name
                    # kept for old MCP clients.
                    args.get("query") or args.get("task_description") or "",
                    args.get("context_feedback"),
                    state,
                )
            span.result = {"outcome": text}
            if ok is False:
                span.status = "failed"
                span.error = text

        # The Explorer and the video analyzer report their status explicitly;
        # note and history texts carry it structurally (a ``ToolFailure``), so a
        # history lookup's "no match" answer is an answer, not an error.
        if ok is None:
            ok = not is_tool_failure(text)
        return ToolExecutionResult(
            tool_call_id=tool_call_id,
            tool_name=name,
            status="success" if ok else "error",
            text_summary=text,
            # Content blocks (a step screenshot) ride along for callers that
            # forward multimodal tool results; the text summary stays complete.
            raw_result=blocks,
        )

    async def _read_note(self, key: str, start_line: int | None, end_line: int | None) -> str:
        try:
            base_dir = self.ctx.data_engine.base_dir if self.ctx.data_engine else None
            if not base_dir:
                return format_read_note_failure(key, "DataEngine not initialized.")
            return read_note_content(base_dir, key, start_line, end_line)
        except Exception as e:
            return format_read_note_failure(key, str(e))

    async def _list_notes(self) -> str:
        try:
            base_dir = self.ctx.data_engine.base_dir if self.ctx.data_engine else None
            if not base_dir:
                return format_list_notes_failure("DataEngine not initialized.")
            return format_list_notes_success(list_notes_info(base_dir))
        except Exception as e:
            return format_list_notes_failure(str(e))

    async def _history_tool(
        self, raw_name: str, args: dict[str, Any]
    ) -> tuple[str, list[dict] | None]:
        """Runs one of the shared history tools (the same instances the Pro
        agents bind), dispatched generically by name.

        Returns the text answer and, for a screenshot, the multimodal content
        blocks the tool produced.
        """
        tool = history_tool_by_name(raw_name)
        if tool is None:
            return ToolFailure(f"Error: Tool '{raw_name}' not supported."), None
        accepted = {k: v for k, v in args.items() if k in tool.args_schema.model_fields}
        try:
            result = await tool.execute(ctx=self.ctx, **accepted)
        except Exception as e:
            logger.error(f"Error running {raw_name}: {e}")
            return ToolFailure(f"{raw_name} failed: {e}"), None
        text, images = split_multimodal_result(result)
        if images:
            return text or "Screenshot attached.", result
        return text, None

    async def _video_analyzer(self, time_description: str, purpose: str) -> tuple[str, bool]:
        """Runs the video-analyzing subagent over the session recording.

        Same subagent the Pro operator's ``video_analyzer`` LangChain tool
        delegates to; imported lazily because the video stack is heavy.
        """
        from artemis.agents.video_analyzer.video_analyzer import VideoAnalyzer

        try:
            outcome, status = await VideoAnalyzer(self.ctx).run(time_description, purpose)
        except Exception as e:
            logger.error(f"Error running video analyzer: {e}")
            return f"Error running video analyzer: {e}", False
        if status in ("failed", "error"):
            return f"Video analysis failed: {outcome}", False
        return outcome, True

    async def _ask_explorer(
        self, query: str, context_feedback: str | None, state: Any
    ) -> tuple[str, bool]:
        """Runs the Explorer pipeline for this executor's agent; returns ``(text, ok)``.

        ``ok`` is False only when the Explorer run itself failed: a clean
        "not found" is a successful answer the agent has to reason about.
        """
        # Imported lazily: the explorer stack is heavy and recursive (it spawns an LLM
        # sub-agent), which is also why it must never live behind the action server.
        from artemis.tools.explorer_tool import locate, register_candidates, render_text

        try:
            outcome = await locate(
                self.ctx, state, query, context_feedback or "", agent_name=self.agent_name
            )
            registered = register_candidates(self.ctx, state, outcome)
            # Candidates join ``state.indexed_elements``, and this executor's
            # ``click`` resolves an index against that list (Pro parity), so the
            # answer teaches the index syntax next to the coordinates.
            return render_text(query, outcome, registered, index_targets=True), not outcome.error
        except Exception as e:
            return f"Error executing ask_explorer: {e}", False
