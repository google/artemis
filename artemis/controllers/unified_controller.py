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
import os
from pathlib import Path
import tempfile
import time
from typing import Any
from uuid import uuid4

from artemis.config.paths import get_temp_dir
from artemis.context import ArtemisContext
from artemis.drivers.factory import get_driver
from artemis.drivers.base import BaseDeviceDriver
from artemis.controllers.device_controller import ScreenDataResponse
from artemis.controllers.types import (
    SwipeRequest,
    SwipeStartEndCoordinatesRequest,
    SwipeStartEndPercentagesRequest,
    TapOutput,
)
from artemis.utils.logger import get_logger
from artemis.utils.video import (
    DEFAULT_MAX_DURATION_SECONDS,
    RecordingSession,
    VideoRecordingResult,
    cleanup_video_segments,
    concatenate_videos,
    get_active_session,
    get_ffmpeg_path,
    has_active_session,
    remove_active_session,
    set_active_session,
    trim_video,
)

logger = get_logger(__name__)


class UnifiedMobileController:
    def __init__(self, ctx: ArtemisContext):
        self.ctx = ctx
        self._driver: BaseDeviceDriver = get_driver(ctx)
        self._segment_cache: dict[tuple[float, float | None], VideoRecordingResult] = {}

    @property
    def driver(self) -> BaseDeviceDriver:
        return self._driver

    @property
    def controller(self) -> Any:
        return self._driver

    async def tap_at(
        self,
        x: int,
        y: int,
        long_press: bool = False,
        long_press_duration: int = 1000,
        times: int = 1,
        delay_ms: int = 100,
    ) -> TapOutput:
        try:
            if long_press:
                success = await self._driver.long_press(x, y, duration_ms=long_press_duration)
            else:
                success = await self._driver.tap(
                    x, y, duration_ms=100, times=times, delay_ms=delay_ms
                )
            return TapOutput(error=None if success else f"Tap failed at ({x}, {y})")
        except Exception as e:
            return TapOutput(error=str(e))

    async def tap_percentage(
        self,
        x_percent: int,
        y_percent: int,
        long_press: bool = False,
        long_press_duration: int = 1000,
    ) -> TapOutput:
        """Tap at percentage-based coordinates (0 to 100)."""
        norm_x = int(x_percent * 10)
        norm_y = int(y_percent * 10)
        success = await self._driver.tap_normalized(
            norm_x, norm_y, long_press=long_press, duration_ms=long_press_duration
        )
        return TapOutput(
            error=None if success else f"Tap percentage failed at ({x_percent}%, {y_percent}%)"
        )

    async def tap_element(
        self,
        resource_id: str | None = None,
        text: str | None = None,
        index: int = 0,
        long_press: bool = False,
        long_press_duration: int = 1000,
    ) -> TapOutput:
        """Tap on a UI element by finding it in the hierarchy."""
        success = await self._driver.tap_element(
            resource_id=resource_id,
            text=text,
            index=index,
            long_press=long_press,
            duration_ms=long_press_duration,
        )
        return TapOutput(
            error=None
            if success
            else f"Failed to tap element (resource_id={resource_id}, text={text})"
        )

    async def swipe_coords(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration: int = 400,
    ) -> str | None:
        """Swipe between two coordinate points."""
        success = await self._driver.swipe(start_x, start_y, end_x, end_y, duration_ms=duration)
        return None if success else f"Swipe failed from ({start_x},{start_y}) to ({end_x},{end_y})"

    async def swipe_percentage(
        self,
        start_x_percent: int,
        start_y_percent: int,
        end_x_percent: int,
        end_y_percent: int,
        duration: int = 400,
    ) -> str | None:
        """Swipe using percentage-based coordinates (0 to 100)."""
        start_norm = [int(start_x_percent * 10), int(start_y_percent * 10)]
        end_norm = [int(end_x_percent * 10), int(end_y_percent * 10)]
        success = await self._driver.swipe_normalized(start_norm, end_norm, duration_ms=duration)
        return None if success else "Swipe percentage failed"

    async def swipe_request(self, request: SwipeRequest) -> str | None:
        mode = request.swipe_mode

        if isinstance(mode, SwipeStartEndCoordinatesRequest):
            return await self.swipe_coords(
                start_x=mode.start.x,
                start_y=mode.start.y,
                end_x=mode.end.x,
                end_y=mode.end.y,
                duration=request.duration or 400,
            )
        elif isinstance(mode, SwipeStartEndPercentagesRequest):
            return await self.swipe_percentage(
                start_x_percent=mode.start.x_percent,
                start_y_percent=mode.start.y_percent,
                end_x_percent=mode.end.x_percent,
                end_y_percent=mode.end.y_percent,
                duration=request.duration or 400,
            )
        else:
            return "Unsupported swipe mode"

    async def type_text(self, text: str, clear_existing: bool = True) -> bool:
        return await self._driver.input_text(text, clear_existing=clear_existing)

    async def take_screenshot(self) -> str:
        screen_data = await self._driver.get_screen_data()
        return screen_data.screenshot_base64

    async def launch_app(self, package_or_bundle_id: str) -> bool:
        return await self._driver.launch_app(package_or_bundle_id)

    async def terminate_app(self, package_or_bundle_id: str | None) -> bool:
        if not package_or_bundle_id:
            return False
        return await self._driver.stop_app(package_or_bundle_id)

    async def open_url(self, url: str) -> bool:
        await self._driver.execute_shell(f"am start -a android.intent.action.VIEW -d '{url}'")
        return True

    async def go_back(self) -> bool:
        return await self._driver.press_key("back")

    async def go_home(self) -> bool:
        return await self._driver.press_key("home")

    async def press_enter(self) -> bool:
        return await self._driver.press_key("enter")

    async def press_key(self, keycode: str) -> bool:
        return await self._driver.press_key(keycode)

    async def erase_text(self, nb_chars: int | None = None) -> bool:
        if nb_chars is not None and nb_chars > 0:
            for _ in range(nb_chars):
                await self._driver.press_key("delete")
            return True
        # Safe & robust full clear: Move to End -> Shift+Home selection -> Delete -> Fallback backspaces
        try:
            clear_cmd = (
                "input keyevent 123 && "
                "input keyevent --meta 1 122 && "
                "input keyevent 67 && "
                "input keyevent 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67"
            )
            await self._driver.execute_shell(clear_cmd)
        except Exception:
            for _ in range(30):
                await self._driver.press_key("delete")
        return True

    async def get_ui_elements(self) -> list[dict]:
        screen_data = await self._driver.get_screen_data()
        return screen_data.ui_elements or []

    async def get_screen_data(self) -> "ScreenDataResponse":
        """Get screen data including screenshot, UI hierarchy, dimensions, and platform."""
        data = await self._driver.get_screen_data()
        return ScreenDataResponse(
            base64=data.screenshot_base64,
            elements=data.ui_elements or [],
            width=data.width,
            height=data.height,
            platform=data.platform,
        )

    async def find_element(
        self,
        resource_id: str | None = None,
        text: str | None = None,
        index: int = 0,
    ) -> tuple[dict | None, str | None]:
        elem, _, error = await self._driver.find_element(
            resource_id=resource_id,
            text=text,
            index=index,
        )
        return elem, error

    def _get_device_id(self) -> str:
        if self.ctx and self.ctx.device and self.ctx.device.device_id:
            return self.ctx.device.device_id
        return getattr(self._driver, "device_id", None) or "default"

    async def extract_segment_metadata(
        self,
        start_time: float,
        end_time: float | None = None,
        output_path: Path | None = None,
    ) -> VideoRecordingResult:
        """Get a video segment for a specific time range (relative to video start)."""
        cache_key = (
            round(start_time, 1),
            round(end_time, 1) if end_time is not None else None,
        )
        if cache_key in self._segment_cache:
            cached_res = self._segment_cache[cache_key]
            if cached_res.success and cached_res.video_path and cached_res.video_path.exists():
                logger.info(
                    f"Reusing cached trimmed video segment for range {cache_key[0]}s to {cache_key[1]}s"
                )
                return cached_res

        device_id = self._get_device_id()

        # Handle mock driver
        if (
            getattr(self._driver, "is_mock", False)
            or getattr(getattr(self.ctx, "device", None), "mobile_platform", None) == "mock"
            or os.environ.get("ARTEMIS_MOCK_DRIVER") == "1"
        ):
            mock_video = Path("/tmp/mock_recording.mp4")
            return VideoRecordingResult(
                success=True,
                video_path=mock_video,
                message="Mock segment extracted",
            )

        session = get_active_session(device_id)
        if not session:
            return VideoRecordingResult(
                success=False,
                message=f"No active recording for device {device_id}",
            )

        try:
            mkv_path = session.local_video_path
            if not mkv_path:
                return VideoRecordingResult(
                    success=False,
                    message="Recording file not found",
                )

            # Check if scrcpy process terminated and restart if needed (synchronous fallback)
            crashed = False
            restart_message = ""
            if session.process and session.process.returncode is not None:
                logger.warning(f"scrcpy process for {device_id} has terminated unexpectedly")
                crashed = True
                if mkv_path.exists() and mkv_path not in session.android_video_segments:
                    session.android_video_segments.append(mkv_path)
                try:
                    session.android_segment_index += 1
                    output_dir = mkv_path.parent
                    new_video_path = output_dir / f"recording_{session.android_segment_index}.mkv"
                    cmd = [
                        "scrcpy",
                        "--serial",
                        device_id,
                        "--no-window",
                        "--record",
                        str(new_video_path),
                        "--record-format",
                        "mkv",
                        "--video-bit-rate",
                        "2M",
                    ]
                    process = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    await asyncio.sleep(0.8)
                    if process.returncode is None:
                        session.process = process
                        session.local_video_path = new_video_path
                        mkv_path = new_video_path
                        restart_message = "Successfully restarted scrcpy."
                    else:
                        stderr = await process.stderr.read()
                        restart_message = f"Failed to restart scrcpy: {stderr.decode()}"
                except Exception as e:
                    restart_message = f"Failed to restart scrcpy: {e}"

            # Determine source file to trim from (support multi-segment seamless trimming)
            all_segments = [p for p in session.android_video_segments if p.exists()]
            if mkv_path.exists() and mkv_path not in all_segments:
                all_segments.append(mkv_path)

            if not all_segments:
                return VideoRecordingResult(
                    success=False,
                    message="No recording segments found on disk",
                )

            temp_combined_file = None
            if len(all_segments) > 1:
                temp_combined_file = mkv_path.parent / "temp_combined_active.mkv"
                concat_ok = await concatenate_videos(all_segments, temp_combined_file)
                trim_source = (
                    temp_combined_file if (concat_ok and temp_combined_file.exists()) else mkv_path
                )
            else:
                trim_source = all_segments[0]

            current_time = time.time()
            video_duration = current_time - session.start_time

            # Strict timeline alignment:
            # DataEngine session start time is the reference T0 (0.0s).
            # Video recording start time is session.start_time.
            offset = 0.0
            if session.data_engine_start_time is not None:
                offset = max(0.0, session.start_time - session.data_engine_start_time)

            video_start_relative_time = start_time - offset
            video_end_relative_time = end_time - offset if end_time is not None else None

            safe_duration = max(0.0, video_duration - 0.5)

            truncation_warning = None
            if video_end_relative_time is not None and video_end_relative_time > safe_duration:
                truncation_warning = (
                    f"Video segment truncated. Requested end time {end_time:.1f}s "
                    f"exceeded available safe duration. Truncated to {safe_duration + offset:.1f}s."
                )

            if video_end_relative_time is None or video_end_relative_time > safe_duration:
                video_end_relative_time = safe_duration

            if video_start_relative_time < 0:
                video_start_relative_time = 0.0

            if video_start_relative_time >= video_end_relative_time:
                if safe_duration > 0:
                    video_end_relative_time = safe_duration
                    video_start_relative_time = max(
                        0.0,
                        safe_duration - ((end_time - start_time) if end_time else 2.0),
                    )

            if video_start_relative_time >= video_end_relative_time:
                return VideoRecordingResult(
                    success=False,
                    message=(
                        f"Invalid time range or requested too close to current time. Available duration: {safe_duration:.1f}s"
                    ),
                )

            trim_output_dir = tempfile.mkdtemp(
                prefix="video_trimmed_",
                dir=get_temp_dir("trimmed_videos"),
            )
            trim_output_path = Path(trim_output_dir) / "segment.mp4"

            success = False
            for attempt in range(3):
                success = await trim_video(
                    trim_source,
                    video_start_relative_time,
                    video_end_relative_time,
                    trim_output_path,
                )
                if success and trim_output_path.exists():
                    break
                logger.warning(f"ffmpeg trim attempt {attempt + 1} failed, retrying in 0.5s...")
                await asyncio.sleep(0.5)

            # Cleanup temp combined MKV if created
            if temp_combined_file and temp_combined_file.exists():
                try:
                    temp_combined_file.unlink()
                except Exception:
                    pass

            if not success or not trim_output_path.exists():
                return VideoRecordingResult(
                    success=False,
                    message="Failed to trim video segment after retries",
                )

            actual_start = video_start_relative_time + offset
            actual_end = video_end_relative_time + offset
            file_size_mb = trim_output_path.stat().st_size / (1024 * 1024)
            duration = video_end_relative_time - video_start_relative_time

            message = f"Video segment retrieved for range {actual_start:.1f}s to {actual_end:.1f}s"
            if crashed:
                message += (
                    f". Warning: scrcpy crashed. Data loss may have occurred. {restart_message}"
                )

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

    async def _recording_watchdog(self, device_id: str) -> None:
        """Background watchdog to automatically detect crashes and recover scrcpy recording."""
        while True:
            session = get_active_session(device_id)
            if not session or not session.is_active:
                break
            proc = session.process
            if proc is None:
                break
            try:
                ret = await proc.wait()
                session = get_active_session(device_id)
                if not session or not session.is_active:
                    break

                logger.warning(
                    f"scrcpy recording process for {device_id} terminated unexpectedly (code {ret}). "
                    f"Auto-restarting recording segment {session.android_segment_index + 1}..."
                )
                if session.local_video_path and session.local_video_path.exists():
                    if session.local_video_path not in session.android_video_segments:
                        session.android_video_segments.append(session.local_video_path)

                session.android_segment_index += 1
                output_dir = session.local_video_path.parent
                new_video_path = output_dir / f"recording_{session.android_segment_index}.mkv"
                cmd = [
                    "scrcpy",
                    "--serial",
                    device_id,
                    "--no-window",
                    "--record",
                    str(new_video_path),
                    "--record-format",
                    "mkv",
                    "--video-bit-rate",
                    "2M",
                ]
                new_proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                session.process = new_proc
                session.local_video_path = new_video_path
                await asyncio.sleep(0.8)
                if new_proc.returncode is not None:
                    stderr = await new_proc.stderr.read()
                    logger.error(f"Failed to auto-restart scrcpy recording: {stderr.decode()}")
                    break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in recording watchdog for {device_id}: {e}")
                break

    async def start_video_recording(
        self,
        output_dir: Path | None = None,
        max_duration_seconds: int = DEFAULT_MAX_DURATION_SECONDS,
    ) -> VideoRecordingResult:
        """Start screen recording on Android device using scrcpy."""
        self._segment_cache.clear()
        device_id = self._get_device_id()

        # Check mock driver first
        if (
            getattr(self._driver, "is_mock", False)
            or getattr(getattr(self.ctx, "device", None), "mobile_platform", None) == "mock"
            or os.environ.get("ARTEMIS_MOCK_DRIVER") == "1"
        ):
            await self._driver.start_video_recording(output_dir)
            return VideoRecordingResult(success=True, message="Mock recording started")

        if has_active_session(device_id):
            return VideoRecordingResult(
                success=False,
                message=f"Recording already in progress for device {device_id}",
            )

        try:
            if not output_dir:
                output_dir = Path(
                    tempfile.mkdtemp(prefix="scrcpy_", dir=get_temp_dir("recordings"))
                )
            else:
                output_dir = Path(output_dir)
                output_dir.mkdir(parents=True, exist_ok=True)

            local_video_path = output_dir / "recording.mkv"
            video_id = uuid4()
            start_time = time.time()
            data_engine_start_time = (
                self.ctx.data_engine.session_start_time
                if (self.ctx and self.ctx.data_engine)
                else start_time
            )

            session = RecordingSession(
                video_id=video_id,
                device_id=device_id,
                start_time=start_time,
                data_engine_start_time=data_engine_start_time,
                local_video_path=local_video_path,
                is_active=True,
            )

            # Persist to local database if Data Engine is active
            if self.ctx and self.ctx.data_engine:
                self.ctx.data_engine.record_video_start(
                    video_id=video_id,
                    device_id=device_id,
                    local_video_path=local_video_path,
                    start_time=session.start_time,
                )

            # Start scrcpy in background
            cmd = [
                "scrcpy",
                "--serial",
                device_id,
                "--no-window",
                "--record",
                str(local_video_path),
                "--record-format",
                "mkv",
                "--video-bit-rate",
                "2M",
            ]

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            session.process = process
            set_active_session(device_id, session)

            logger.info(f"Started scrcpy recording on {device_id}, saving to {local_video_path}")

            # Check if process failed immediately
            await asyncio.sleep(0.8)
            if process.returncode is not None:
                stderr = await process.stderr.read()
                err_msg = stderr.decode()
                logger.error(f"scrcpy failed to start on {device_id}: {err_msg}")
                remove_active_session(device_id)
                return VideoRecordingResult(
                    success=False,
                    message=f"scrcpy failed to start: {err_msg}",
                )

            # Start background watchdog to auto-recover if scrcpy terminates
            session.watchdog_task = asyncio.create_task(self._recording_watchdog(device_id))

            return VideoRecordingResult(
                success=True,
                message=f"Recording started on {device_id}",
            )

        except Exception as e:
            logger.error(f"Failed to start scrcpy recording: {e}")
            remove_active_session(device_id)
            return VideoRecordingResult(
                success=False,
                message=f"Failed to start recording: {e}",
            )

    async def stop_video_recording(self) -> VideoRecordingResult:
        """Stop scrcpy recording and return the converted MP4 video file."""
        self._segment_cache.clear()
        device_id = self._get_device_id()

        # Check mock driver first
        if (
            getattr(self._driver, "is_mock", False)
            or getattr(getattr(self.ctx, "device", None), "mobile_platform", None) == "mock"
            or os.environ.get("ARTEMIS_MOCK_DRIVER") == "1"
        ):
            p = await self._driver.stop_video_recording()
            return VideoRecordingResult(
                success=True,
                video_path=Path(p) if p else None,
                message="Mock recording stopped",
            )

        session = get_active_session(device_id)
        if not session:
            return VideoRecordingResult(
                success=False,
                message=f"No active recording for device {device_id}",
            )

        # Mark inactive and cancel watchdog task
        session.is_active = False
        if session.watchdog_task and not session.watchdog_task.done():
            session.watchdog_task.cancel()

        try:
            process = session.process
            if process is not None:
                try:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except Exception as proc_e:
                    logger.warning(f"Error terminating scrcpy process: {proc_e}")

            output_path = session.local_video_path
            if not output_path or not output_path.exists():
                remove_active_session(device_id)
                return VideoRecordingResult(
                    success=False,
                    message="Recording file not found on disk",
                )

            # Concatenate previous segments if scrcpy crashed and restarted
            if session.android_video_segments:
                all_segments = session.android_video_segments
                if output_path.exists() and output_path not in all_segments:
                    all_segments.append(output_path)
                concatenated_mkv = output_path.parent / "full_recording.mkv"
                if await concatenate_videos(all_segments, concatenated_mkv):
                    cleanup_video_segments(all_segments)
                    output_path = concatenated_mkv

            # Convert MKV to web-streamable MP4
            mp4_path = (
                output_path.parent / "recording.mp4"
                if output_path.name == "full_recording.mkv"
                else output_path.with_suffix(".mp4")
            )
            success = await self._convert_mkv_to_mp4(output_path, mp4_path)

            remove_active_session(device_id)

            final_video_path = mp4_path if (success and mp4_path.exists()) else output_path

            # Persist update to local database if Data Engine is active
            if self.ctx and self.ctx.data_engine:
                self.ctx.data_engine.record_video_stop(
                    video_id=session.video_id,
                    device_id=device_id,
                    local_video_path=final_video_path,
                    start_time=session.start_time,
                    end_time=time.time(),
                )

            if success and mp4_path.exists():
                try:
                    if output_path != mp4_path and output_path.exists():
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
                    message=f"Recording stopped, saved as MKV to {output_path}",
                    video_path=output_path,
                )

        except Exception as e:
            logger.error(f"Failed to stop scrcpy recording: {e}")
            remove_active_session(device_id)
            return VideoRecordingResult(
                success=False,
                message=f"Failed to stop recording: {e}",
            )

    async def _convert_mkv_to_mp4(self, mkv_path: Path, mp4_path: Path) -> bool:
        """Convert MKV to MP4 using ffmpeg with faststart for web streaming."""
        if not mkv_path.exists():
            return False
        try:
            # 1. Try fast stream copy with +faststart
            process = await asyncio.create_subprocess_exec(
                get_ffmpeg_path(),
                "-y",
                "-i",
                str(mkv_path),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(mp4_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await process.wait()
            if mp4_path.exists() and mp4_path.stat().st_size > 0:
                return True

            # 2. Fallback to re-encoding if stream copy fails
            logger.warning("ffmpeg fast copy failed, re-encoding MKV to MP4...")
            process = await asyncio.create_subprocess_exec(
                get_ffmpeg_path(),
                "-y",
                "-i",
                str(mkv_path),
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(mp4_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await process.wait()
            return mp4_path.exists() and mp4_path.stat().st_size > 0
        except Exception as e:
            logger.error(f"Failed to convert MKV to MP4: {e}")
            return False

    async def cleanup(self) -> None:
        await self._driver.disconnect()
