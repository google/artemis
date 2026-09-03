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

"""Universal Tool Declarations, Execution Results, and Dispatcher for Artemis."""

import ast
import base64
import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from google.genai import types as genai_types
from langchain_core.messages import ToolMessage
from pydantic import BaseModel, Field

from artemis.agents.explorer.constants import (
    ASK_EXPLORER_CONTEXT_FEEDBACK_DESCRIPTION,
    ASK_EXPLORER_DESCRIPTION,
    ASK_EXPLORER_QUERY_DESCRIPTION,
    ASK_EXPLORER_TOOL_NAME,
)
from artemis.context import ArtemisContext
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.core.tool_declaration import ToolDeclaration
from artemis.data_engine.trace import trace
from artemis.mcp.action_specs import tool_declaration
from artemis.mcp.action_types import ActionCode
from artemis.mcp.actuators.adb import AdbActuator
from artemis.mcp.observation import observe
from artemis.tools.explorer_tool import locate, register_candidates, render_text
from artemis.graph.state import State
from artemis.utils.coordinates import (
    compute_smart_swipe_coordinates,
    parse_swipe_parameters,
)
from artemis.utils.logger import get_logger
from artemis.utils.notes import (
    LIST_NOTES_DOCSTRING,
    READ_NOTE_ARG_KEY_DESC,
    READ_NOTE_DOCSTRING,
    format_list_notes_failure,
    format_list_notes_success,
    format_read_note_failure,
    list_notes_info,
    read_note_content,
)

logger = get_logger(__name__)

ACTION_TOOL_NAMES = {
    "click",
    "click_sequence",
    "long_press",
    "input_text",
    "swipe",
    "press_key",
    "manage_app",
    "wait_for_delay",
    "wait_for_text",
}


class ToolExecutionResult(BaseModel):
    """Standardized result produced by executing a mobile automation or utility tool."""

    tool_call_id: str
    tool_name: str
    status: Literal["success", "error", "cancelled"]
    text_summary: str
    screenshot_bytes: bytes | None = None
    screenshot_path: str | None = None
    ui_elements_text: str | None = None
    raw_result: Any = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_langchain_tool_message(self) -> ToolMessage:
        """Converts execution result into a standardized multimodal LangChain ToolMessage."""
        content_blocks: list[dict[str, Any]] = [
            {"type": "text", "text": self.text_summary or f"Action '{self.tool_name}' completed."}
        ]
        if self.ui_elements_text:
            content_blocks.append(
                {
                    "type": "text",
                    "text": f"--- UI Element List ---\n{self.ui_elements_text}",
                }
            )
        if self.screenshot_bytes:
            b64_img = base64.b64encode(self.screenshot_bytes).decode("utf-8")
            content_blocks.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"},
                }
            )
        return ToolMessage(
            tool_call_id=self.tool_call_id,
            name=self.tool_name,
            content=content_blocks,
            status=self.status,
        )


def prune_intermediate_screenshots(messages: list[Any]) -> None:
    """Prunes heavy binary screenshot blocks from intermediate observation messages.

    Keeps the latest screenshot across the conversation history to preserve context budget.
    Supports both LangChain BaseMessage and google.genai.types.Content formats.
    """
    if not messages:
        return

    # Check for google.genai.types.Content objects
    if hasattr(messages[0], "parts"):
        user_indices_with_images = []
        for idx in range(1, len(messages)):
            content = messages[idx]
            if getattr(content, "role", None) == "user" and getattr(content, "parts", None):
                for part in content.parts:
                    if getattr(part, "inline_data", None) is not None:
                        user_indices_with_images.append(idx)
                        break

        if len(user_indices_with_images) > 1:
            indices_to_prune = user_indices_with_images[:-1]
            for idx in indices_to_prune:
                content = messages[idx]
                new_parts = []
                for part in content.parts:
                    if getattr(part, "inline_data", None) is not None:
                        try:
                            new_parts.append(
                                genai_types.Part.from_text(
                                    text=(
                                        "[Screenshot of intermediate step omitted for performance]"
                                    )
                                )
                            )
                        except Exception as exc:
                            logger.debug(
                                "Could not build placeholder text part for pruned screenshot;"
                                " dropping it: %s",
                                exc,
                                exc_info=True,
                            )
                    else:
                        new_parts.append(part)
                content.parts = new_parts
        return

    # LangChain BaseMessage format
    last_img_msg_idx = -1
    for idx in range(len(messages) - 1, -1, -1):
        msg = messages[idx]
        msg_content = getattr(msg, "content", None)
        if isinstance(msg_content, list):
            has_image = any(
                isinstance(block, dict) and block.get("type") in ("image_url", "image")
                for block in msg_content
            )
            if has_image:
                last_img_msg_idx = idx
                break

    for idx in range(len(messages)):
        if idx == last_img_msg_idx:
            continue
        msg = messages[idx]
        msg_content = getattr(msg, "content", None)
        if isinstance(msg_content, list):
            msg.content = [
                b
                for b in msg_content
                if not (isinstance(b, dict) and b.get("type") in ("image_url", "image"))
            ]


async def capture_screenshot_and_parse_ui(
    ctx: ArtemisContext,
    state: State,
    controller: UnifiedMobileController,
    skip_settling: bool = False,
) -> tuple[str | None, bytes | None, str | None]:
    """Captures screenshot and parses fused XML tree after optional screen settling delay.

    Thin adapter over :func:`artemis.mcp.observation.observe` that adds the LangGraph
    ``State`` write-back (indexed points/elements) the agents rely on.
    """
    obs, screenshot_bytes = await observe(ctx, controller, settle_ms=0 if skip_settling else 400)
    if not obs.ok:
        return None, None, None

    if obs.hierarchy_ok:
        state.indexed_points = [el["center"] for el in obs.elements]
        state.indexed_elements = obs.elements

    return obs.screenshot_path, screenshot_bytes, obs.elements_text


def normalize_coordinate_target(target: Any) -> int | list[int] | Any:
    """Normalizes coordinate targets or index references to standard types.

    Supports:
    - Integer index: e.g. 1 or "1" -> 1
    - Coordinate list: e.g. [500, 280] or ["500", "280"] -> [500, 280]
    - Serialized coordinate string: e.g. "[500, 280]" or "500, 280" -> [500, 280]
    """
    if isinstance(target, int):
        return target

    if isinstance(target, (list, tuple)):
        if len(target) == 2:
            try:
                return [int(target[0]), int(target[1])]
            except (ValueError, TypeError):
                pass
        elif len(target) == 1:
            return normalize_coordinate_target(target[0])

    if isinstance(target, str):
        cleaned = target.strip()
        if cleaned.isdigit() or (cleaned.startswith("-") and cleaned[1:].isdigit()):
            return int(cleaned)
        try:
            parsed = json.loads(cleaned)
            return normalize_coordinate_target(parsed)
        except (json.JSONDecodeError, TypeError):
            pass
        if "," in cleaned:
            parts = [p.strip().strip("[]() ") for p in cleaned.split(",")]
            if len(parts) == 2:
                try:
                    return [int(parts[0]), int(parts[1])]
                except (ValueError, TypeError):
                    pass

    return target


def serialize_mobile_action_result(result: Any) -> dict[str, Any]:
    """Serializes MobileActionExecutor execution results into a clean structured dict.

    Extracts the post-action screenshot hash from the captured image to eliminate guesswork.
    """
    if not isinstance(result, tuple) or len(result) < 3:
        return {"outcome": str(result) if result is not None else ""}
    outcome, img_bytes, shot_path, *rest = result
    xml_list = rest[0] if rest else None

    post_image_name = None
    if shot_path:
        post_image_name = Path(shot_path).stem
    elif img_bytes:
        post_image_name = hashlib.sha256(img_bytes).hexdigest()

    is_err = bool(outcome and ("Error" in outcome or "Failed" in outcome))
    return {
        "outcome": outcome,
        "post_image_name": post_image_name,
        "has_xml": bool(xml_list),
        "status": "error" if is_err else "success",
    }


def serialize_explorer_result(result: Any) -> dict[str, Any]:
    """Stores an ``(text, ok)`` Explorer answer as a flat trace payload."""
    if isinstance(result, tuple) and len(result) == 2:
        text, ok = result
        return {"outcome": text, "status": "success" if ok else "error"}
    return {"outcome": str(result) if result is not None else ""}


class MobileActionExecutor:
    """Adapts LLM tool calls onto an actuator backend and captures observations.

    Device-call bodies live in the actuator (``artemis/mcp/actuators/``); this class
    keeps the agent-side responsibilities: argument normalization, index resolution
    against LangGraph ``State``, act-then-observe screenshot capture, tracing, and the
    legacy 4-tuple result contract.
    """

    def __init__(
        self,
        ctx: ArtemisContext,
        controller: UnifiedMobileController | None = None,
        actuator: "AdbActuator | None" = None,
        agent_name: str = "validator",
    ):
        self.ctx = ctx
        self.actuator = actuator or AdbActuator(ctx, controller)
        self.controller = self.actuator.controller
        # Identifies the calling agent to the Explorer tier resolver: the tier
        # is a user setting keyed by profile / agent, never chosen by the agent.
        self.agent_name = agent_name

    @trace(type="action", name="click", serializer=serialize_mobile_action_result)
    async def exec_click(
        self,
        target: list[int] | Any,
        state: State,
        times: int = 1,
        delay_ms: int = 100,
    ) -> tuple[str, bytes | None, str | None, str | None]:
        try:
            target = normalize_coordinate_target(target)
            if not isinstance(target, (list, tuple)) or len(target) != 2:
                raise ValueError(f"Invalid target format: {target}")
            nx, ny = target
            res = await self.actuator.click(int(nx), int(ny), times=times, delay_ms=delay_ms)
            outcome = res.message

            img_bytes, shot_path, xml_list = None, None, None
            if res.ok:
                shot_path, img_bytes, xml_list = await capture_screenshot_and_parse_ui(
                    self.ctx, state, self.controller
                )
        except Exception as e:
            outcome = f"Error during click: {e}"
            img_bytes, shot_path, xml_list = None, None, None

        return outcome, img_bytes, shot_path, xml_list

    @trace(type="action", name="click_sequence", serializer=serialize_mobile_action_result)
    async def exec_click_sequence(
        self,
        sequence: list[int | list[int] | str] | str,
        state: State,
        delay_ms: int = 50,
    ) -> tuple[str, bytes | None, str | None, str | None]:
        width = getattr(self.ctx.device, "device_width", 1080)
        height = getattr(self.ctx.device, "device_height", 2400)
        try:
            if isinstance(sequence, str):
                sequence_str = sequence.strip()
                try:
                    sequence = json.loads(sequence_str)
                except Exception:
                    try:
                        sequence = ast.literal_eval(sequence_str)
                    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
                        pass

            if not isinstance(sequence, (list, tuple)):
                return (
                    f"Error during click sequence: Invalid sequence format: {sequence}",
                    None,
                    None,
                    None,
                )

            # Resolve each entry to normalized coordinates: integer element indices go
            # through state.indexed_points (pixel centers -> normalized), coordinate
            # pairs are already normalized.
            norm_points: list[tuple[int, int]] = []
            for raw_target in sequence:
                target = normalize_coordinate_target(raw_target)
                if isinstance(target, int):
                    idx = target
                    points = getattr(state, "indexed_points", []) or []
                    if 1 <= idx <= len(points):
                        pt = points[idx - 1]
                        px, py = int(pt[0]), int(pt[1])
                        nx = int(max(0, min(1000, round(px * 1000 / max(1, width)))))
                        ny = int(max(0, min(1000, round(py * 1000 / max(1, height)))))
                    else:
                        return (
                            (
                                "Error during click sequence: Index"
                                f" {idx} is out of range (available: {len(points)})"
                            ),
                            None,
                            None,
                            None,
                        )
                elif isinstance(target, (list, tuple)) and len(target) == 2:
                    nx, ny = int(target[0]), int(target[1])
                else:
                    return (
                        f"Error during click sequence: Invalid target format: {raw_target}",
                        None,
                        None,
                        None,
                    )
                norm_points.append((nx, ny))

            res = await self.actuator.click_sequence(norm_points, delay_ms=delay_ms)
            if not res.ok:
                return res.message, None, None, None

            shot_path, img_bytes, xml_list = await capture_screenshot_and_parse_ui(
                self.ctx, state, self.controller
            )
            return res.message, img_bytes, shot_path, xml_list
        except Exception as e:
            return f"Error executing click sequence: {e}", None, None, None

    @trace(type="action", name="long_press", serializer=serialize_mobile_action_result)
    async def exec_long_press(
        self,
        target: list[int] | Any,
        state: State,
        duration_ms: int = 1000,
        duration: int | None = None,
    ) -> tuple[str, bytes | None, str | None, str | None]:
        if duration is not None:
            duration_ms = duration
        try:
            target = normalize_coordinate_target(target)
            if not isinstance(target, (list, tuple)) or len(target) != 2:
                raise ValueError(f"Invalid target format: {target}")
            nx, ny = target
            res = await self.actuator.long_press(int(nx), int(ny), duration_ms=duration_ms)
            outcome = res.message

            img_bytes, shot_path, xml_list = None, None, None
            if res.ok:
                shot_path, img_bytes, xml_list = await capture_screenshot_and_parse_ui(
                    self.ctx, state, self.controller
                )
        except Exception as e:
            outcome = f"Error during long press: {e}"
            img_bytes, shot_path, xml_list = None, None, None

        return outcome, img_bytes, shot_path, xml_list

    @trace(type="action", name="input_text", serializer=serialize_mobile_action_result)
    async def exec_input_text(
        self,
        text: str,
        target: list[int] | Any,
        state: State,
        clear_exist: bool = True,
    ) -> tuple[str, bytes | None, str | None, str | None]:
        try:
            norm_target: tuple[int, int] | None = None
            if target:
                target = normalize_coordinate_target(target)
                if not isinstance(target, (list, tuple)) or len(target) != 2:
                    raise ValueError(f"Invalid target format: {target}")
                norm_target = (int(target[0]), int(target[1]))

            res = await self.actuator.input_text(text, norm_target, clear_exist=clear_exist)
            outcome = res.message
            shot_path, img_bytes, xml_list = await capture_screenshot_and_parse_ui(
                self.ctx, state, self.controller
            )
        except Exception as e:
            outcome = f"Error during input text: {e}"
            img_bytes, shot_path, xml_list = None, None, None

        return outcome, img_bytes, shot_path, xml_list

    @trace(type="action", name="swipe", serializer=serialize_mobile_action_result)
    async def exec_swipe(
        self,
        action: str | list[int] | Any = None,
        duration: int = 800,
        state: State | None = None,
        duration_ms: int | None = None,
        **kwargs: Any,
    ) -> tuple[str, bytes | None, str | None, str | None]:
        width = getattr(self.ctx.device, "device_width", 1080)
        height = getattr(self.ctx.device, "device_height", 2400)
        dur = duration_ms if duration_ms is not None else duration

        swipe_input = dict(kwargs) if kwargs else {}
        if isinstance(action, dict):
            swipe_input.update(action)
        elif action is not None:
            swipe_input["action"] = action
        if "duration" not in swipe_input:
            swipe_input["duration"] = dur

        kind, target, final_duration = parse_swipe_parameters(swipe_input, default_duration=dur)

        try:
            if kind == "direction":
                dir_name = target
                # Smart-swipe resolution needs State (indexed elements + ui tree), so
                # it stays client-side; the actuator only receives the two points.
                x1, y1, x2, y2, smart_dur = compute_smart_swipe_coordinates(
                    direction=target,
                    target=swipe_input.get("target"),
                    indexed_elements=getattr(state, "indexed_elements", None) if state else None,
                    ui_hierarchy=getattr(state, "latest_ui_hierarchy", None) if state else None,
                    width=width,
                    height=height,
                    duration=final_duration,
                )
                final_duration = smart_dur
                nx1 = int(max(0, min(1000, round(x1 * 1000 / max(1, width)))))
                ny1 = int(max(0, min(1000, round(y1 * 1000 / max(1, height)))))
                nx2 = int(max(0, min(1000, round(x2 * 1000 / max(1, width)))))
                ny2 = int(max(0, min(1000, round(y2 * 1000 / max(1, height)))))
                res = await self.actuator.swipe((nx1, ny1), (nx2, ny2), final_duration)
                if not res.ok:
                    return f"Error swiping {dir_name}: {res.detail}", None, None, None
                outcome = f"Swipe completed successfully. Swiped {dir_name}."
            elif kind == "coords":
                x1, y1, x2, y2 = target
                res = await self.actuator.swipe(
                    (int(x1), int(y1)), (int(x2), int(y2)), final_duration
                )
                if not res.ok:
                    return f"Error dragging: {res.detail}", None, None, None
                outcome = f"Swipe completed successfully. Swiped from [{x1}, {y1}] to [{x2}, {y2}]."
            else:
                return (
                    f"Error during swipe: Invalid direction: {action or kwargs}",
                    None,
                    None,
                    None,
                )

            current_state = state or State()
            shot_path, img_bytes, xml_list = await capture_screenshot_and_parse_ui(
                self.ctx, current_state, self.controller
            )
        except Exception as e:
            outcome = f"Error during swipe: {e}"
            img_bytes, shot_path, xml_list = None, None, None

        return outcome, img_bytes, shot_path, xml_list

    @trace(type="action", name="press_key", serializer=serialize_mobile_action_result)
    async def exec_press_key(
        self,
        key: str,
        state: State,
    ) -> tuple[str, bytes | None, str | None, str | None]:
        try:
            res = await self.actuator.press_key(key)
            if not res.ok:
                return res.message, None, None, None

            outcome = res.message
            shot_path, img_bytes, xml_list = await capture_screenshot_and_parse_ui(
                self.ctx, state, self.controller
            )
        except Exception as e:
            outcome = f"Error during press_key: {e}"
            img_bytes, shot_path, xml_list = None, None, None

        return outcome, img_bytes, shot_path, xml_list

    @trace(type="action", name="manage_app", serializer=serialize_mobile_action_result)
    async def exec_manage_app(
        self,
        action: str,
        app_name: str,
        state: State,
    ) -> tuple[str, bytes | None, str | None, str | None]:
        try:
            res = await self.actuator.manage_app(action, app_name)
            # Argument-level failures return without an observation, exactly as the
            # historical early returns did; a failed launch still captures the screen.
            if res.code in (ActionCode.PACKAGE_NOT_FOUND, ActionCode.INVALID_ARGS):
                return res.message, None, None, None

            outcome = res.message
            shot_path, img_bytes, xml_list = await capture_screenshot_and_parse_ui(
                self.ctx, state, self.controller
            )
        except Exception as e:
            outcome = f"Error during manage_app: {e}"
            img_bytes, shot_path, xml_list = None, None, None

        return outcome, img_bytes, shot_path, xml_list

    @trace(type="action", name="wait_for_delay", serializer=serialize_mobile_action_result)
    async def exec_wait_for_delay(
        self,
        time_in_ms: int,
        state: State,
    ) -> tuple[str, bytes | None, str | None, str | None]:
        try:
            res = await self.actuator.wait_for_delay(time_in_ms)
            outcome = res.message
            shot_path, img_bytes, xml_list = await capture_screenshot_and_parse_ui(
                self.ctx, state, self.controller
            )
        except Exception as e:
            outcome = f"Error during wait_for_delay: {e}"
            img_bytes, shot_path, xml_list = None, None, None

        return outcome, img_bytes, shot_path, xml_list

    @trace(type="action", name="wait_for_text", serializer=serialize_mobile_action_result)
    async def exec_wait_for_text(
        self,
        text: str,
        wait_state: str | None,
        timeout_ms: int | None,
        state: State,
    ) -> tuple[str, bytes | None, str | None, str | None]:
        try:
            res = await self.actuator.wait_for_text(text, wait_state, timeout_ms)
            outcome = res.message
            # A timeout still captures the screen, as the original loop did.
            shot_path, img_bytes, xml_list = await capture_screenshot_and_parse_ui(
                self.ctx, state, self.controller
            )
        except Exception as e:
            outcome = f"Error during wait_for_text: {e}"
            img_bytes, shot_path, xml_list = None, None, None

        return outcome, img_bytes, shot_path, xml_list

    @trace(type="tool", name="read_note")
    async def exec_read_note(
        self,
        key: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> str:
        try:
            base_dir = self.ctx.data_engine.base_dir if self.ctx.data_engine else None
            if not base_dir:
                return format_read_note_failure(key, "DataEngine not initialized.")
            return read_note_content(base_dir, key, start_line, end_line)
        except Exception as e:
            return format_read_note_failure(key, str(e))

    @trace(type="tool", name="list_notes")
    async def exec_list_notes(self) -> str:
        try:
            base_dir = self.ctx.data_engine.base_dir if self.ctx.data_engine else None
            if not base_dir:
                return format_list_notes_failure("DataEngine not initialized.")
            notes = list_notes_info(base_dir)
            return format_list_notes_success(notes)
        except Exception as e:
            return format_list_notes_failure(str(e))

    @trace(type="tool", name="ask_explorer", serializer=serialize_explorer_result)
    async def exec_ask_explorer(
        self,
        query: str,
        context_feedback: str | None,
        state: State,
    ) -> tuple[str, bool]:
        """Runs the Explorer pipeline; returns ``(text, ok)``.

        ``ok`` is False only when the Explorer run itself failed. A clean
        "not found" is a successful answer the agent must reason about, so it
        must not be reported as a tool error.
        """
        try:
            outcome = await locate(
                self.ctx, state, query, context_feedback or "", agent_name=self.agent_name
            )
            registered = register_candidates(self.ctx, state, outcome)
            return render_text(query, outcome, registered), not outcome.error
        except Exception as e:
            return f"Error executing ask_explorer: {e}", False

    async def execute(
        self,
        name: str,
        args: dict[str, Any],
        tool_call_id: str,
        state: State,
    ) -> ToolExecutionResult:
        """Executes the specified tool by name and wraps result into ToolExecutionResult."""
        raw_name = name.split(":")[-1] if ":" in name else name
        res_text = ""
        img_bytes, shot_path, xml_list = None, None, None
        # Explicit status for tools that report it (the Explorer); None keeps the
        # historical text-sniffing fallback for the device actions.
        explicit_ok: bool | None = None

        if raw_name == "click":
            res_text, img_bytes, shot_path, xml_list = await self.exec_click(
                args.get("target") or args.get("coordinates") or [],
                state,
                times=args.get("times", 1),
                delay_ms=args.get("delay_ms", 100),
            )
        elif raw_name == "click_sequence":
            res_text, img_bytes, shot_path, xml_list = await self.exec_click_sequence(
                args.get("sequence") or [],
                state,
                delay_ms=args.get("delay_ms", 50),
            )
        elif raw_name == "long_press":
            res_text, img_bytes, shot_path, xml_list = await self.exec_long_press(
                args.get("target") or args.get("coordinates") or [],
                state,
                duration_ms=args.get("duration_ms", 1000),
            )
        elif raw_name == "input_text":
            res_text, img_bytes, shot_path, xml_list = await self.exec_input_text(
                args.get("text", ""),
                args.get("target") or args.get("coordinates"),
                state,
                clear_exist=args.get("clear_exist", True),
            )
        elif raw_name == "swipe":
            res_text, img_bytes, shot_path, xml_list = await self.exec_swipe(
                action=args, duration=args.get("duration", 400), state=state
            )
        elif raw_name == "press_key":
            res_text, img_bytes, shot_path, xml_list = await self.exec_press_key(
                args.get("key", "BACK"), state
            )
        elif raw_name == "manage_app":
            res_text, img_bytes, shot_path, xml_list = await self.exec_manage_app(
                args.get("action", "launch"), args.get("app_name", ""), state
            )
        elif raw_name == "wait_for_delay":
            res_text, img_bytes, shot_path, xml_list = await self.exec_wait_for_delay(
                args.get("time_in_ms", 1000), state
            )
        elif raw_name == "wait_for_text":
            res_text, img_bytes, shot_path, xml_list = await self.exec_wait_for_text(
                args.get("text", ""),
                args.get("wait_state"),
                args.get("timeout_ms", 5000),
                state,
            )
        elif raw_name == "read_note":
            res_text = await self.exec_read_note(
                args.get("key", ""),
                args.get("start_line"),
                args.get("end_line"),
            )
        elif raw_name == "list_notes":
            res_text = await self.exec_list_notes()
        elif raw_name == "ask_explorer":
            res_text, explicit_ok = await self.exec_ask_explorer(
                # ``task_description`` is the pre-contract argument name kept
                # for old MCP clients.
                args.get("query") or args.get("task_description") or "",
                args.get("context_feedback"),
                state,
            )
        else:
            res_text = f"Error: Tool '{name}' not supported."

        if shot_path:
            state.latest_screenshot = shot_path

        status: Literal["success", "error"] = (
            "success" if (img_bytes is not None or raw_name not in ACTION_TOOL_NAMES) else "error"
        )
        if explicit_ok is not None:
            status = "success" if explicit_ok else "error"
        elif "Error" in res_text or "Failed" in res_text:
            status = "error"

        return ToolExecutionResult(
            tool_call_id=tool_call_id,
            tool_name=name,
            status=status,
            text_summary=res_text,
            screenshot_bytes=img_bytes,
            screenshot_path=shot_path,
            ui_elements_text=xml_list,
        )


# Device-action declarations are generated from the canonical manifest
# (artemis/mcp/action_specs.py); the historical constant names remain the public API.
CLICK_TOOL = tool_declaration("click")

CLICK_SEQUENCE_TOOL = tool_declaration("click_sequence")

LONG_PRESS_TOOL = tool_declaration("long_press")

INPUT_TEXT_TOOL = tool_declaration("input_text")

SWIPE_TOOL = tool_declaration("swipe")

PRESS_KEY_TOOL = tool_declaration("press_key")

READ_NOTE_TOOL = ToolDeclaration(
    name="read_note",
    description=READ_NOTE_DOCSTRING,
    parameters={
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": READ_NOTE_ARG_KEY_DESC,
            },
            "start_line": {
                "type": "integer",
                "description": "Start line to read (1-indexed, inclusive).",
            },
            "end_line": {
                "type": "integer",
                "description": "End line to read (1-indexed, inclusive).",
            },
        },
        "required": ["key"],
    },
)

LIST_NOTES_TOOL = ToolDeclaration(
    name="list_notes",
    description=LIST_NOTES_DOCSTRING,
    parameters={"type": "object", "properties": {}},
)

MANAGE_APP_TOOL = tool_declaration("manage_app")

WAIT_FOR_DELAY_TOOL = tool_declaration("wait_for_delay")

REPORT_TASK_STATUS_TOOL = ToolDeclaration(
    name="report_task_status",
    description=(
        "[REPORT] Use this tool to return your final answer when you are done"
        " with the task. This is the ONLY way to return your final conclusion."
    ),
    parameters={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": (
                    "Whether the task was completed successfully ('completed') or"
                    " failed/unreachable ('failed')."
                ),
            },
            "explanation": {
                "type": "string",
                "description": "Provide the final explanation or summary of the task execution.",
            },
        },
        "required": ["status", "explanation"],
    },
)

# Same contract as the Operator's LangChain tool (artemis/tools/explorer_tool.py):
# the Explorer tier is a user setting, so no tier argument is exposed here.
ASK_EXPLORER_TOOL = ToolDeclaration(
    name=ASK_EXPLORER_TOOL_NAME,
    description=ASK_EXPLORER_DESCRIPTION,
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": ASK_EXPLORER_QUERY_DESCRIPTION,
            },
            "context_feedback": {
                "type": "string",
                "description": ASK_EXPLORER_CONTEXT_FEEDBACK_DESCRIPTION,
            },
        },
        "required": ["query"],
    },
)

VALIDATOR_TOOLS_DECLARATION: list[ToolDeclaration] = [
    CLICK_TOOL,
    LONG_PRESS_TOOL,
    INPUT_TEXT_TOOL,
    SWIPE_TOOL,
    PRESS_KEY_TOOL,
    READ_NOTE_TOOL,
    LIST_NOTES_TOOL,
    MANAGE_APP_TOOL,
    WAIT_FOR_DELAY_TOOL,
]
