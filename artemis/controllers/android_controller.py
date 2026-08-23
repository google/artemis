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
import base64
from io import BytesIO
from pathlib import Path
import re
import tempfile
import time
from typing import Any
from uuid import uuid4

from adbutils import AdbClient, AdbDevice
from PIL import Image

from artemis.clients.ui_automator_client import (
    UIAutomatorClient,
    _parse_hierarchy_xml_to_elements,
)
from artemis.config import get_temp_dir
from artemis.controllers.device_controller import (
    MobileDeviceController,
    ScreenDataResponse,
)
from artemis.controllers.types import (
    Bounds,
    CoordinatesSelectorRequest,
    TapOutput,
)
from artemis.data_engine.models import VideoRecordingRecord
from artemis.utils.logger import get_logger
from artemis.utils.video import (
    DEFAULT_MAX_DURATION_SECONDS,
    RecordingSession,
    VideoRecordingResult,
    build_scrcpy_record_command,
    cleanup_video_segments,
    concatenate_videos,
    get_active_session,
    has_active_session,
    normalize_recording_to_mp4,
    remove_active_session,
    set_active_session,
    trim_video,
)

logger = get_logger(__name__)


class AndroidDeviceController(MobileDeviceController):
    def __init__(
        self,
        device_id: str,
        adb_client: AdbClient,
        ui_adb_client: UIAutomatorClient,
        device_width: int,
        device_height: int,
        data_engine_start_time: float | None = None,
        data_engine: Any | None = None,
    ):
        self.device_id = device_id
        self.adb_client = adb_client
        self.ui_adb_client = ui_adb_client
        self.device_width = device_width
        self.device_height = device_height
        self.data_engine_start_time = data_engine_start_time
        self.data_engine = data_engine
        self._device: AdbDevice | None = None
        self._segment_cache: dict[tuple[float, float | None], VideoRecordingResult] = {}

    @property
    def device(self) -> AdbDevice:
        if self._device is None:
            self._device = self.adb_client.device(serial=self.device_id)
        return self._device

    async def tap(
        self,
        coords: CoordinatesSelectorRequest,
        long_press: bool = False,
        long_press_duration: int = 1000,
        times: int = 1,
        delay_ms: int = 100,
    ) -> TapOutput:
        try:
            if long_press:
                cmd = (
                    f"input swipe {coords.x} {coords.y} {coords.x} {coords.y} {long_press_duration}"
                )
            else:
                if times <= 1:
                    cmd = f"input tap {coords.x} {coords.y}"
                else:
                    # Chain multiple taps in a single shell command to eliminate shell spawning overhead
                    # between rapid consecutive clicks (e.g. 7 taps to enter developer mode)
                    taps = [f"input tap {coords.x} {coords.y}"] * times
                    cmd = f" && sleep {delay_ms / 1000.0:.3f} && ".join(taps)

            logger.info(f"Executing ADB shell: {cmd}")
            self.device.shell(cmd)
            return TapOutput(error=None)
        except Exception as e:
            return TapOutput(error=f"ADB tap failed: {str(e)}")

    async def swipe(
        self,
        start: CoordinatesSelectorRequest,
        end: CoordinatesSelectorRequest,
        duration: int = 400,
    ) -> str | None:
        try:
            cmd = f"input touchscreen swipe {start.x} {start.y} {end.x} {end.y} {duration}"
            logger.info(f"Executing ADB command: {cmd}")
            self.device.shell(cmd)
            return None
        except Exception as e:
            return f"ADB swipe failed: {str(e)}"

    async def get_screen_data(self) -> ScreenDataResponse:
        """Get screen data using the UIAutomator2 client"""
        try:
            logger.info("Using UIAutomator2 for screen data retrieval")
            ui_data = self.ui_adb_client.get_screen_data()
            return ScreenDataResponse(
                base64=ui_data.base64,
                elements=ui_data.elements,
                width=ui_data.width,
                height=ui_data.height,
                platform="android",
            )
        except Exception as e:
            logger.error(f"Failed to get screen data: {e}")
            raise

    async def screenshot(self) -> str:
        try:
            b64 = self.ui_adb_client.get_screenshot_base64()
            if b64 is None:
                raise RuntimeError("Failed to capture screenshot")
            return b64
        except Exception as e:
            logger.error(f"Failed to take screenshot: {e}")
            raise

    async def input_text(self, text: str) -> bool:
        try:
            # Fast path for ASCII text: Use ADB directly.
            # This avoids toggling FastInputIME, which hides the keyboard and can cause focus loss.
            is_ascii = all(ord(c) < 128 for c in text)
            if is_ascii:
                return self._input_text_adb_fallback(text)

            self.ui_adb_client.send_text(text)
            return True
        except Exception as e:
            logger.warning(f"UIAutomator2 send_text failed: {e}, falling back to ADB shell")
            return self._input_text_adb_fallback(text)

    def _input_text_adb_fallback(self, text: str) -> bool:
        """Fallback method using ADB shell input text command."""
        try:
            lines = text.split("\n")
            for line_idx, line in enumerate(lines):
                if line_idx > 0:
                    self.device.shell("input keyevent 66")
                if not line:
                    continue
                parts = line.split("%s")
                for i, part in enumerate(parts):
                    to_write = ""
                    for char in part:
                        if char == " ":
                            to_write += "%s"
                        elif char in [
                            "&",
                            "<",
                            ">",
                            "|",
                            ";",
                            "(",
                            ")",
                            "$",
                            "`",
                            "\\",
                            '"',
                            "'",
                        ]:
                            to_write += f"\\{char}"
                        else:
                            to_write += char

                    if to_write:
                        self.device.shell(f"input text '{to_write}'")

                    if i < len(parts) - 1:
                        self.device.shell("input keyevent 62")

            return True
        except Exception as e:
            logger.error(f"Failed to input text via ADB fallback: {e}")
            return False

    async def launch_app(self, package_or_bundle_id: str) -> bool:
        try:
            # Clear foreground obstructions before launching
            logger.info(
                "Clearing foreground obstructions (status bar, system dialogs,"
                " keyboard) before launching app..."
            )
            try:
                # 1. Collapse status bar / notification shade
                self.device.shell("cmd statusbar collapse")
            except Exception as e:
                logger.warning(f"Failed to collapse status bar: {e}")

            try:
                # 2. Close system dialogs
                self.device.shell("am broadcast -a android.intent.action.CLOSE_SYSTEM_DIALOGS")
            except Exception as e:
                logger.warning(f"Failed to close system dialogs: {e}")

            # 3. Dismiss keyboard if visible
            if self.is_keyboard_visible():
                await self.dismiss_keyboard()

            self.device.app_start(package_or_bundle_id)
            return True
        except Exception as e:
            logger.error(f"Failed to launch app {package_or_bundle_id}: {e}")
            return False

    async def terminate_app(self, package_or_bundle_id: str | None) -> bool:
        try:
            if package_or_bundle_id is None:
                current_app = self._get_current_foreground_package()
                if current_app:
                    logger.info(f"Stopping currently running app: {current_app}")
                    self.device.app_stop(current_app)
                else:
                    logger.warning("No foreground app detected")
                    return False
            else:
                self.device.app_stop(package_or_bundle_id)
            return True
        except Exception as e:
            logger.error(f"Failed to terminate app {package_or_bundle_id}: {e}")
            return False

    async def open_url(self, url: str) -> bool:
        try:
            cmd = f"am start -a android.intent.action.VIEW -d {url}"
            logger.info(f"Executing ADB shell: {cmd}")
            self.device.shell(cmd)
            return True
        except Exception as e:
            logger.error(f"Failed to open URL {url}: {e}")
            return False

    def is_keyboard_visible(self) -> bool:
        try:
            res = self.device.shell("dumpsys input_method")
            if isinstance(res, bytes):
                res = res.decode("utf-8")

            # Common patterns for open keyboard in dumpsys
            patterns = [
                "mInputShown=true",
                "mInputViewShowing=true",
                "mInputViewShowing=VISIBLE",
            ]
            for p in patterns:
                if p in res:
                    return True

            # Alternate check using dumpsys window
            res_win = self.device.shell(
                "dumpsys window | grep -E 'mInputMethodWindow|mShowingInputMethod'"
            )
            if isinstance(res_win, bytes):
                res_win = res_win.decode("utf-8")
            if "mInputMethodWindow" in res_win and "visible=true" in res_win.lower():
                return True

            return False
        except Exception as e:
            logger.error(f"Failed to check keyboard visibility: {e}")
            return False

    async def dismiss_keyboard(self) -> bool:
        try:
            if not self.is_keyboard_visible():
                return True

            logger.info("Keyboard is visible, attempting to dismiss via Keyevent 111 (Escape)")
            self.device.shell("input keyevent 111")
            await asyncio.sleep(0.3)

            if not self.is_keyboard_visible():
                logger.info("Keyboard successfully dismissed via Keyevent 111")
                return True

            logger.info("Keyboard still visible, trying Keyevent 4 (BACK) as fallback to dismiss")
            self.device.shell("input keyevent 4")
            await asyncio.sleep(0.3)

            if not self.is_keyboard_visible():
                logger.info("Keyboard successfully dismissed via Keyevent 4")
                return True

            logger.warning("Failed to dismiss keyboard after Escape and BACK")
            return False
        except Exception as e:
            logger.error(f"Error in dismiss_keyboard: {e}")
            return False

    async def press_back(self) -> bool:
        try:
            if self.is_keyboard_visible():
                logger.info(
                    "Intercepted press_back: Keyboard is visible. Attempting to"
                    " dismiss keyboard first."
                )
                return await self.dismiss_keyboard()

            logger.info("Executing normal press_back (keyboard not visible)")
            self.device.shell("input keyevent 4")
            return True
        except Exception as e:
            logger.error(f"Failed to press back: {e}")
            return False

    async def press_home(self) -> bool:
        try:
            self.device.shell("input keyevent 3")
            return True
        except Exception as e:
            logger.error(f"Failed to press home: {e}")
            return False

    async def press_enter(self) -> bool:
        try:
            self.device.shell("input keyevent 66")
            return True
        except Exception as e:
            logger.error(f"Failed to press enter: {e}")
            return False

    async def press_key(self, keycode: str) -> bool:
        try:
            # Intercept BACK keycodes to handle keyboard dismissal first
            clean_keycode = str(keycode).strip().upper()
            if clean_keycode in ("4", "KEYCODE_BACK", "BACK"):
                if self.is_keyboard_visible():
                    logger.info(
                        f"Intercepted press_key({keycode}): Keyboard is"
                        " visible. Dismissing keyboard first."
                    )
                    return await self.dismiss_keyboard()

            self.device.shell(f"input keyevent {keycode}")
            return True
        except Exception as e:
            logger.error(f"Failed to press key {keycode}: {e}")
            return False

    async def get_ui_hierarchy(self) -> list[dict]:

        max_attempts = 4
        retry_delay = 0.5

        for attempt in range(1, max_attempts + 1):
            try:
                hierarchy_xml = self.ui_adb_client.get_hierarchy()
                elements = _parse_hierarchy_xml_to_elements(hierarchy_xml)

                # Check if hierarchy contains actual UI elements besides the root node
                if len(elements) > 1:
                    if attempt > 1:
                        logger.info(
                            f"Successfully retrieved non-empty UI hierarchy on attempt {attempt}"
                        )
                    return elements

                logger.warning(
                    "Retrieved empty or root-only UI hierarchy (length"
                    f" {len(elements)}) on attempt {attempt}/{max_attempts}."
                    f" Retrying in {retry_delay}s..."
                )
            except Exception as e:
                logger.warning(
                    f"Failed to get UI hierarchy on attempt {attempt}/{max_attempts}: {e}"
                )
                if attempt == max_attempts:
                    logger.error(f"Failed to get UI hierarchy after {max_attempts} attempts: {e}")
                    return []

            if attempt < max_attempts:
                await asyncio.sleep(retry_delay)

        return []

    def find_element(
        self,
        ui_hierarchy: list[dict],
        resource_id: str | None = None,
        text: str | None = None,
        index: int = 0,
    ) -> tuple[dict | None, Bounds | None, str | None]:
        if not resource_id and not text:
            return None, None, "No resource_id or text provided"

        matches = []
        for element in ui_hierarchy:
            if resource_id and element.get("resource-id") == resource_id:
                matches.append(element)
            elif text and (element.get("text") == text or element.get("accessibilityText") == text):
                matches.append(element)

        if not matches:
            criteria = f"resource_id='{resource_id}'" if resource_id else f"text='{text}'"
            return None, None, f"No element found with {criteria}"

        if index >= len(matches):
            criteria = f"resource_id='{resource_id}'" if resource_id else f"text='{text}'"
            return (
                None,
                None,
                (f"Index {index} out of range for {criteria} (found {len(matches)} matches)"),
            )

        element = matches[index]
        bounds = self._extract_bounds(element)

        return element, bounds, None

    def _get_current_foreground_package(self) -> str | None:
        try:
            app_info = self.device.current_app()
            if app_info and app_info.package:
                return app_info.package
            return None
        except Exception as e:
            logger.error(
                f"Failed to get current foreground package using adbutils: {e}."
                " Falling back to dumpsys parsing."
            )
            try:
                result = self.device.shell("dumpsys window | grep mCurrentFocus")

                # Convert to string if bytes
                if isinstance(result, bytes):
                    result_str = result.decode("utf-8")
                elif isinstance(result, str):
                    result_str = result
                else:
                    return None

                if result_str and "=" in result_str:
                    parts = result_str.split("/")
                    if len(parts) > 0:
                        package = parts[0].split()[-1]
                        return package if package else None
            except Exception as fallback_err:
                logger.error(f"Fallback dumpsys parsing also failed: {fallback_err}")
            return None

    def _extract_bounds(self, element: dict) -> Bounds | None:
        bounds_str = element.get("bounds")
        if not bounds_str or not isinstance(bounds_str, str):
            return None

        try:
            match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str)
            if match:
                return Bounds(
                    x1=int(match.group(1)),
                    y1=int(match.group(2)),
                    x2=int(match.group(3)),
                    y2=int(match.group(4)),
                )
        except (ValueError, IndexError):
            return None

        return None

    async def erase_text(self, nb_chars: int | None = None) -> bool:
        # 1. If clearing the whole field, try fast UIAutomator2 clear first
        if nb_chars is None:
            logger.info("Attempting fast UIAutomator2 clear_text...")
            if self.ui_adb_client.clear_text():
                return True
            logger.warning("UIAutomator2 clear_text failed, falling back to optimized ADB shell")

        # 2. Optimized ADB fallback: combine multiple KEYCODE_DEL keyevents into a single command
        try:
            chars_to_delete = nb_chars if nb_chars is not None else 50
            if chars_to_delete > 0:
                # KEYCODE_DEL is 67. Build arguments list "67 67 67 ..."
                keycode_args = " ".join(["67"] * chars_to_delete)
                cmd = f"input keyevent {keycode_args}"
                logger.info(f"Executing combined ADB shell delete: {cmd}")
                self.device.shell(cmd)
            return True
        except Exception as e:
            logger.error(f"Failed to erase text via ADB fallback: {e}")
            return False

    async def cleanup(self) -> None:
        pass

    def get_compressed_b64_screenshot(self, image_base64: str, quality: int = 50) -> str:
        if image_base64.startswith("data:image"):
            image_base64 = image_base64.split(",")[1]

        image_data = base64.b64decode(image_base64)
        image = Image.open(BytesIO(image_data))

        compressed_io = BytesIO()
        image.save(compressed_io, format="JPEG", quality=quality, optimize=True)

        compressed_base64 = base64.b64encode(compressed_io.getvalue()).decode("utf-8")
        return compressed_base64

    async def start_video_recording(
        self,
        max_duration_seconds: int = DEFAULT_MAX_DURATION_SECONDS,
        output_dir: Path | None = None,
    ) -> VideoRecordingResult:
        """Start screen recording on Android device using scrcpy."""
        self._segment_cache.clear()
        if has_active_session(self.device_id):
            return VideoRecordingResult(
                success=False,
                message=(f"Recording already in progress for device {self.device_id}"),
            )

        try:
            # Create a temp file for the MKV recording if output_dir not provided
            if not output_dir:
                output_dir = Path(
                    tempfile.mkdtemp(prefix="scrcpy_", dir=get_temp_dir("recordings"))
                )
            else:
                output_dir.mkdir(parents=True, exist_ok=True)
            local_video_path = output_dir / "recording.mkv"

            video_id = uuid4()
            session = RecordingSession(
                video_id=video_id,
                device_id=self.device_id,
                start_time=time.time(),
                data_engine_start_time=self.data_engine_start_time,
                local_video_path=local_video_path,
                capture_width=self.device_width,
                capture_height=self.device_height,
            )

            # Persist to local database if Data Engine is active
            if self.data_engine and self.data_engine.storage:
                try:
                    record = VideoRecordingRecord(
                        video_id=video_id,
                        session_id=self.data_engine.current_session_id,
                        device_id=self.device_id,
                        start_time=session.start_time,
                        local_video_path=str(local_video_path),
                    )
                    self.data_engine.storage.create_video_recording(record)
                except Exception as db_err:
                    logger.error(f"Failed to persist video recording start to DB: {db_err}")

            # Start scrcpy in background
            cmd = build_scrcpy_record_command(
                "scrcpy", self.device_id, local_video_path, lock_capture_orientation=False
            )

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            session.process = process
            set_active_session(self.device_id, session)

            logger.info(
                f"Started scrcpy recording on {self.device_id}, saving to {local_video_path}"
            )

            # Wait a bit and check if it's still running.
            await asyncio.sleep(1.0)
            if process.returncode is not None:
                stderr = await process.stderr.read()
                logger.error(f"scrcpy failed to start: {stderr.decode()}")
                remove_active_session(self.device_id)
                return VideoRecordingResult(
                    success=False,
                    message=f"scrcpy failed to start: {stderr.decode()}",
                )

            return VideoRecordingResult(
                success=True,
                message=f"Recording started on {self.device_id}",
            )

        except Exception as e:
            logger.error(f"Failed to start scrcpy recording: {e}")
            remove_active_session(self.device_id)
            return VideoRecordingResult(
                success=False,
                message=f"Failed to start recording: {e}",
            )

    async def stop_video_recording(self) -> VideoRecordingResult:
        """Stop scrcpy recording and return the video file."""
        self._segment_cache.clear()
        session = get_active_session(self.device_id)
        if not session:
            return VideoRecordingResult(
                success=False,
                message=f"No active recording for device {self.device_id}",
            )

        try:
            process = session.process
            if process is not None:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=5.0)

            output_path = session.local_video_path

            # Fix: Concatenate previous segments if scrcpy crashed and restarted
            if session.android_video_segments:
                all_segments = session.android_video_segments + [output_path]
                concatenated_mkv = output_path.parent / "full_recording.mkv"

                if await concatenate_videos(all_segments, concatenated_mkv):
                    cleanup_video_segments(all_segments)
                    output_path = concatenated_mkv

            # Convert MKV to MP4
            mp4_path = (
                output_path.parent / "recording.mp4"
                if output_path.name == "full_recording.mkv"
                else output_path.with_suffix(".mp4")
            )
            success = await self._convert_mkv_to_mp4(output_path, mp4_path)

            remove_active_session(self.device_id)

            final_video_path = mp4_path if (success and mp4_path.exists()) else output_path

            # Persist update to local database if Data Engine is active
            if self.data_engine and self.data_engine.storage:
                try:
                    record = VideoRecordingRecord(
                        video_id=session.video_id,
                        session_id=self.data_engine.current_session_id,
                        device_id=self.device_id,
                        start_time=session.start_time,
                        end_time=time.time(),
                        local_video_path=str(final_video_path),
                    )
                    self.data_engine.storage.update_video_recording(record)
                except Exception as db_err:
                    logger.error(f"Failed to persist video recording stop to DB: {db_err}")

            if success and mp4_path.exists():
                try:
                    output_path.unlink()
                except Exception:
                    pass
                return VideoRecordingResult(
                    success=True,
                    message=f"Recording stopped, saved to {mp4_path}",
                    video_path=mp4_path,
                )
            else:
                return VideoRecordingResult(
                    success=True,
                    message=(
                        f"Recording stopped, saved as MKV to {output_path} (conversion failed)"
                    ),
                    video_path=output_path,
                )

        except Exception as e:
            logger.error(f"Failed to stop scrcpy recording: {e}")
            remove_active_session(self.device_id)
            return VideoRecordingResult(
                success=False,
                message=f"Failed to stop recording: {e}",
            )

    async def extract_segment_metadata(
        self,
        start_relative_time: float,
        end_relative_time: float | None = None,
    ) -> VideoRecordingResult:
        """Get a video segment for a specific time range (relative to video start)."""
        cache_key = (
            round(start_relative_time, 1),
            round(end_relative_time, 1) if end_relative_time is not None else None,
        )
        if cache_key in self._segment_cache:
            cached_res = self._segment_cache[cache_key]
            if cached_res.success and cached_res.video_path and cached_res.video_path.exists():
                logger.info(
                    "Reusing cached trimmed video segment for range"
                    f" {cache_key[0]}s to {cache_key[1]}s"
                )
                return cached_res

        session = get_active_session(self.device_id)
        if not session:
            return VideoRecordingResult(
                success=False,
                message=f"No active recording for device {self.device_id}",
            )

        try:
            mkv_path = session.local_video_path
            if not mkv_path or not mkv_path.exists():
                return VideoRecordingResult(
                    success=False,
                    message="Recording file not found",
                )

            # Check if process is still running (best effort for disconnection check)
            crashed = False
            restart_message = ""

            if session.process and session.process.returncode is not None:
                logger.warning(f"scrcpy process for {self.device_id} has terminated unexpectedly")
                crashed = True

                # Move current file to segments list (save it before overwriting session state)
                session.android_video_segments.append(mkv_path)

                # Try to restart scrcpy
                try:
                    session.android_segment_index += 1
                    output_dir = mkv_path.parent
                    new_video_path = output_dir / f"recording_{session.android_segment_index}.mkv"

                    cmd = build_scrcpy_record_command(
                        "scrcpy", self.device_id, new_video_path, lock_capture_orientation=False
                    )

                    process = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )

                    # Wait a bit and check if it's still running.
                    await asyncio.sleep(1.0)
                    if process.returncode is None:
                        session.process = process
                        session.local_video_path = new_video_path
                        restart_message = "Successfully restarted scrcpy."
                    else:
                        stderr = await process.stderr.read()
                        restart_message = f"Failed to restart scrcpy: {stderr.decode()}"
                except Exception as e:
                    restart_message = f"Failed to restart scrcpy: {e}"

            current_time = time.time()
            video_duration = current_time - session.start_time

            # Calculate offset
            offset = 0.0
            if session.data_engine_start_time is not None:
                offset = session.start_time - session.data_engine_start_time
                if offset < 0:
                    offset = 0.0

            video_start_relative_time = start_relative_time - offset
            video_end_relative_time = (
                end_relative_time - offset if end_relative_time is not None else None
            )

            # Safety buffer: avoid reading the very last second being written
            safe_duration = max(0.0, video_duration - 1.0)

            truncation_warning = None
            if video_end_relative_time is not None and video_end_relative_time > safe_duration:
                truncation_warning = (
                    "Video segment truncated. Requested end time"
                    f" {end_relative_time:.1f}s exceeded available safe"
                    f" duration. Truncated to {safe_duration + offset:.1f}s."
                )

            if video_end_relative_time is None or video_end_relative_time > safe_duration:
                video_end_relative_time = safe_duration

            if video_start_relative_time < 0:
                video_start_relative_time = 0.0

            if video_start_relative_time >= video_end_relative_time:
                return VideoRecordingResult(
                    success=False,
                    message=(
                        "Invalid time range or requested too close to current"
                        f" time. Available duration: {safe_duration:.1f}s"
                    ),
                )

            # Trim directly from MKV to MP4 (using mkv_path which points to the file before we potentially changed it for restart)

            trim_output_dir = tempfile.mkdtemp(
                prefix="video_trimmed_",
                dir=get_temp_dir("trimmed_videos"),
            )
            trim_output_path = Path(trim_output_dir) / "segment.mp4"

            # Retry mechanism for ffmpeg stability
            success = False
            for attempt in range(3):
                success = await trim_video(
                    mkv_path,
                    video_start_relative_time,
                    video_end_relative_time,
                    trim_output_path,
                )
                if success and trim_output_path.exists():
                    break
                logger.warning(f"ffmpeg trim attempt {attempt + 1} failed, retrying in 0.5s...")
                await asyncio.sleep(0.5)

            if not success or not trim_output_path.exists():
                return VideoRecordingResult(
                    success=False,
                    message="Failed to trim video segment after retries",
                )

            actual_start = video_start_relative_time + offset
            actual_end = video_end_relative_time + offset

            message = f"Video segment retrieved for range {actual_start:.1f}s to {actual_end:.1f}s"
            if crashed:
                message += (
                    f". Warning: scrcpy crashed. Data loss may have occurred. {restart_message}"
                )

            file_size_mb = trim_output_path.stat().st_size / (1024 * 1024)
            duration = video_end_relative_time - video_start_relative_time

            res = VideoRecordingResult(
                success=True,
                message=message,
                video_path=trim_output_path,
                file_size_mb=round(file_size_mb, 2),
                duration_seconds=round(duration, 2),
                actual_start_relative_time=actual_start,
                warning=truncation_warning,
            )
            self._segment_cache[cache_key] = res
            return res

        except Exception as e:
            logger.error(f"Failed to get video segment: {e}")
            return VideoRecordingResult(
                success=False,
                message=f"Failed to get video segment: {e}",
            )

    async def _convert_mkv_to_mp4(self, mkv_path: Path, mp4_path: Path) -> bool:
        """Normalize MKV into a fixed-size, browser-safe MP4."""
        session = get_active_session(self.device_id)
        return await normalize_recording_to_mp4(
            mkv_path,
            mp4_path,
            session.capture_width if session else self.device_width,
            session.capture_height if session else self.device_height,
        )
