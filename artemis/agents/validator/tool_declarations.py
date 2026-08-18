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
import asyncio
import base64
import hashlib
import inspect
import json
from pathlib import Path
import time
from typing import Any, Literal

from google.genai import types as genai_types
from langchain_core.messages import ToolMessage
from pydantic import BaseModel, Field

from artemis.config import get_temp_dir, resolve_explorer_version
from artemis.context import ArtemisContext
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.core.tool_declaration import ToolDeclaration
from artemis.data_engine.trace import trace
from artemis.tools.explorer_tool import _run_explorer_logic
from artemis.graph.state import State
from artemis.tools.mobile.launch_app import find_package
from artemis.utils.app_launch_utils import launch_app_with_retries
from artemis.utils.coordinates import (
    compute_smart_swipe_coordinates,
    parse_swipe_parameters,
)
from artemis.utils.logger import get_logger
from artemis.utils.ocr_xml_fusion import fuse_ocr_with_xml
from artemis.utils.visualization import format_minimal_list_with_elements
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
                        except Exception:
                            pass
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
    """Captures screenshot and parses fused XML tree after optional screen settling delay."""
    try:
        if skip_settling:
            logger.info("Retrieving screen data directly (skip settling)...")
            device_data = await controller.get_screen_data()
            latest_screenshot_b64 = device_data.base64
        else:
            logger.info("Delaying 0.4s before capturing screen state...")
            await asyncio.sleep(0.4)
            device_data = await controller.get_screen_data()
            latest_screenshot_b64 = device_data.base64

        latest_screenshot_bytes = base64.b64decode(latest_screenshot_b64)
    except Exception as e:
        logger.error(f"Failed to capture screen state: {e}")
        return None, None, None

    try:
        xml_hierarchy = device_data.elements
        width = device_data.width or 1080
        height = device_data.height or 2400
        if ctx.device:
            ctx.device.device_width = width
            ctx.device.device_height = height

        ocr_results = []

        fused_xml = fuse_ocr_with_xml(xml_hierarchy, ocr_results)

        minimal_list_str, elements, _ = format_minimal_list_with_elements(fused_xml, width, height)

        state.indexed_points = [el["center"] for el in elements]
        state.indexed_elements = elements
    except Exception as e:
        logger.error(f"Failed to fetch or fuse UI hierarchy: {e}")
        minimal_list_str = "Not available due to hierarchy error."

    screenshot_path = None
    if ctx.data_engine:
        try:
            image_name = ctx.data_engine.get_or_create_image(
                latest_screenshot_bytes,
                ui_tree=xml_hierarchy,
                ocr_result=ocr_results,
            )
            screenshot_path = str(ctx.data_engine.get_image_path(image_name))
        except Exception as e:
            logger.error(f"Failed to save image in DataEngine: {e}")

    if not screenshot_path:
        try:
            temp_dir = get_temp_dir("screenshots")
            temp_file = temp_dir / f"screenshot_{int(time.time())}.jpg"
            temp_file.write_bytes(latest_screenshot_bytes)
            screenshot_path = str(temp_file)
        except Exception as e:
            logger.error(f"Failed to save fallback screenshot: {e}")

    return screenshot_path, latest_screenshot_bytes, minimal_list_str


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


class MobileActionExecutor:
    """Encapsulates device action execution, observation capture, and result reporting."""

    def __init__(self, ctx: ArtemisContext, controller: UnifiedMobileController | None = None):
        self.ctx = ctx
        self.controller = controller or UnifiedMobileController(ctx)

    @trace(type="action", name="click", serializer=serialize_mobile_action_result)
    async def exec_click(
        self,
        target: list[int] | Any,
        state: State,
        times: int = 1,
        delay_ms: int = 100,
    ) -> tuple[str, bytes | None, str | None, str | None]:
        width = getattr(self.ctx.device, "device_width", 1080)
        height = getattr(self.ctx.device, "device_height", 2400)
        try:
            target = normalize_coordinate_target(target)
            if not isinstance(target, (list, tuple)) or len(target) != 2:
                raise ValueError(f"Invalid target format: {target}")
            nx, ny = target
            x = int(max(0, min(width - 1, int(nx) * width / 1000)))
            y = int(max(0, min(height - 1, int(ny) * height / 1000)))

            result = await self.controller.tap_at(x, y, times=times, delay_ms=delay_ms)
            err = getattr(result, "error", None) if result else None
            success = err is None
            outcome = (
                f"Clicked at [{nx}, {ny}] (normalized) successfully."
                if success
                else f"Error executing click: {err}"
            )

            img_bytes, shot_path, xml_list = None, None, None
            if success:
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
                    except Exception:
                        pass

            if not isinstance(sequence, (list, tuple)):
                return (
                    f"Error during click sequence: Invalid sequence format: {sequence}",
                    None,
                    None,
                    None,
                )

            outcomes = []
            for i, raw_target in enumerate(sequence):
                target = normalize_coordinate_target(raw_target)
                if isinstance(target, int):
                    idx = target
                    points = getattr(state, "indexed_points", []) or []
                    if 1 <= idx <= len(points):
                        pt = points[idx - 1]
                        x, y = int(pt[0]), int(pt[1])
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
                    nx, ny = target
                    x = int(max(0, min(width - 1, int(nx) * width / 1000)))
                    y = int(max(0, min(height - 1, int(ny) * height / 1000)))
                else:
                    return (
                        f"Error during click sequence: Invalid target format: {raw_target}",
                        None,
                        None,
                        None,
                    )

                result = await self.controller.tap_at(x, y)
                err = getattr(result, "error", None) if result else None
                if err:
                    return (
                        f"Error executing click at step {i + 1}: {err}",
                        None,
                        None,
                        None,
                    )
                outcomes.append(f"Tapped at [{x}, {y}]")
                if i < len(sequence) - 1:
                    await asyncio.sleep(max(0, delay_ms) / 1000.0)

            outcome_str = f"Sequence clicked successfully: {'; '.join(outcomes)}"
            shot_path, img_bytes, xml_list = await capture_screenshot_and_parse_ui(
                self.ctx, state, self.controller
            )
            return outcome_str, img_bytes, shot_path, xml_list
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
        width = getattr(self.ctx.device, "device_width", 1080)
        height = getattr(self.ctx.device, "device_height", 2400)
        if duration is not None:
            duration_ms = duration
        try:
            target = normalize_coordinate_target(target)
            if not isinstance(target, (list, tuple)) or len(target) != 2:
                raise ValueError(f"Invalid target format: {target}")
            nx, ny = target
            x = int(max(0, min(width - 1, int(nx) * width / 1000)))
            y = int(max(0, min(height - 1, int(ny) * height / 1000)))

            result = await self.controller.tap_at(
                x, y, long_press=True, long_press_duration=duration_ms
            )
            err = getattr(result, "error", None) if result else None
            success = err is None
            outcome = (
                f"Long pressed at [{nx}, {ny}] (normalized) for {duration_ms}ms successfully."
                if success
                else f"Error executing long press: {err}"
            )

            img_bytes, shot_path, xml_list = None, None, None
            if success:
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
        width = getattr(self.ctx.device, "device_width", 1080)
        height = getattr(self.ctx.device, "device_height", 2400)
        try:
            if target:
                target = normalize_coordinate_target(target)
                if not isinstance(target, (list, tuple)) or len(target) != 2:
                    raise ValueError(f"Invalid target format: {target}")
                nx, ny = target
                x = int(max(0, min(width - 1, int(nx) * width / 1000)))
                y = int(max(0, min(height - 1, int(ny) * height / 1000)))

                await self.controller.tap_at(x, y)
            if clear_exist and hasattr(self.controller, "erase_text"):
                await self.controller.erase_text()
            if hasattr(self.controller, "type_text"):
                await self.controller.type_text(text)

            outcome = f"Executed typing '{text}'."
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
                x1, y1, x2, y2, smart_dur = compute_smart_swipe_coordinates(
                    direction=target,
                    target=swipe_input.get("target"),
                    indexed_elements=getattr(state, "indexed_elements", None) if state else None,
                    ui_hierarchy=getattr(state, "ui_tree", None) if state else None,
                    width=width,
                    height=height,
                    duration=final_duration,
                )
                final_duration = smart_dur
                err = await self.controller.swipe_coords(x1, y1, x2, y2, final_duration)
                if err:
                    return f"Error swiping {dir_name}: {err}", None, None, None
                outcome = f"Swipe completed successfully. Swiped {dir_name}."
            elif kind == "coords":
                x1, y1, x2, y2 = target
                px1 = int(max(0, min(width - 1, int(x1) * width / 1000)))
                py1 = int(max(0, min(height - 1, int(y1) * height / 1000)))
                px2 = int(max(0, min(width - 1, int(x2) * width / 1000)))
                py2 = int(max(0, min(height - 1, int(y2) * height / 1000)))
                err = await self.controller.swipe_coords(px1, py1, px2, py2, final_duration)
                if err:
                    return f"Error dragging: {err}", None, None, None
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
            valid_keys = {"home", "back", "enter", "delete", "tab", "search", "menu", "app_switch"}
            key_str = str(key).lower()
            if key_str not in valid_keys:
                return f"Error executing key press '{key}'.", None, None, None

            if key_str == "back" and hasattr(self.controller, "go_back"):
                await self.controller.go_back()
            elif key_str == "home" and hasattr(self.controller, "go_home"):
                await self.controller.go_home()
            elif key_str == "enter" and hasattr(self.controller, "press_enter"):
                await self.controller.press_enter()
            elif hasattr(self.controller, "press_key"):
                res = await self.controller.press_key(key)
                err = getattr(res, "error", None) if res else None
                if err:
                    return f"Error executing key press '{key}': {err}", None, None, None

            outcome = f"Executed key press '{key}'."
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
            res = find_package(self.ctx, app_name, use_fallback=False)
            pkg = await res if inspect.iscoroutine(res) else res
            if not pkg:
                return f"Error finding package for app: {app_name}", None, None, None
            target_pkg = pkg

            if action.lower() == "launch":
                res_launch = launch_app_with_retries(self.ctx, target_pkg)
                if inspect.iscoroutine(res_launch):
                    res_launch = await res_launch
                if isinstance(res_launch, tuple):
                    success, error_msg = res_launch
                else:
                    success, error_msg = bool(res_launch), ""

                outcome = (
                    f"Launched app '{app_name}' ({target_pkg}) successfully."
                    if success
                    else f"Failed to launch app '{app_name}': {error_msg}"
                )
            elif action.lower() == "stop":
                if hasattr(self.controller, "terminate_app"):
                    res_term = self.controller.terminate_app(target_pkg)
                    if inspect.iscoroutine(res_term):
                        await res_term
                outcome = f"Terminated app '{app_name}' successfully."
            else:
                return f"Invalid manage_app action: {action}", None, None, None

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
            delay_s = max(0.0, float(time_in_ms) / 1000.0)
            await asyncio.sleep(delay_s)
            outcome = f"Waited for {time_in_ms}ms successfully."
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
            timeout_s = (timeout_ms or 5000) / 1000.0
            start = time.time()
            found = False
            target_state = (wait_state or "appear").lower()

            while time.time() - start < timeout_s:
                tree = await self.controller.get_ui_elements()
                text_present = text.lower() in (tree or "").lower()
                if target_state == "appear" and text_present:
                    found = True
                    break
                elif target_state == "disappear" and not text_present:
                    found = True
                    break
                await asyncio.sleep(0.5)

            outcome = (
                f"Successfully waited for text '{text}' to {target_state}."
                if found
                else f"Timed out waiting for text '{text}' to {target_state}."
            )
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

    @trace(type="tool", name="ask_explorer")
    async def exec_ask_explorer(
        self,
        query: str,
        context_feedback: str | None,
        state: State,
        version: str | None = None,
    ) -> str:
        try:
            active_version = resolve_explorer_version(
                self.ctx,
                explicit_version=version,
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
            res_text = await self.exec_ask_explorer(
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
        if "Error" in res_text or "Failed" in res_text:
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


CLICK_TOOL = ToolDeclaration(
    name="click",
    description=(
        "[ACTION] Tap/click on the target coordinate on the screen. The screen"
        " after click will be returned automatically. For buttons, checkboxes,"
        " tabs, icons, items in a list, tap ON the element. For text fields /"
        " search bars / input boxes, tap INSIDE the input box to focus it."
    ),
    parameters={
        "type": "object",
        "properties": {
            "target": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Normalized coordinates [x, y] in 0-1000 scale.",
            },
            "times": {
                "type": "integer",
                "description": "Number of taps to perform (default 1). Set to 2 for double click.",
            },
            "delay_ms": {
                "type": "integer",
                "description": "Delay between taps in milliseconds (default 100).",
            },
        },
        "required": ["target"],
    },
)

CLICK_SEQUENCE_TOOL = ToolDeclaration(
    name="click_sequence",
    description=(
        "[ACTION] Executes a sequence of taps one by one in order on the"
        " specified targets (e.g. [[500, 280], [885, 362]]). The screen will be returned ONLY after all clicks"
        " in the sequence have completed."
    ),
    parameters={
        "type": "object",
        "properties": {
            "sequence": {
                "type": "array",
                "items": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": (
                        "Normalized coordinates [x, y] in 0-1000 scale (e.g., [500, 280]),"
                        " or a single integer element index."
                    ),
                },
                "description": "List of targets to tap in sequence, e.g. [[500, 280], [885, 362]].",
            },
            "delay_ms": {
                "type": "integer",
                "description": "Delay between consecutive taps in milliseconds (default 50ms).",
            },
        },
        "required": ["sequence"],
    },
)

LONG_PRESS_TOOL = ToolDeclaration(
    name="long_press",
    description=(
        "[ACTION] Long press on a target coordinate on the screen. The screen"
        " after long pressing will be returned automatically."
    ),
    parameters={
        "type": "object",
        "properties": {
            "target": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Normalized coordinates [x, y] in 0-1000 scale.",
            },
            "duration_ms": {
                "type": "integer",
                "description": "Duration of press in milliseconds (default 1000ms).",
            },
        },
        "required": ["target"],
    },
)

INPUT_TEXT_TOOL = ToolDeclaration(
    name="input_text",
    description=(
        "[ACTION] Type text into an input field on the screen. The screen after"
        " typing will be returned automatically. Automatically taps inside the"
        " input box at target [x, y] to focus before typing."
    ),
    parameters={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Text to type into the focused input field.",
            },
            "target": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Coordinates [x, y] of the input box in 0-1000 scale.",
            },
            "clear_exist": {
                "type": "boolean",
                "description": (
                    "Whether to clear existing text in the input box before typing (default True)."
                ),
            },
        },
        "required": ["text", "target"],
    },
)

SWIPE_TOOL = ToolDeclaration(
    name="swipe",
    description=(
        "[ACTION] Perform a swipe, drag, or slider-adjustment gesture on the screen. The screen and UI hierarchy after swipe will be returned automatically.\n\n"
        "• Directional Scrolling ('direction' or 'action'): Recommended for general browsing and standard page scrolling in most scenarios. Automatically computes safe swipe vectors and adaptive duration, retains a ~40% visual overlap anchor for zero-omission traversal, and prevents inertial flings. Supports scoping to a sub-container via 'target'. If it fails on certain custom layouts, fall back to specifying exact coordinates ('start' and 'end') directly.\n"
        "• Precise Coordinate Gestures ('start', 'end' or coordinates list): Best for local, fine-grained interactions such as adjusting sliders/SeekBars (e.g., volume, brightness, progress bars), drag-and-drop / list reordering, or as a reliable fallback when directional scrolling fails on specific containers. Always drag slightly PAST the target position to overcome touch slop and reliably trigger the update. When setting a slider to Maximum (100%) or Minimum (0%), swipe fully to the extreme boundary."
    ),
    parameters={
        "type": "object",
        "properties": {
            "direction": {
                "type": "string",
                "enum": ["up", "down", "left", "right"],
                "description": (
                    "Direction for scrolling and swiping: 'up' (drags bottom-to-top, scrolling down to reveal content below),"
                    " 'down' (drags top-to-bottom, scrolling up to reveal content above),"
                    " 'left' (drags right-to-left, scrolling right),"
                    " 'right' (drags left-to-right, scrolling left)."
                ),
            },
            "start": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Start normalized coordinates [start_x, start_y] in 0-1000 scale.",
            },
            "end": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "End normalized coordinates [end_x, end_y] in 0-1000 scale.",
            },
            "target": {
                "description": "Optional target element index (e.g. 2) or container bounds [left, top, right, bottom] to scope the directional swipe within.",
            },
            "action": {
                "description": (
                    "Backward-compatible swipe gesture: smart direction string ('up', 'down', 'left', 'right')"
                    " OR precise custom coordinates [start_x, start_y, end_x, end_y] in 0-1000 scale."
                ),
            },
            "duration": {
                "type": "integer",
                "description": (
                    "Optional swipe/drag duration in milliseconds (default 800). For drag-and-drop,"
                    " list reordering, or sliding/adjusting sliders (e.g., volume, brightness, SeekBars),"
                    " set duration >= 1000 (e.g. 1500). If omitted for directional swipe, duration is computed automatically."
                ),
            },
        },
    },
)

PRESS_KEY_TOOL = ToolDeclaration(
    name="press_key",
    description=(
        "[ACTION] Press a physical or virtual system button. The screen after"
        " pressing will be returned automatically."
    ),
    parameters={
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": (
                    "Standard Android system button name (ENTER, BACK, HOME, APP_SWITCH)."
                ),
            }
        },
        "required": ["key"],
    },
)

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

MANAGE_APP_TOOL = ToolDeclaration(
    name="manage_app",
    description=(
        "[ACTION] Launch or force stop a specified application. The screen"
        " after launch/stop will be returned automatically."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "The action type ('launch' or 'stop').",
            },
            "app_name": {
                "type": "string",
                "description": "Display name or package name of the application.",
            },
        },
        "required": ["action", "app_name"],
    },
)

WAIT_FOR_DELAY_TOOL = ToolDeclaration(
    name="wait_for_delay",
    description=(
        "[ACTION] Pause execution and wait for a specified duration in milliseconds."
        " The screen after pause will be returned automatically."
    ),
    parameters={
        "type": "object",
        "properties": {
            "time_in_ms": {
                "type": "integer",
                "description": (
                    "The exact duration to pause in milliseconds (e.g., 2000 for 2s,"
                    " 60000 for 1 min, 180000 for 3 mins, 300000 for 5 mins)."
                    " Convert any required waiting duration into milliseconds."
                ),
            }
        },
        "required": ["time_in_ms"],
    },
)

REPORT_FAILURE_ANALYSIS_TOOL = ToolDeclaration(
    name="report_failure_analysis",
    description=(
        "[REPORT] Use this tool to return your final answer when you are done"
        " with the analysis and optional repair. This is the ONLY way to return"
        " your final conclusion."
    ),
    parameters={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": (
                    "Whether the failure was repaired locally ('fixed') or not ('cannot_fix')."
                ),
            },
            "analysis": {
                "type": "string",
                "description": (
                    "Provide the cause analysis and handling method description in a single"
                    " paragraph, and append the specific operations you ran."
                ),
            },
        },
        "required": ["status", "analysis"],
    },
)

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

ASK_EXPLORER_TOOL = ToolDeclaration(
    name="ask_explorer",
    description=(
        "[EXPLORER] Call the Explorer sub-agent to locate an element or"
        " coordinate on the screen when XML hierarchy lacks coordinates or"
        " visual target is ambiguous."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_description": {
                "type": "string",
                "description": "The description of what to find on the screen.",
            },
            "roi": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Optional bounding box [ymin, xmin, ymax, xmax] in 0-1000 scale.",
            },
        },
        "required": ["task_description"],
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
    REPORT_FAILURE_ANALYSIS_TOOL,
]
