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

"""Concrete Android Device Driver implementation using ADB and UIAutomator2."""

import asyncio
import base64
from io import BytesIO
from pathlib import Path
from typing import Any, Literal

from adbutils import AdbClient, AdbDevice
from artemis.clients.ui_automator_client import (
    UIAutomatorClient,
    _parse_hierarchy_xml_to_elements,
)
from artemis.config.paths import get_temp_dir
from artemis.drivers.base import BaseDeviceDriver, KeyCode, ScreenData, SwipeDirection
from artemis.toolchain import find_ffmpeg, find_scrcpy
from artemis.utils.video import build_scrcpy_record_command
from artemis.utils.ui_filter import filter_ui_hierarchy
from artemis.utils.logger import get_logger

logger = get_logger(__name__)

# KeyCode mapping from string name to Android keycode integer
ANDROID_KEYCODE_MAP: dict[str, int] = {
    "home": 3,
    "back": 4,
    "enter": 66,
    "delete": 67,
    "power": 26,
    "app_switch": 187,
    "volume_up": 24,
    "volume_down": 25,
}


def _escape_for_adb_text(s: str) -> str:
    """Escapes special characters for adb shell input text."""
    return (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("'", "\\'")
        .replace("`", "\\`")
        .replace("$", "\\$")
        .replace("&", "\\&")
        .replace("|", "\\|")
        .replace(";", "\\;")
        .replace("<", "\\<")
        .replace(">", "\\>")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .replace("*", "\\*")
        .replace("?", "\\?")
        .replace("~", "\\~")
        .replace(" ", "%s")
    )


class AndroidAdbDriver(BaseDeviceDriver):
    """Production-grade Android Device Driver powered by ADB and UIAutomator2."""

    def __init__(
        self,
        device_id: str,
        adb_client: AdbClient,
        ui_adb_client: UIAutomatorClient | None = None,
        width: int = 1080,
        height: int = 2400,
    ):
        self._device_id = device_id
        self._adb_client = adb_client
        self._ui_adb_client = ui_adb_client
        self._width = width
        self._height = height
        self._device: AdbDevice | None = None
        self._recording_process: asyncio.subprocess.Process | None = None
        self._recording_output_path: Path | None = None

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def screen_size(self) -> tuple[int, int]:
        return (self._width, self._height)

    @property
    def device(self) -> AdbDevice:
        if self._device is None:
            self._device = self._adb_client.device(serial=self._device_id)
        return self._device

    async def connect(self) -> None:
        """Verify device availability."""
        try:
            state = self.device.get_state()
            logger.info(f"Android device '{self._device_id}' connected (state: {state})")
        except Exception as e:
            logger.warning(f"Connecting to Android device '{self._device_id}': {e}")

    async def disconnect(self) -> None:
        """Clean up ongoing recordings and tunnels."""
        if self._recording_process:
            await self.stop_video_recording()
        if self._ui_adb_client and hasattr(self._ui_adb_client, "disconnect"):
            await asyncio.to_thread(self._ui_adb_client.disconnect)

    async def get_screen_data(self, skip_settling: bool = False) -> ScreenData:
        """Captures screenshot and XML hierarchy concurrently."""
        if not skip_settling:
            await asyncio.sleep(0.3)

        # 1. Capture raw screenshot
        screenshot_bytes: bytes = b""
        screenshot_base64: str = ""
        ui_hierarchy_xml: str | None = None
        ui_elements: list[dict[str, Any]] = []

        # Check if ui_adb_client has direct screen data support
        if self._ui_adb_client and hasattr(self._ui_adb_client, "get_screen_data"):
            try:
                raw_ui_data = self._ui_adb_client.get_screen_data()
                if (
                    hasattr(raw_ui_data, "base64")
                    and isinstance(raw_ui_data.base64, str)
                    and raw_ui_data.base64
                ):
                    screenshot_base64 = raw_ui_data.base64
                    screenshot_bytes = base64.b64decode(screenshot_base64)
                if hasattr(raw_ui_data, "width") and isinstance(raw_ui_data.width, int):
                    self._width = raw_ui_data.width
                if hasattr(raw_ui_data, "height") and isinstance(raw_ui_data.height, int):
                    self._height = raw_ui_data.height
                if hasattr(raw_ui_data, "hierarchy_xml") and isinstance(
                    raw_ui_data.hierarchy_xml, str
                ):
                    ui_hierarchy_xml = raw_ui_data.hierarchy_xml
                if hasattr(raw_ui_data, "elements"):
                    raw_elements = raw_ui_data.elements
                    if isinstance(raw_elements, dict):
                        ui_elements = [raw_elements]
                    elif isinstance(raw_elements, list):
                        ui_elements = raw_elements
            except Exception as e:
                logger.debug(f"Direct ui_adb_client.get_screen_data error: {e}")

        if not screenshot_base64 or not isinstance(screenshot_base64, str):
            try:
                pil_img = await asyncio.to_thread(self.device.screenshot)
                if hasattr(pil_img, "save"):
                    buf = BytesIO()
                    pil_img.save(buf, format="PNG")
                    screenshot_bytes = buf.getvalue()
                    self._width, self._height = pil_img.size
                    screenshot_base64 = base64.b64encode(screenshot_bytes).decode("utf-8")
            except Exception as e:
                logger.warning(f"Device screenshot capture failed on {self._device_id}: {e}")

        if not screenshot_base64 or not isinstance(screenshot_base64, str):
            # Fallback for headless testing environments
            screenshot_base64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
            screenshot_bytes = base64.b64decode(screenshot_base64)

        # 2. Capture UI Hierarchy via UIAutomator
        if self._ui_adb_client and not ui_elements:
            try:
                if hasattr(self._ui_adb_client, "get_hierarchy"):
                    res = self._ui_adb_client.get_hierarchy()
                    hierarchy = await res if asyncio.iscoroutine(res) else res
                    if isinstance(hierarchy, str):
                        ui_hierarchy_xml = hierarchy
                        ui_elements = _parse_hierarchy_xml_to_elements(hierarchy)
                    else:
                        ui_elements = hierarchy
                elif hasattr(self._ui_adb_client, "get_ui_elements"):
                    res = self._ui_adb_client.get_ui_elements()
                    ui_elements = await res if asyncio.iscoroutine(res) else res
            except Exception as e:
                logger.debug(f"UI hierarchy extraction failed: {e}")

        if isinstance(ui_elements, dict):
            clean_ui_elements = [ui_elements]
        elif isinstance(ui_elements, list):
            clean_ui_elements = ui_elements
        else:
            clean_ui_elements = []

        w = self._width if isinstance(self._width, int) else 1080
        h = self._height if isinstance(self._height, int) else 2400
        clean_ui_elements = filter_ui_hierarchy(
            clean_ui_elements,
            screen_width=w,
            screen_height=h,
        )
        clean_b64 = (
            screenshot_base64
            if isinstance(screenshot_base64, str) and screenshot_base64
            else "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        )
        clean_bytes = (
            screenshot_bytes
            if isinstance(screenshot_bytes, bytes) and screenshot_bytes
            else base64.b64decode(clean_b64)
        )

        return ScreenData(
            screenshot_bytes=clean_bytes,
            screenshot_base64=clean_b64,
            ui_hierarchy_xml=ui_hierarchy_xml,
            ui_elements=clean_ui_elements,
            width=w,
            height=h,
            platform="android",
        )

    async def tap(
        self,
        x: int,
        y: int,
        duration_ms: int = 100,
        times: int = 1,
        delay_ms: int = 100,
    ) -> bool:
        try:
            if duration_ms >= 500:
                cmd = f"input swipe {x} {y} {x} {y} {duration_ms}"
            else:
                if times <= 1:
                    cmd = f"input tap {x} {y}"
                else:
                    taps = [f"input tap {x} {y}"] * times
                    cmd = f" && sleep {delay_ms / 1000.0:.3f} && ".join(taps)

            logger.info(f"[ADB] {cmd}")
            await asyncio.to_thread(self.device.shell, cmd)
            return True
        except Exception as e:
            logger.error(f"Tap failed at ({x}, {y}): {e}")
            return False

    async def long_press(self, x: int, y: int, duration_ms: int = 1000) -> bool:
        return await self.tap(x=x, y=y, duration_ms=duration_ms)

    async def swipe(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration_ms: int = 800,
    ) -> bool:
        try:
            cmd = f"input swipe {start_x} {start_y} {end_x} {end_y} {duration_ms}"
            logger.info(f"[ADB] {cmd}")
            await asyncio.to_thread(self.device.shell, cmd)
            return True
        except Exception as e:
            logger.error(f"Swipe failed from ({start_x},{start_y}) to ({end_x},{end_y}): {e}")
            return False

    async def swipe_direction(
        self,
        direction: SwipeDirection | Literal["up", "down", "left", "right"],
        duration_ms: int = 800,
    ) -> bool:
        dir_str = str(direction).lower()
        mid_x = int(
            self._width * 0.6
        )  # Lock to 60% width to avoid edge gestures and alphabet fast-scroll sidebar
        mid_y = self._height // 2

        if dir_str == "up":
            # Drag bottom to top -> scrolls down (0.7 -> 0.3 leaves ~50-60% overlap, 800ms prevents fling)
            return await self.swipe(
                mid_x, int(self._height * 0.7), mid_x, int(self._height * 0.3), duration_ms
            )
        elif dir_str == "down":
            # Drag top to bottom -> scrolls up
            return await self.swipe(
                mid_x, int(self._height * 0.3), mid_x, int(self._height * 0.7), duration_ms
            )
        elif dir_str == "left":
            # Drag right to left -> scrolls right
            return await self.swipe(
                int(self._width * 0.75), mid_y, int(self._width * 0.25), mid_y, duration_ms
            )
        elif dir_str == "right":
            # Drag left to right -> scrolls left
            return await self.swipe(
                int(self._width * 0.25), mid_y, int(self._width * 0.75), mid_y, duration_ms
            )
        return False

    async def input_text(self, text: str, clear_existing: bool = True) -> bool:
        try:
            if clear_existing:
                # Safe & robust clearing: Move to End -> Shift+Home selection -> Delete -> Fallback backspaces
                clear_cmd = (
                    "input keyevent 123 && "
                    "input keyevent --meta 1 122 && "
                    "input keyevent 67 && "
                    "input keyevent 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67"
                )
                await asyncio.to_thread(self.device.shell, clear_cmd)
            else:
                # Append mode: move cursor to the very end of existing text
                await asyncio.to_thread(
                    self.device.shell,
                    "input keyevent 123",  # KEYCODE_MOVE_END
                )

            # Normalize literal escaped newlines from LLM / tool call serialization
            norm_text = (
                text.replace(r"\r\n", "\n")
                .replace(r"\n", "\n")
                .replace(r"\r", "\n")
            )

            # 1. Tier 1: Try clipboard injection + KEYCODE_PASTE (Zero IME interference, preserves multiline, works for all charsets)
            if self._ui_adb_client:
                try:
                    set_clip_ok = False
                    if hasattr(self._ui_adb_client, "set_clipboard"):
                        set_clip_ok = self._ui_adb_client.set_clipboard(norm_text)
                    elif hasattr(self._ui_adb_client, "_device") and self._ui_adb_client._device:
                        self._ui_adb_client._device.set_clipboard(norm_text)
                        set_clip_ok = True

                    if set_clip_ok:
                        await asyncio.to_thread(self.device.shell, "input keyevent 279")
                        return True
                except Exception as e:
                    logger.debug(f"Clipboard paste fallback to ADB input: {e}")

            # 2. Tier 2: Check if ADBKeyboard is currently active
            try:
                default_ime = await asyncio.to_thread(
                    self.device.shell, "settings get secure default_input_method"
                )
                if "adbkeyboard" in str(default_ime).lower():
                    b64_text = base64.b64encode(norm_text.encode("utf-8")).decode("utf-8")
                    broadcast_cmd = f"am broadcast -a ADB_INPUT_B64 --es msg '{b64_text}'"
                    await asyncio.to_thread(self.device.shell, broadcast_cmd)
                    return True
            except Exception:
                pass

            # 3. Tier 3: Universal Native ADB input text fallback
            lines = norm_text.split("\n")
            for i, line in enumerate(lines):
                if i > 0:
                    # Send Enter key between lines
                    await asyncio.to_thread(self.device.shell, "input keyevent 66")
                if line:
                    escaped = _escape_for_adb_text(line)
                    await asyncio.to_thread(self.device.shell, f"input text {escaped}")
            return True
        except Exception as e:
            logger.error(f"Input text failed for '{text}': {e}")
            return False

    async def press_key(self, key: KeyCode | str | int) -> bool:
        try:
            keycode_val = key
            if isinstance(key, (KeyCode, str)):
                key_name = str(key).lower().replace("keycode.", "")
                keycode_val = ANDROID_KEYCODE_MAP.get(key_name, key)

            await asyncio.to_thread(self.device.shell, f"input keyevent {keycode_val}")
            return True
        except Exception as e:
            logger.error(f"Press key failed for '{key}': {e}")
            return False

    async def launch_app(self, package_name: str) -> bool:
        try:
            cmd = f"monkey -p {package_name} -c android.intent.category.LAUNCHER 1"
            await asyncio.to_thread(self.device.shell, cmd)
            return True
        except Exception as e:
            logger.error(f"Launch app failed for '{package_name}': {e}")
            return False

    async def stop_app(self, package_name: str) -> bool:
        try:
            await asyncio.to_thread(self.device.shell, f"am force-stop {package_name}")
            return True
        except Exception as e:
            logger.error(f"Stop app failed for '{package_name}': {e}")
            return False

    async def get_current_package(self) -> str | None:
        try:
            # 1. Try modern current_app from adbutils
            if hasattr(self.device, "current_app"):
                try:
                    app_info = await asyncio.to_thread(self.device.current_app)
                    if app_info and getattr(app_info, "package", None):
                        return app_info.package
                except Exception:
                    pass

            # 2. Structured dumpsys extraction
            out = await asyncio.to_thread(
                self.device.shell, "dumpsys window displays | grep -E 'mCurrentFocus|mFocusedApp'"
            )
            for line in str(out).splitlines():
                if "/" in line:
                    for token in line.split():
                        if "/" in token and "." in token:
                            clean = token.split("/")[0].strip("{} ,")
                            if clean and not clean.startswith("Window") and "." in clean:
                                return clean
        except Exception as e:
            logger.debug(f"Error querying current package: {e}")
        return None

    async def execute_shell(self, command: str, timeout_seconds: float = 15.0) -> str:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self.device.shell, command),
                timeout=timeout_seconds,
            )
        except Exception as e:
            return f"Error: {e}"

    async def start_video_recording(self, output_dir: Path | None = None) -> None:
        """Starts screen recording via scrcpy in background."""
        out_dir = output_dir or get_temp_dir("recordings")
        out_dir.mkdir(parents=True, exist_ok=True)
        self._recording_mkv_path = out_dir / "recording.mkv"
        self._recording_output_path = out_dir / "recording.mp4"
        logger.info(f"Starting scrcpy video recording to {self._recording_mkv_path}...")
        try:
            scrcpy_bin = find_scrcpy()
            cmd = build_scrcpy_record_command(
                scrcpy_bin,
                self.device_id,
                self._recording_mkv_path,
                lock_capture_orientation=False,
            )
            self._scrcpy_process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.warning(f"Failed to start scrcpy subprocess: {e}")

    async def stop_video_recording(self) -> str | None:
        """Stops background video capture and returns local recording file path."""
        if hasattr(self, "_scrcpy_process") and self._scrcpy_process:
            try:
                self._scrcpy_process.terminate()
                await asyncio.wait_for(self._scrcpy_process.wait(), timeout=5.0)
            except Exception:
                pass
            self._scrcpy_process = None

        mkv = getattr(self, "_recording_mkv_path", None)
        mp4 = getattr(self, "_recording_output_path", None)

        if mkv and mkv.exists() and mp4:
            try:
                proc = await asyncio.create_subprocess_exec(
                    find_ffmpeg(),
                    "-y",
                    "-i",
                    str(mkv),
                    "-c",
                    "copy",
                    "-movflags",
                    "+faststart",
                    str(mp4),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.wait()
                if mp4.exists() and mp4.stat().st_size > 0:
                    try:
                        mkv.unlink()
                    except Exception:
                        pass
                    return str(mp4)
            except Exception as e:
                logger.warning(f"Failed to convert MKV to MP4 in AdbDriver: {e}")

        if mp4 and mp4.exists():
            return str(mp4)
        if mkv and mkv.exists():
            return str(mkv)
        return None
