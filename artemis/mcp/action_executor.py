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

"""MCP-backed action executor for FlashRunner and FailureAnalyzer.

Same public contract as the legacy ``MobileActionExecutor`` --
``execute(name, args, tool_call_id, state) -> ToolExecutionResult`` -- but execution
routes through the in-process action MCP session, and dispatch is table-driven so a
backend's extension tools are callable without touching this file.

Kept agent-side (by design, not omission): argument normalization, element-index
resolution against LangGraph ``State``, smart-swipe resolution, act-then-observe
capture and ``State`` write-back, tracing (a ``ContextVar`` cannot cross the server's
task boundary -- see the plan's R2), and the non-device tools (``read_note``,
``list_notes``, ``ask_explorer``).

Status is carried by ``ActionResult.ok`` -- the historical substring sniffing on
``"Error"``/``"Failed"`` is gone, so typing the literal text "Failed" into a field no
longer marks the step as failed, and a successful tap with a failed screenshot is no
longer misreported as an error.
"""

import ast
import hashlib
import json
from pathlib import Path
from typing import Any

from artemis.config import resolve_explorer_version
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


class _ArgError(ValueError):
    """Argument-translation failure whose message is already fully formatted."""


class McpActionExecutor:
    """Routes agent tool calls through the unified action MCP session."""

    def __init__(
        self,
        ctx: ArtemisContext,
        controller: UnifiedMobileController | None = None,
        actuator: AdbActuator | None = None,
    ):
        self.ctx = ctx
        self.actuator = actuator or getattr(ctx, "actuator", None) or AdbActuator(ctx, controller)
        self.controller = self.actuator.controller
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
    ) -> ToolExecutionResult:
        """Executes the named tool and wraps the outcome for the calling agent."""
        raw_name = name.split(":")[-1] if ":" in name else name

        if raw_name in ("read_note", "list_notes", "ask_explorer"):
            return await self._execute_agent_tool(raw_name, name, args, tool_call_id, state)

        if raw_name in self.action_tool_names:
            return await self._execute_device_action(raw_name, name, args, tool_call_id, state)

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
    ) -> ToolExecutionResult:
        span = TraceSpan(name=raw_name, trace_type="action", ctx=self.ctx)
        span.payload = {"args": args}

        message = ""
        res: ActionResult | None = None
        img_bytes: bytes | None = None
        shot_path: str | None = None
        xml_list: str | None = None

        with span:
            try:
                session = await self._session_or_start()
                wire_name, wire_args, finalize = self._translate(raw_name, args, state)
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
            },
        )

    @staticmethod
    def _observe_despite_failure(raw_name: str, res: ActionResult) -> bool:
        """Failures that still observe the screen, mirroring the legacy executor.

        A failed app launch and a wait_for_text timeout captured the screen; argument
        and package errors returned without one.
        """
        if raw_name == "manage_app":
            return res.code not in (ActionCode.PACKAGE_NOT_FOUND, ActionCode.INVALID_ARGS)
        if raw_name == "wait_for_text":
            return res.code is ActionCode.TIMEOUT
        return False

    # --- Argument translation --------------------------------------------------------

    def _translate(
        self, raw_name: str, args: dict[str, Any], state: Any
    ) -> tuple[str, dict[str, Any], Any]:
        """Maps agent-facing args to wire args; returns (name, args, finalize).

        ``finalize(res)`` post-processes the ActionResult message where the historical
        wording depended on client-side context (direction swipes).
        """
        if raw_name == "click":
            target = self._require_pair(
                args.get("target") or args.get("coordinates") or [], "click"
            )
            return (
                "click",
                {
                    "target": list(target),
                    "times": args.get("times", 1),
                    "delay_ms": args.get("delay_ms", 100),
                },
                None,
            )

        if raw_name == "long_press":
            target = self._require_pair(
                args.get("target") or args.get("coordinates") or [], "long press"
            )
            return (
                "long_press",
                {
                    "target": list(target),
                    "duration_ms": args.get("duration_ms", args.get("duration", 1000)),
                },
                None,
            )

        if raw_name == "input_text":
            raw_target = args.get("target") or args.get("coordinates")
            target = None
            if raw_target:
                target = list(self._require_pair(raw_target, "input text"))
            return (
                "input_text",
                {
                    "text": args.get("text", ""),
                    "target": target,
                    "clear_exist": args.get("clear_exist", True),
                },
                None,
            )

        if raw_name == "click_sequence":
            return (
                "click_sequence",
                {
                    "sequence": self._resolve_sequence(args.get("sequence") or [], state),
                    "delay_ms": args.get("delay_ms", 50),
                },
                None,
            )

        if raw_name == "swipe":
            return self._translate_swipe(args, state)

        if raw_name == "press_key":
            return "press_key", {"key": args.get("key", "BACK")}, None

        if raw_name == "manage_app":
            return (
                "manage_app",
                {
                    "action": args.get("action", "launch"),
                    "app_name": args.get("app_name", ""),
                },
                None,
            )

        if raw_name == "wait_for_delay":
            return "wait_for_delay", {"time_in_ms": args.get("time_in_ms", 1000)}, None

        if raw_name == "wait_for_text":
            return (
                "wait_for_text",
                {
                    "text": args.get("text", ""),
                    "wait_state": args.get("wait_state") or "appear",
                    "timeout_ms": args.get("timeout_ms", 5000),
                },
                None,
            )

        # Backend extension: pass the arguments straight through.
        return raw_name, dict(args), None

    @staticmethod
    def _require_pair(raw: Any, label: str) -> tuple[int, int]:
        target = normalize_coordinate_target(raw)
        if not isinstance(target, (list, tuple)) or len(target) != 2:
            prefix = {
                "click": "Error during click",
                "long press": "Error during long press",
                "input text": "Error during input text",
            }[label]
            raise _ArgError(f"{prefix}: Invalid target format: {raw}")
        return int(target[0]), int(target[1])

    def _resolve_sequence(self, sequence: Any, state: Any) -> list[list[int]]:
        """Resolves click_sequence entries (indices or pairs) to normalized pairs."""
        if isinstance(sequence, str):
            sequence_str = sequence.strip()
            try:
                sequence = json.loads(sequence_str)
            except Exception:
                try:
                    sequence = ast.literal_eval(sequence_str)
                except Exception:
                    pass

        if not isinstance(sequence, (list, tuple)):
            raise _ArgError(f"Error during click sequence: Invalid sequence format: {sequence}")

        width = getattr(self.ctx.device, "device_width", 1080) if self.ctx.device else 1080
        height = getattr(self.ctx.device, "device_height", 2400) if self.ctx.device else 2400

        resolved: list[list[int]] = []
        for raw_target in sequence:
            target = normalize_coordinate_target(raw_target)
            if isinstance(target, int):
                idx = target
                points = getattr(state, "indexed_points", []) or []
                if not 1 <= idx <= len(points):
                    raise _ArgError(
                        "Error during click sequence: Index"
                        f" {idx} is out of range (available: {len(points)})"
                    )
                px, py = int(points[idx - 1][0]), int(points[idx - 1][1])
                nx = int(max(0, min(1000, round(px * 1000 / max(1, width)))))
                ny = int(max(0, min(1000, round(py * 1000 / max(1, height)))))
            elif isinstance(target, (list, tuple)) and len(target) == 2:
                nx, ny = int(target[0]), int(target[1])
            else:
                raise _ArgError(f"Error during click sequence: Invalid target format: {raw_target}")
            resolved.append([nx, ny])
        return resolved

    def _translate_swipe(self, args: dict[str, Any], state: Any) -> tuple[str, dict[str, Any], Any]:
        width = getattr(self.ctx.device, "device_width", 1080) if self.ctx.device else 1080
        height = getattr(self.ctx.device, "device_height", 2400) if self.ctx.device else 2400
        default_duration = args.get("duration", 400)
        kind, target, final_duration = parse_swipe_parameters(
            dict(args), default_duration=default_duration
        )

        if kind == "direction":
            dir_name = target
            x1, y1, x2, y2, smart_dur = compute_smart_swipe_coordinates(
                direction=target,
                target=args.get("target"),
                indexed_elements=getattr(state, "indexed_elements", None) if state else None,
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
                return f"Swipe completed successfully. Swiped {dir_name}."

            return (
                "swipe",
                {
                    "start": [_norm(x1, width), _norm(y1, height)],
                    "end": [_norm(x2, width), _norm(y2, height)],
                    "duration_ms": smart_dur,
                },
                finalize,
            )

        if kind == "coords":
            x1, y1, x2, y2 = target

            def finalize(res: ActionResult) -> str:
                if not res.ok:
                    return f"Error dragging: {res.detail}"
                return f"Swipe completed successfully. Swiped from [{x1}, {y1}] to [{x2}, {y2}]."

            return (
                "swipe",
                {
                    "start": [int(x1), int(y1)],
                    "end": [int(x2), int(y2)],
                    "duration_ms": final_duration,
                },
                finalize,
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
        with span:
            if raw_name == "read_note":
                text = await self._read_note(
                    args.get("key", ""), args.get("start_line"), args.get("end_line")
                )
            elif raw_name == "list_notes":
                text = await self._list_notes()
            else:
                text = await self._ask_explorer(
                    args.get("query") or args.get("task_description") or "",
                    args.get("context_feedback"),
                    state,
                )
            span.result = {"outcome": text}

        # These texts come from our own failure formatters, so the historical
        # "Error" marker check is reliable here (unlike free-form device output).
        return ToolExecutionResult(
            tool_call_id=tool_call_id,
            tool_name=name,
            status="error" if "Error" in (text or "") else "success",
            text_summary=text,
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

    async def _ask_explorer(self, query: str, context_feedback: str | None, state: Any) -> str:
        # Imported lazily: the explorer stack is heavy and recursive (it spawns an LLM
        # sub-agent), which is also why it must never live behind the action server.
        from artemis.tools.explorer_tool import _run_explorer_logic

        try:
            active_version = resolve_explorer_version(
                self.ctx,
                explicit_version=None,
                agent_or_profile_name="validator",
            )
            result = await _run_explorer_logic(
                self.ctx,
                state,
                query,
                context_feedback or "",
                version=active_version,
            )
            if isinstance(result, list):
                texts = [
                    item["text"]
                    for item in result
                    if isinstance(item, dict) and item.get("type") == "text"
                ]
                return "\n".join(texts) if texts else str(result)
            return str(result)
        except Exception as e:
            return f"Error executing ask_explorer: {e}"
