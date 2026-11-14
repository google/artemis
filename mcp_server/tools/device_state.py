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

"""MCP Tool: mobile_get_device_state."""

import asyncio
import base64
import os

from mcp_server.base import mcp
from artemis.mcp.adb_server import _get_controller
from artemis.runtime.device_target import normalize_device_platform
from artemis.runtime.ios_observation import observe_ios_controller
from mcp_server.utils import env_utils
from artemis.utils.ocr_xml_fusion import (
    fuse_ocr_with_xml,
    _detect_status_bar_height,
    _crop_image_remove_status_bar,
    _map_coordinates_back,
)
from artemis.utils.ocr_api import is_ocr_configured, perform_ocr
from artemis.utils.visualization import format_minimal_list_with_elements


@mcp.tool()
async def mobile_get_device_state(
    view_type: str,
    device_serial: str | None = None,
    platform: str | None = None,
) -> str:
    """Real-time mobile device state observer (for debugging and validation).

    Retrieves a real-time screenshot or a simplified UI element tree from the
    target device — useful for inspecting device status or tracking a
    subagent's progress.

    Args:
        view_type: Observation type:
          - "screenshot": captures the screen, saves it to the workspace, and
            returns the image's local file URI.
          - "hierarchy": returns the simplified text-labeled element list —
            exactly what the automation subagent sees when making decisions.
        device_serial: Optional device serial (e.g. "emulator-5554", or an iOS
          device UDID with platform="ios") to inspect a specific device;
          omitted → the default connected device.
        platform: "android" (default) or "ios". For iOS, device_serial is a
          device UDID (simulator or paired physical device); omit it to use the single booted simulator.
    """
    try:
        # Strict at the public boundary: a misspelled platform must never fall
        # through to the Android controller. None/empty stays Android.
        canonical_platform = normalize_device_platform(platform)
    except ValueError as e:
        return f"Error: Failed to initialize/lock device controller: {e}"
    try:
        if canonical_platform == "ios":
            # iOS resolution enumerates simctl/devicectl synchronously; offload
            # the blocking factory so the MCP loop stays responsive.
            controller = await asyncio.to_thread(
                _get_controller, device_serial=device_serial, target_platform="ios"
            )
        else:
            controller = _get_controller(device_serial=device_serial)
        is_ios = str(getattr(controller.ctx.device, "mobile_platform", "android")).lower() == "ios"
    except Exception as e:
        return f"Error: Failed to initialize/lock device controller: {e}"

    try:
        if is_ios:
            # Owns the whole one-shot lifecycle — device lease, native connect,
            # capture, disconnect, release — all cancellation-safe.
            device_data = await observe_ios_controller(controller)
        else:
            device_data = await controller.get_screen_data()
        device_width = controller.ctx.device.device_width
        device_height = controller.ctx.device.device_height
        latest_screenshot_b64 = device_data.base64
        xml_hierarchy = device_data.elements

        project_root = env_utils.get_project_root()

        if view_type == "screenshot":
            screenshot_bytes = base64.b64decode(latest_screenshot_b64)
            device_id = controller.ctx.device.device_id
            safe_device_id = "".join(
                [c if c.isalnum() or c in ("-", "_") else "_" for c in device_id]
            )
            ext = "png" if is_ios else "jpg"
            screenshot_filename = f"live_screenshot_{safe_device_id}.{ext}"
            screenshot_path = os.path.join(project_root, screenshot_filename)

            with open(screenshot_path, "wb") as f:
                f.write(screenshot_bytes)

            return f"file://{screenshot_path}"

        elif view_type == "hierarchy":
            ocr_results = []
            if is_ocr_configured():
                try:
                    screen_height = device_data.height
                    status_bar_height = _detect_status_bar_height(xml_hierarchy, screen_height)
                    if status_bar_height > 0:
                        cropped_b64, _, _ = _crop_image_remove_status_bar(
                            latest_screenshot_b64, status_bar_height
                        )
                        raw_ocr_results = await perform_ocr(cropped_b64)
                        ocr_results = _map_coordinates_back(raw_ocr_results, status_bar_height)
                    else:
                        ocr_results = await perform_ocr(latest_screenshot_b64)
                except Exception:
                    ocr_results = []

            fused_xml = fuse_ocr_with_xml(xml_hierarchy, ocr_results)
            hierarchy_text, elements, labels = format_minimal_list_with_elements(
                fused_xml, width=device_width, height=device_height
            )
            return hierarchy_text

        else:
            return f"Error: Invalid view_type '{view_type}'. Supported types are 'screenshot' and 'hierarchy'."

    except Exception as e:
        return f"Error: An unexpected error occurred while communicating with the device: {e}"
