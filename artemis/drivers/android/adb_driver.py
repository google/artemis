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
import re
from io import BytesIO
from pathlib import Path
from typing import Any, Literal

from adbutils import AdbClient, AdbDevice
from artemis.config.paths import get_temp_dir
from artemis.drivers.base import BaseDeviceDriver, KeyCode, ScreenData, SwipeDirection
from artemis.toolchain import find_ffmpeg, find_scrcpy
from artemis.utils.video import build_scrcpy_record_command
from artemis.utils.ui_filter import filter_ui_hierarchy
from third_party.mobile_use.clients.ui_automator_client import (
    UIAutomatorClient,
    parse_hierarchy_xml_to_elements,
)
from third_party.mobile_use.utils.logger import get_logger

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
    """Android device driver using ADB and UIAutomator2."""

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
        # Lazily built clipboard-capable client. ``_ui_adb_client`` is only
        # supplied by the MCP/cloud paths; on a plain local run it is None, and
        # the clipboard tier in input_text() would silently be skipped so every
        # entry fell through to `input text`. This is created on first use.
        self._input_client: Any = None
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
                        ui_elements = parse_hierarchy_xml_to_elements(hierarchy)
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

    async def _get_input_client(self) -> Any:
        """Return a client that can set the clipboard / set text, or None.

        Prefers the injected UI client, then any client already resolved by the
        helper/uiautomator factory. The factory client routes through the
        Artemis Accessibility Helper, which can write the system clipboard on
        every API level (reads are the restricted operation since Android 10).
        """
        if self._ui_adb_client is not None:
            return self._ui_adb_client
        if self._input_client is not None:
            return self._input_client
        try:
            from artemis.clients.screen_client_factory import create_screen_client

            self._input_client = await asyncio.to_thread(
                create_screen_client, self._device_id
            )
        except Exception as e:
            logger.debug(f"Could not build a clipboard-capable client: {e}")
            self._input_client = None
        return self._input_client

    async def _clipboard_paste(self, text: str) -> bool:
        """Set the clipboard and paste with KEYCODE_PASTE. True on confirmed paste."""
        client = await self._get_input_client()
        if client is None:
            return False

        # The first RPC after a cold start can lose the race with the helper's
        # backend attach / forward setup and return False even though the
        # helper is healthy. Retry once after a beat before giving up, otherwise
        # a transient miss silently drops us onto the input-text path.
        set_ok = False
        for attempt in range(2):
            try:
                ok = False
                if hasattr(client, "set_clipboard"):
                    res = client.set_clipboard(text)
                    if asyncio.iscoroutine(res):
                        res = await res
                    ok = bool(res)
                elif hasattr(client, "_device") and getattr(client, "_device", None):
                    res = client._device.set_clipboard(text)
                    if asyncio.iscoroutine(res):
                        res = await res
                    ok = True
                set_ok = ok
            except Exception as e:
                logger.debug(f"set_clipboard attempt {attempt + 1} failed: {e}")
                set_ok = False
            if set_ok:
                break
            if attempt == 0:
                # Nudge the backend to resolve/attach before retrying.
                try:
                    if hasattr(client, "get_hierarchy"):
                        res = client.get_hierarchy()
                        if asyncio.iscoroutine(res):
                            await res
                except Exception:
                    pass
                await asyncio.sleep(1.0)

        if not set_ok:
            return False

        try:
            await asyncio.to_thread(self.device.shell, "input keyevent 279")
        except Exception as e:
            logger.debug(f"KEYCODE_PASTE failed: {e}")
            return False
        # Give the IME a beat to drain the paste before we inspect the field.
        await asyncio.sleep(0.35)
        return await self._verify_focused_text(text)

    async def _verify_focused_text(self, expected: str) -> bool:
        """Best-effort check that ``expected`` is present in the focused field.

        Returns True when the text is confirmed, and also when the field cannot
        be inspected (masked password fields, custom WebViews, no editable node
        in the dump) - we only fail when we can positively see that nothing
        landed. That keeps masked fields working while still stopping the
        silent-failure loop that made the agent retry the same entry forever.
        """
        try:
            client = await self._get_input_client()
            if client is None or not hasattr(client, "get_hierarchy"):
                return True
            xml = client.get_hierarchy()
            if asyncio.iscoroutine(xml):
                xml = await xml
            if not isinstance(xml, str) or not xml:
                return True

            # Collect every focused editable node; React Native and Compose
            # routinely emit several, and only one of them is the real field.
            editable: list[str] = []
            for match in re.finditer(r"<node\b[^>]*>", xml):
                node = match.group(0)
                if 'focused="true"' not in node:
                    continue
                is_editable = (
                    "EditText" in node
                    or 'class="android.widget.EditText"' in node
                    or 'editable="true"' in node
                )
                if not is_editable:
                    continue
                text_attr = re.search(r'\stext="([^"]*)"', node)
                editable.append(text_attr.group(1) if text_attr else "")

            if not editable:
                # No focused editable node found -> cannot judge; assume success.
                return True

            probe = expected.replace("\n", "").strip()
            if not probe:
                return True

            for current in editable:
                if not current:
                    # An empty focused field proves nothing either way; keep
                    # looking rather than matching "" as a substring.
                    continue
                # password variants render dots; a mismatch there is not evidence.
                if set(current) <= {"•", "*", "●", "."}:
                    return True
                if probe in current or current in probe:
                    return True
            # Every focused editable field was empty while we expected content.
            return False
        except Exception as e:
            logger.debug(f"Focused-text verification skipped: {e}")
            return True

    async def input_text(self, text: str, clear_existing: bool = True) -> bool:
        try:
            # Normalize literal escaped newlines from LLM / tool call serialization
            norm_text = text.replace(r"\r\n", "\n").replace(r"\n", "\n").replace(r"\r", "\n")

            client = await self._get_input_client()

            # 0. Clear through the helper when available - one RPC is more
            #    reliable than 21 synthesized backspaces on custom keyboards.
            cleared = False
            if client is not None and hasattr(client, "clear_text"):
                try:
                    res = client.clear_text()
                    if asyncio.iscoroutine(res):
                        res = await res
                    cleared = bool(res)
                except Exception as e:
                    logger.debug(f"Helper clear_text failed: {e}")

            if not cleared:
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

            # 1. Tier 1: clipboard injection + KEYCODE_PASTE. This is the
            #    IME-free path and is what apps with a custom keypad (e.g. a
            #    phone-number field that ignores synthesized input events)
            #    actually accept.
            if await self._clipboard_paste(norm_text):
                return True
            logger.debug(
                "Clipboard paste did not take; falling back to native input text."
            )

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
            except Exception as e:
                # ADBKeyboard probe/broadcast failed; fall through to native input.
                logger.debug(f"ADBKeyboard IME path failed, falling back to ADB input: {e}")

            # 3. Tier 3: Universal Native ADB input text fallback
            lines = norm_text.split("\n")
            for i, line in enumerate(lines):
                if i > 0:
                    # Send Enter key between lines
                    await asyncio.to_thread(self.device.shell, "input keyevent 66")
                if line:
                    escaped = _escape_for_adb_text(line)
                    await asyncio.to_thread(self.device.shell, f"input text {escaped}")
            await asyncio.sleep(0.35)
            return await self._verify_focused_text(norm_text)
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
                except Exception as e:
                    # current_app is unsupported on some devices; use dumpsys below.
                    logger.debug(f"current_app query failed, falling back to dumpsys: {e}")

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
            except (TimeoutError, ProcessLookupError, OSError) as e:
                # Already exited, or did not stop within the timeout; a lingering
                # scrcpy may keep the MKV file locked on Windows.
                logger.debug(f"scrcpy process did not terminate cleanly: {e}")
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
                    except OSError:
                        # Best-effort cleanup of the intermediate MKV file.
                        pass
                    return str(mp4)
            except Exception as e:
                logger.warning(f"Failed to convert MKV to MP4 in AdbDriver: {e}")

        if mp4 and mp4.exists():
            return str(mp4)
        if mkv and mkv.exists():
            return str(mkv)
        return None
