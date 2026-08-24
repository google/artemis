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

import asyncio
import json
import logging
import os
from pathlib import Path
import re
import sys

# Ensure repository root is in sys.path when executed directly or via MCP runner
_REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# pylint: disable=wrong-import-position
from adbutils import AdbClient
from mcp.server.fastmcp import Context, FastMCP

try:
    from mcp.server.fastmcp.server import Settings as FastMCPSettings

    FastMCPSettings.model_rebuild()
except Exception:
    pass

from artemis.clients.ui_automator_client import UIAutomatorClient
from artemis.context import ArtemisContext, DeviceContext, DevicePlatform
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.platform import platform
from artemis.utils.app_launch_utils import launch_app_with_retries

# Redirect stdio to prevent logs from breaking MCP JSON-RPC protocol and causing deadlocks.
try:
    from artemis.config import settings

    log_path = Path(settings.TRACES_PATH) / "mcp_server.log"
except Exception:
    log_path = Path("traces") / "mcp_server.log"

log_path.parent.mkdir(parents=True, exist_ok=True)

for name in ["artemis", __name__]:
    log_instance = logging.getLogger(name)
    log_instance.setLevel(logging.DEBUG)
    log_instance.propagate = False
    # Remove standard streams handlers
    log_instance.handlers = [
        h for h in log_instance.handlers if not isinstance(h, logging.StreamHandler)
    ]
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s | %(name)s | %(levelname)s | %(message)s"))
    log_instance.addHandler(fh)

logger = logging.getLogger(__name__)
logger.info("MCP Server Python process fully started. Configuring FastMCP service...")

# Avoid concurrent SQLite WAL locks by blocking child processes from connecting to primary DataEngine.
os.environ["ARTEMIS_IPC_PORT"] = ""

# Create minimal MCP server
mcp = FastMCP("Android_ADB_Controller")

_GLOBAL_CONTROLLER = None


def _get_controller():
    """Lazy-load device controller on-demand as a singleton."""
    global _GLOBAL_CONTROLLER
    if _GLOBAL_CONTROLLER is not None:
        return _GLOBAL_CONTROLLER

    logger.info("Initializing lazy device controller...")
    if os.environ.get("ARTEMIS_CLOUD_MODE") == "1":
        logger.info(
            "GCP CLOUD MODE active: initializing CloudMobileDeviceController via UnifiedMobileController..."
        )
        target_serial = (
            os.environ.get("ARTEMIS_DEVICE_ID")
            or os.environ.get("ADB_DEVICE_SERIAL")
            or "cloud_device"
        )
        ctx = ArtemisContext(
            device=DeviceContext(
                platform=DevicePlatform.ANDROID,
                device_id=target_serial,
                width=1080,
                height=2400,
            )
        )
        _GLOBAL_CONTROLLER = UnifiedMobileController(ctx=ctx)
        return _GLOBAL_CONTROLLER

    host = os.environ.get("ADB_HOST", "localhost")
    port_str = os.environ.get("ADB_PORT", "5037")
    port = int(port_str) if port_str.isdigit() else 5037
    if "ADB_SERVER_SOCKET" not in os.environ and (host != "localhost" or port != 5037):
        os.environ["ADB_SERVER_SOCKET"] = f"tcp:{host}:{port}"
    adb = AdbClient(host=host, port=port)

    target_serial = os.environ.get("ARTEMIS_DEVICE_ID") or os.environ.get("ADB_DEVICE_SERIAL")
    devices = adb.device_list()
    if not devices:
        raise Exception(f"No Android devices found at {host}:{port}")

    if target_serial:
        matched = [d for d in devices if d.serial == target_serial]
        if not matched:
            raise Exception(
                f"Target Android device '{target_serial}' not found at"
                f" {host}:{port} among available devices."
            )
        device = matched[0]
    else:
        device = devices[0]
    device_id = device.serial

    ui_client = UIAutomatorClient(device_id=device_id)
    try:
        ui_data = ui_client.get_screen_data()
        width, height = ui_data.width, ui_data.height
    except Exception as e:
        logger.warning(f"Failed initial screen data check, using defaults: {e}")
        width, height = 1080, 2400

    ctx = ArtemisContext(
        trace_id="mcp-session",
        device=DeviceContext(
            host_platform=platform.os_type.name,
            mobile_platform=DevicePlatform.ANDROID,
            device_id=device_id,
            device_width=width,
            device_height=height,
        ),
        adb_client=adb,
        ui_adb_client=ui_client,
    )

    _GLOBAL_CONTROLLER = UnifiedMobileController(ctx)
    logger.info(f"Lazy device controller fully initialized for device: {device_id}")
    return _GLOBAL_CONTROLLER


def _find_element_at_coords(elements: list[dict], x: int, y: int) -> dict | None:
    """Finds the smallest (leaf-most) focusable element containing [x, y]."""
    matching_element = None
    min_area = float("inf")

    for elem in elements:
        is_focusable = (
            elem.get("focusable") == "true"
            or elem.get("clickable") == "true"
            or "EditText" in str(elem.get("class", ""))
        )
        if not is_focusable:
            continue

        bounds_str = elem.get("bounds")
        if bounds_str and isinstance(bounds_str, str):
            match = re.match(r"\[(\-?\d+),(\-?\d+)\]\[(\-?\d+),(\-?\d+)\]", bounds_str)
            if match:
                x1, y1, x2, y2 = map(int, match.groups())
                if x1 <= x <= x2 and y1 <= y <= y2:
                    area = (x2 - x1) * (y2 - y1)
                    if area < min_area:
                        min_area = area
                        matching_element = elem

    return matching_element


async def _ensure_focus_at_coords(controller, x: int, y: int) -> str | None:
    """Ensures the element at [x, y] is focused, tapping only if it is not already focused."""
    try:
        elements = await controller.get_ui_elements()
        elem = _find_element_at_coords(elements, x, y)
        if elem and elem.get("focused") == "true":
            logger.info(f"Element under [{x}, {y}] is already focused. Skipping tap.")
            return None
    except Exception as e:
        logger.warning(f"Failed to check focus status: {e}. Falling back to unconditional tap.")

    result = await controller.tap_at(x=x, y=y)
    if hasattr(result, "error") and result.error:
        return result.error

    # Wait for the UI to settle and keyboard to pop up after tapping
    await asyncio.sleep(1.0)
    return None


@mcp.tool()
async def tap(
    ctx: Context,
    coordinates: list[int],
    times: int = 1,
    delay_ms: int = 100,
) -> str:
    """Taps on the screen at coordinates.

    'coordinates' is a list [x, y].
    'times' is the number of consecutive clicks (default 1).
    'delay_ms' is the delay in milliseconds between consecutive clicks (default
    100).
    """
    try:
        controller = _get_controller()
    except Exception as e:
        return f"Error: Controller lazy initialization failed: {e}"

    if len(coordinates) != 2:
        return "Error: coordinates must be [x, y]"

    result = await controller.tap_at(
        x=coordinates[0], y=coordinates[1], times=times, delay_ms=delay_ms
    )

    if result.error:
        return f"Error: {result.error}"
    return "Success"


@mcp.tool()
async def long_press_on(
    ctx: Context,
    coordinates: list[int],
    duration: int = 1000,
) -> str:
    """Long presses on the screen at coordinates.

    'coordinates' is a list [x, y].
    'duration' is in milliseconds (default 1000).
    """
    try:
        controller = _get_controller()
    except Exception as e:
        return f"Error: Controller lazy initialization failed: {e}"

    if len(coordinates) != 2:
        return "Error: coordinates must be [x, y]"

    result = await controller.tap_at(
        x=coordinates[0],
        y=coordinates[1],
        long_press=True,
        long_press_duration=duration,
    )

    if result.error:
        return f"Error: {result.error}"
    return "Success"


@mcp.tool()
async def swipe(
    ctx: Context,
    coordinates: list[int],
    duration: int = 400,
) -> str:
    """Swipes from start coordinates to end coordinates.

    'coordinates' is a list [start_x, start_y, end_x, end_y].
    'duration' is in milliseconds (default 400).

    Set duration >= 1000 to drag-and-drop. Drag slightly past the target
    position to trigger reordering.
    """
    try:
        controller = _get_controller()
    except Exception as e:
        return f"Error: Controller lazy initialization failed: {e}"

    if len(coordinates) != 4:
        return "Error: coordinates must be [start_x, start_y, end_x, end_y]"

    error = await controller.swipe_coords(
        start_x=coordinates[0],
        start_y=coordinates[1],
        end_x=coordinates[2],
        end_y=coordinates[3],
        duration=duration,
    )
    if error:
        return f"Error: {error}"
    return "Success"


@mcp.tool()
async def back(ctx: Context) -> str:
    """Simulates pressing the system back button."""
    try:
        controller = _get_controller()
    except Exception as e:
        return f"Error: Controller lazy initialization failed: {e}"

    success = await controller.go_back()
    return "Success" if success else "Failed"


@mcp.tool()
async def launch_app(ctx: Context, package_name: str) -> str:
    """Launches an application by its Android package name with retries and smart polling."""
    try:
        controller = _get_controller()
    except Exception as e:
        return f"Error: Controller lazy initialization failed: {e}"

    success, error_msg = await launch_app_with_retries(controller.ctx, package_name)
    return "Success" if success else f"Failed: {error_msg}"


@mcp.tool()
async def stop_app(ctx: Context, package_name: str) -> str:
    """Force stops an application by its Android package name."""
    try:
        controller = _get_controller()
    except Exception as e:
        return f"Error: Controller lazy initialization failed: {e}"

    success = await controller.terminate_app(package_name)
    return "Success" if success else "Failed"


@mcp.tool()
async def open_link(ctx: Context, url: str) -> str:
    """Opens a URL or deep link on the device."""
    try:
        controller = _get_controller()
    except Exception as e:
        return f"Error: Controller lazy initialization failed: {e}"

    success = await controller.open_url(url)
    return "Success" if success else "Failed"


@mcp.tool()
async def focus_and_input_text(
    ctx: Context,
    coordinates: list[int],
    text: str,
    clear_before_input: bool = False,
) -> str:
    """Focuses on a UI element at coordinates and inputs text.

    'coordinates' is a list [x, y] to tap first to gain focus.
    'clear_before_input' if True, clears all existing text before typing. If False, appends text at the end of existing content.
    'text' supports multi-line content with '\\n'.
    """
    try:
        controller = _get_controller()
    except Exception as e:
        return f"Error: Controller lazy initialization failed: {e}"

    if len(coordinates) != 2:
        return "Error: coordinates must be [x, y]"

    err = await _ensure_focus_at_coords(controller, coordinates[0], coordinates[1])
    if err:
        return f"Error focusing element: {err}"

    if clear_before_input:
        success = await controller.erase_text()
        if not success:
            return "Failed to clear existing text"
    else:
        # Move cursor to the end for reliable append
        await controller.press_key("123")

    success = await controller.type_text(text, clear_existing=False)
    return "Success" if success else "Failed"


@mcp.tool()
async def focus_and_clear_text(
    ctx: Context,
    coordinates: list[int],
) -> str:
    """Focuses on a UI element at coordinates and clears its text content.

    'coordinates' is a list [x, y] to tap first to gain focus.
    """
    try:
        controller = _get_controller()
    except Exception as e:
        return f"Error: Controller lazy initialization failed: {e}"

    if len(coordinates) != 2:
        return "Error: coordinates must be [x, y]"

    err = await _ensure_focus_at_coords(controller, coordinates[0], coordinates[1])
    if err:
        return f"Error focusing element: {err}"

    success = await controller.erase_text()
    return "Success" if success else "Failed"


@mcp.tool()
async def erase_one_char(ctx: Context) -> str:
    """Erases a single character (simulates Backspace)."""
    try:
        controller = _get_controller()
    except Exception as e:
        return f"Error: Controller lazy initialization failed: {e}"

    success = await controller.erase_text(nb_chars=1)
    return "Success" if success else "Failed"


@mcp.tool()
async def press_key(ctx: Context, keycode: str) -> str:
    """Presses a specific Android key event (e.g., KEYCODE_ENTER, KEYCODE_HOME)."""
    try:
        controller = _get_controller()
    except Exception as e:
        return f"Error: Controller lazy initialization failed: {e}"

    try:
        success = await controller.press_key(keycode)
        return "Success" if success else "Failed"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
async def take_screenshot(ctx: Context) -> str:
    """Takes a screenshot of the device screen.

    Returns the screenshot as a base64 encoded JPEG string.
    """
    try:
        controller = _get_controller()
    except Exception as e:
        return f"Error: Controller lazy initialization failed: {e}"

    try:
        screenshot_b64 = await controller.take_screenshot()
        return screenshot_b64
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
async def get_ui_hierarchy(ctx: Context) -> str:
    """Retrieves the current UI elements hierarchy from the device."""
    try:
        controller = _get_controller()
    except Exception as e:
        logger.exception("Failed to initialize controller in get_ui_hierarchy")
        raise e

    try:
        elements = await controller.get_ui_elements()
        return json.dumps(elements)
    except Exception as e:
        logger.exception("Failed to get UI elements in get_ui_hierarchy")
        raise e


if __name__ == "__main__":
    from artemis.runtime import shutdown_awake_service, start_awake_service

    start_awake_service()
    try:
        mcp.run(transport="stdio")
    finally:
        shutdown_awake_service()
