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
import signal
import subprocess
import tempfile
import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from artemis.config.paths import get_temp_dir
from artemis.context import ArtemisContext
from artemis.drivers.factory import get_driver
from artemis.drivers.ios.recording import IosRecordingSession
from artemis.utils.video import (
    ANDROID_RECORDING_SEGMENT_SECONDS,
    await_scrcpy_first_frame,
    build_scrcpy_record_command,
    get_android_display_state,
    remux_recording_to_mp4,
    render_timeline_clip,
    write_recording_manifest,
)
from third_party.mobile_use.controllers.unified_controller import UnifiedMobileControllerBase
from third_party.mobile_use.utils.logger import get_logger
from third_party.mobile_use.utils.video import (
    DEFAULT_MAX_DURATION_SECONDS,
    RecordingSession,
    VideoRecordingResult,
    abort_recording,
    get_active_session,
    no_active_recording,
    recording_already_active,
    remove_active_session,
    set_active_session,
)

if TYPE_CHECKING:
    from artemis.drivers.ios.xcode_driver import XcodeSimulatorDriver

logger = get_logger(__name__)


class UnifiedMobileController(UnifiedMobileControllerBase):
    def __init__(self, ctx: ArtemisContext):
        super().__init__(ctx, get_driver(ctx))
        self._segment_cache: dict[tuple[str, int, float, float], VideoRecordingResult] = {}

    async def open_url(self, url: str) -> bool:
        if self.ctx.device.mobile_platform == "ios":
            return await self._driver.open_url(url)
        return await super().open_url(url)

    async def erase_text(self, nb_chars: int | None = None) -> bool:
        if self.ctx.device.mobile_platform == "ios":
            # No native clear/Backspace key exists; the driver raises
            # NotImplementedError for unsupported clear operations.
            return await self._driver.input_text("", clear_existing=True)
        return await super().erase_text(nb_chars)

    @staticmethod
    async def _spawn_scrcpy(command: list[str]) -> asyncio.subprocess.Process:
        kwargs: dict[str, Any] = {
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        return await asyncio.create_subprocess_exec(*command, **kwargs)

    @staticmethod
    async def _stop_scrcpy(process: asyncio.subprocess.Process) -> None:
        """Ask scrcpy to flush its recorder before falling back to termination."""
        if process.returncode is not None:
            return
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
            await asyncio.wait_for(process.wait(), timeout=8.0)
        except (ProcessLookupError, TimeoutError):
            if process.returncode is None:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=5.0)

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
            return no_active_recording(device_id)

        cache_key = None
        if end_time is not None:
            cache_key = (
                str(session.video_id),
                session.generation,
                round(start_time, 1),
                round(end_time, 1),
            )
            cached_res = self._segment_cache.get(cache_key)
            if (
                cached_res
                and cached_res.success
                and cached_res.video_path
                and cached_res.video_path.exists()
            ):
                logger.info(
                    "Reusing generation-scoped trimmed video segment for range "
                    f"{cache_key[2]}s to {cache_key[3]}s"
                )
                return cached_res

        if isinstance(session, IosRecordingSession):
            result = await self._extract_ios_segment(session, start_time, end_time, output_path)
            if (
                cache_key is not None
                and result.success
                and result.video_path
                and result.video_path.exists()
            ):
                # Sealing bumps the generation; key the entry under the
                # post-seal generation a repeated lookup will observe.
                self._segment_cache[
                    (cache_key[0], session.generation, cache_key[2], cache_key[3])
                ] = result
            return result

        try:
            mkv_path = session.local_video_path
            if not mkv_path:
                return VideoRecordingResult(
                    success=False,
                    message="Recording file not found",
                )

            current_time = time.time()
            video_duration = current_time - session.start_time

            # Strict timeline alignment:
            # DataEngine session start time is the reference T0 (0.0s).
            # session.start_time is the wall-clock time of the first frame in
            # the recording (the file's t=0), estimated at recording start.
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
                timeline_segments = [dict(record) for record in session.android_segment_records]
                active_start = session.android_segment_started_at or session.start_time
                if mkv_path.exists():
                    timeline_segments.append(
                        {
                            "path": mkv_path,
                            "start": max(0.0, active_start - session.start_time),
                            "end": max(0.0, current_time - session.start_time),
                        }
                    )
                success = await render_timeline_clip(
                    timeline_segments,
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
            file_size_mb = trim_output_path.stat().st_size / (1024 * 1024)
            duration = video_end_relative_time - video_start_relative_time

            message = f"Video segment retrieved for range {actual_start:.1f}s to {actual_end:.1f}s"
            res = VideoRecordingResult(
                success=True,
                message=message,
                video_path=trim_output_path,
                file_size_mb=round(file_size_mb, 2),
                duration_seconds=round(duration, 2),
                actual_start_relative_time=actual_start,
                warning=truncation_warning,
                video_id=session.video_id,
                generation=session.generation,
                sealed_until=session.sealed_until,
                source_revision=(f"{session.video_id}:{session.generation}:{round(actual_end, 3)}"),
            )
            if cache_key is not None:
                self._segment_cache[cache_key] = res
            return res

        except Exception as e:
            logger.error(f"Failed to get video segment: {e}")
            return VideoRecordingResult(
                success=False,
                message=f"Failed to get video segment: {e}",
            )

    @staticmethod
    def _segment_mp4_path(source_path: Path, index: int) -> Path:
        return source_path.parent / (
            "recording.mp4" if index == 0 else f"recording_{index:03d}.mp4"
        )

    @staticmethod
    async def _remux_segment_record(record: dict[str, Any]) -> bool:
        source_path = Path(record["path"])
        output_path = Path(record["output_path"])
        success = await remux_recording_to_mp4(source_path, output_path)
        if success:
            record["path"] = output_path
            try:
                source_path.unlink()
            except OSError:
                pass
        return success

    def _finalize_current_segment(self, session: RecordingSession, end_time: float) -> None:
        source_path = session.local_video_path
        if not source_path or not source_path.exists():
            return
        if any(Path(record["path"]) == source_path for record in session.android_segment_records):
            return
        start_time = session.android_segment_started_at or session.start_time
        output_path = self._segment_mp4_path(source_path, session.android_segment_index)
        session.android_video_segments.append(source_path)
        session.android_segment_records.append(
            {
                "path": source_path,
                "output_path": output_path,
                "start": max(0.0, start_time - session.start_time),
                "end": max(0.0, end_time - session.start_time),
                "rotation": session.android_rotation,
                "generation": session.generation,
                "conversion_scheduled": True,
            }
        )
        session.android_conversion_tasks.append(
            asyncio.create_task(self._remux_segment_record(session.android_segment_records[-1]))
        )
        session.sealed_until = max(
            session.sealed_until,
            max(0.0, end_time - session.start_time),
        )

    @staticmethod
    def _segment_session_offsets(
        session: RecordingSession, mp4_paths: list[Path]
    ) -> dict[Path, float]:
        """Map each finalized segment MP4 to its first-frame offset from session start.

        ``record["start"]`` is relative to the recording's own t=0 (the first
        frame of the first segment); shifting it by the recording/DataEngine
        anchor difference yields the session-relative offset the UI needs to
        align step timestamps with the playlist.
        """
        anchor = session.data_engine_start_time
        recording_shift = (session.start_time - anchor) if anchor is not None else 0.0
        offsets: dict[Path, float] = {}
        for record in session.android_segment_records:
            output_path = Path(record.get("output_path") or record.get("path") or "")
            if not output_path.name:
                continue
            offsets[output_path] = max(0.0, float(record.get("start", 0.0)) + recording_shift)
        for path in mp4_paths:
            # Emergency fallback remux of the live file has no record; it is
            # the whole recording, so it starts at the recording anchor.
            offsets.setdefault(path, max(0.0, recording_shift))
        return offsets

    def _record_recording_failure(self, session: RecordingSession, message: str) -> None:
        if self.ctx and self.ctx.data_engine:
            self.ctx.data_engine.record_video_failure(
                video_id=session.video_id,
                device_id=session.device_id,
                local_video_path=session.local_video_path,
                start_time=session.start_time,
                error=message,
            )

    async def _start_next_recording_segment(
        self, session: RecordingSession, display_state: tuple[int, int, int] | None
    ) -> bool:
        session.android_segment_index += 1
        session.generation = session.android_segment_index
        output_dir = session.local_video_path.parent
        new_video_path = output_dir / f"recording_{session.android_segment_index:03d}.mkv"
        process = await self._spawn_scrcpy(
            build_scrcpy_record_command("scrcpy", session.device_id, new_video_path)
        )
        spawned_at = time.time()
        first_frame_at = await await_scrcpy_first_frame(process, spawned_at)
        if process.returncode is not None:
            stderr = await process.stderr.read()
            logger.error(f"Failed to start scrcpy segment: {stderr.decode(errors='replace')}")
            return False
        session.process = process
        session.local_video_path = new_video_path
        session.android_segment_started_at = first_frame_at
        logger.info(
            f"scrcpy segment {session.android_segment_index} first frame estimated "
            f"{first_frame_at - spawned_at:.2f}s after spawn"
        )
        if display_state:
            session.android_rotation, session.capture_width, session.capture_height = display_state
        return True

    async def _recording_watchdog(self, device_id: str) -> None:
        """Roll fixed-orientation segments and recover scrcpy crashes."""
        try:
            while True:
                await asyncio.sleep(0.5)
                session = get_active_session(device_id)
                if not session or not session.is_active or not session.process:
                    return
                now = time.time()
                display_state = await get_android_display_state(device_id)
                rotated = bool(
                    display_state
                    and session.android_rotation is not None
                    and display_state[0] != session.android_rotation
                )
                segment_age = now - (session.android_segment_started_at or session.start_time)
                crashed = session.process.returncode is not None
                if not crashed and not rotated and segment_age < ANDROID_RECORDING_SEGMENT_SECONDS:
                    continue

                if not crashed:
                    await self._stop_scrcpy(session.process)
                self._finalize_current_segment(session, time.time())
                reason = (
                    "rotation" if rotated else "time limit" if not crashed else "recorder crash"
                )
                logger.info(f"Rolling screen recording segment after {reason}")
                if not await self._start_next_recording_segment(session, display_state):
                    return
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"Error in recording supervisor for {device_id}: {e}")

    async def start_video_recording(
        self,
        output_dir: Path | None = None,
        max_duration_seconds: int = DEFAULT_MAX_DURATION_SECONDS,
    ) -> VideoRecordingResult:
        """Start screen recording using the platform's native capture."""
        self._segment_cache.clear()
        if self.ctx.device.mobile_platform == "ios":
            return await self._start_ios_recording(output_dir, max_duration_seconds)
        device_id = self._get_device_id()

        # Check mock driver first
        if (
            getattr(self._driver, "is_mock", False)
            or getattr(getattr(self.ctx, "device", None), "mobile_platform", None) == "mock"
            or os.environ.get("ARTEMIS_MOCK_DRIVER") == "1"
        ):
            await self._driver.start_video_recording(output_dir)
            return VideoRecordingResult(success=True, message="Mock recording started")

        if already_active := recording_already_active(device_id):
            return already_active

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
            display_state = await get_android_display_state(device_id)
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
                capture_width=getattr(getattr(self.ctx, "device", None), "device_width", None),
                capture_height=getattr(getattr(self.ctx, "device", None), "device_height", None),
                android_rotation=display_state[0] if display_state else None,
                android_segment_started_at=start_time,
                is_active=True,
            )
            if display_state:
                session.capture_width, session.capture_height = display_state[1:]

            # Start scrcpy in background
            cmd = build_scrcpy_record_command("scrcpy", device_id, local_video_path)

            process = await self._spawn_scrcpy(cmd)
            spawned_at = time.time()

            session.process = process
            set_active_session(device_id, session)

            logger.info(f"Started scrcpy recording on {device_id}, saving to {local_video_path}")

            # The file's t=0 is the first captured frame, about a second after
            # spawn. Anchor the recording timeline there: segment offsets and
            # analyzer clip trimming all treat session.start_time as that t=0.
            first_frame_at = await await_scrcpy_first_frame(process, spawned_at)
            if process.returncode is not None:
                stderr = await process.stderr.read()
                err_msg = stderr.decode()
                logger.error(f"scrcpy failed to start on {device_id}: {err_msg}")
                if self.ctx and self.ctx.data_engine:
                    self.ctx.data_engine.record_video_start(
                        video_id=video_id,
                        device_id=device_id,
                        local_video_path=local_video_path,
                        start_time=session.start_time,
                    )
                self._record_recording_failure(session, f"scrcpy failed to start: {err_msg}")
                remove_active_session(device_id)
                return VideoRecordingResult(
                    success=False,
                    message=f"scrcpy failed to start: {err_msg}",
                )

            session.start_time = first_frame_at
            session.android_segment_started_at = first_frame_at
            timeline_note = ""
            if session.data_engine_start_time is not None:
                timeline_note = (
                    "; recording timeline offset from session start: "
                    f"{first_frame_at - session.data_engine_start_time:.2f}s"
                )
            logger.info(
                f"scrcpy first frame estimated {first_frame_at - spawned_at:.2f}s after spawn"
                f"{timeline_note}"
            )

            if self.ctx and self.ctx.data_engine:
                self.ctx.data_engine.record_video_start(
                    video_id=video_id,
                    device_id=device_id,
                    local_video_path=local_video_path,
                    start_time=session.start_time,
                )

            session.watchdog_task = asyncio.create_task(self._recording_watchdog(device_id))

            return VideoRecordingResult(
                success=True,
                message=f"Recording started on {device_id}",
                video_id=session.video_id,
                generation=session.generation,
                sealed_until=session.sealed_until,
                source_revision=f"{session.video_id}:{session.generation}:active",
            )

        except Exception as e:
            logger.error(f"Failed to start scrcpy recording: {e}")
            return abort_recording(device_id, "start", e)

    async def stop_video_recording(self) -> VideoRecordingResult:
        """Stop recording and return the finalized MP4 video file."""
        self._segment_cache.clear()
        if self.ctx.device.mobile_platform == "ios":
            return await self._stop_ios_recording()
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
            return no_active_recording(device_id)

        # Mark inactive and cancel watchdog task
        session.is_active = False
        if session.watchdog_task and not session.watchdog_task.done():
            session.watchdog_task.cancel()
            try:
                await session.watchdog_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.debug(
                    f"Recording watchdog for {device_id} ended with an error: {exc}", exc_info=True
                )

        try:
            process = session.process
            if process is not None:
                try:
                    await self._stop_scrcpy(process)
                except Exception as proc_e:
                    logger.warning(f"Error terminating scrcpy process: {proc_e}")

            output_path = session.local_video_path
            has_existing_recording = (output_path and output_path.exists()) or any(
                Path(r.get("output_path", "")).exists() or Path(r.get("path", "")).exists()
                for r in session.android_segment_records
            )
            if not has_existing_recording:
                message = "Recording file not found on disk"
                self._record_recording_failure(session, message)
                remove_active_session(device_id)
                return VideoRecordingResult(
                    success=False,
                    message=message,
                )

            self._finalize_current_segment(session, time.time())
            for record in session.android_segment_records:
                if not record.get("conversion_scheduled"):
                    record["conversion_scheduled"] = True
                    session.android_conversion_tasks.append(
                        asyncio.create_task(self._remux_segment_record(record))
                    )
            if session.android_conversion_tasks:
                await asyncio.gather(*session.android_conversion_tasks, return_exceptions=True)
            mp4_paths = [
                Path(record["output_path"])
                for record in session.android_segment_records
                if Path(record["output_path"]).exists()
                and Path(record["output_path"]).stat().st_size > 0
            ]

            # Emergency fallback: if no segment MP4 was produced, attempt to remux local_video_path directly
            if (
                not mp4_paths
                and output_path
                and output_path.exists()
                and output_path.stat().st_size > 0
            ):
                fallback_mp4 = (
                    output_path.parent / "recording.mp4"
                    if output_path.name != "recording.mp4"
                    else output_path.with_name("recording_converted.mp4")
                )
                if await remux_recording_to_mp4(output_path, fallback_mp4):
                    mp4_paths.append(fallback_mp4)

            if not mp4_paths:
                message = (
                    "Recording finalization failed; no complete browser-safe video was produced"
                )
                self._record_recording_failure(session, message)
                remove_active_session(device_id)
                return VideoRecordingResult(success=False, message=message)

            final_video_path = mp4_paths[0]
            output_dir = final_video_path.parent
            manifest_path = await write_recording_manifest(
                output_dir, mp4_paths, self._segment_session_offsets(session, mp4_paths)
            )

            remove_active_session(device_id)

            # Persist update to local database if Data Engine is active
            if self.ctx and self.ctx.data_engine:
                self.ctx.data_engine.record_video_stop(
                    video_id=session.video_id,
                    device_id=device_id,
                    local_video_path=final_video_path,
                    start_time=session.start_time,
                    end_time=time.time(),
                )

            return VideoRecordingResult(
                success=True,
                message=f"Recording stopped, saved {len(mp4_paths)} video segments",
                video_path=final_video_path,
                video_id=session.video_id,
                generation=session.generation,
                sealed_until=session.sealed_until,
                source_revision=f"{session.video_id}:{session.generation}:ready",
            )

        except Exception as e:
            logger.error(f"Failed to stop scrcpy recording: {e}")
            self._record_recording_failure(session, str(e))
            return abort_recording(device_id, "stop", e)

    def _ios_recording_driver(self) -> "XcodeSimulatorDriver | None":
        """The active driver when it supports native iOS capture, else ``None``."""
        from artemis.drivers.ios.xcode_driver import XcodeSimulatorDriver

        return self._driver if isinstance(self._driver, XcodeSimulatorDriver) else None

    async def _start_ios_recording(
        self, output_dir: Path | None, max_duration_seconds: int
    ) -> VideoRecordingResult:
        """Start native capture through the iOS driver (simctl or devicectl)."""
        device_id = self._get_device_id()
        if already_active := recording_already_active(device_id):
            return already_active
        driver = self._ios_recording_driver()
        if driver is None:
            return VideoRecordingResult(
                success=False,
                message="iOS recording requires the iOS driver.",
            )
        try:
            await driver.start_video_recording(
                output_dir, max_duration_seconds=max_duration_seconds
            )
        except Exception as exc:
            session = driver.recording_session
            if session is None:
                session = IosRecordingSession(
                    video_id=uuid4(),
                    device_id=device_id,
                    start_time=time.time(),
                    local_video_path=(Path(output_dir) / "recording.mov" if output_dir else None),
                    is_active=False,
                )
            if session.data_engine_start_time is None and self.ctx and self.ctx.data_engine:
                session.data_engine_start_time = (
                    self.ctx.data_engine.session_start_time or session.start_time
                )
            if self.ctx and self.ctx.data_engine:
                self.ctx.data_engine.record_video_start(
                    video_id=session.video_id,
                    device_id=device_id,
                    local_video_path=session.local_video_path or "",
                    start_time=session.start_time,
                )
            self._record_recording_failure(session, f"iOS recording failed to start: {exc}")
            return VideoRecordingResult(
                success=False, message=f"iOS recording failed to start: {exc}"
            )
        session = driver.recording_session
        if session is None:
            return VideoRecordingResult(success=False, message="iOS recorder returned no session")
        session.data_engine_start_time = (
            self.ctx.data_engine.session_start_time
            if (self.ctx and self.ctx.data_engine)
            else session.start_time
        )
        set_active_session(device_id, session)
        if self.ctx and self.ctx.data_engine:
            self.ctx.data_engine.record_video_start(
                video_id=session.video_id,
                device_id=device_id,
                local_video_path=session.local_video_path,
                start_time=session.start_time,
            )
        warning = "; ".join(session.errors) or None
        return VideoRecordingResult(
            success=True,
            message=f"iOS recording started on {device_id}",
            video_id=session.video_id,
            generation=session.generation,
            sealed_until=session.sealed_until,
            source_revision=f"{session.video_id}:{session.generation}:active",
            warning=warning,
        )

    async def _stop_ios_recording(self) -> VideoRecordingResult:
        """Finalize the driver's recorder and report the sealed manifest."""
        device_id = self._get_device_id()
        driver = self._ios_recording_driver()
        session = get_active_session(device_id)
        if session is None:
            session = driver.recording_session if driver is not None else None
        if session is None or session.video_id is None:
            return no_active_recording(device_id)
        if driver is None:
            message = "iOS recording stop requires the Xcode simulator driver."
            self._record_recording_failure(session, message)
            return VideoRecordingResult(success=False, message=message)

        async def _finalize() -> str | None:
            session.is_active = False
            return await driver.stop_video_recording()

        try:
            final_path = await asyncio.shield(_finalize())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_recording_failure(session, f"iOS recording stop failed: {exc}")
            return VideoRecordingResult(success=False, message=f"iOS recording stop failed: {exc}")
        finally:
            remove_active_session(device_id)

        missing = [
            record
            for record in session.segments
            if not (
                record.get("conversion_done")
                and Path(record["output_path"]).exists()
                and Path(record["output_path"]).stat().st_size > 0
            )
        ]
        if final_path is None or not session.segments or missing:
            message = "; ".join(session.errors) or "iOS recording finalized with no valid segments"
            self._record_recording_failure(session, message)
            return VideoRecordingResult(success=False, message=message)

        final_video_path = Path(final_path)
        if self.ctx and self.ctx.data_engine:
            self.ctx.data_engine.record_video_stop(
                video_id=session.video_id,
                device_id=device_id,
                local_video_path=final_video_path,
                start_time=session.start_time,
                end_time=session.start_time + session.sealed_until,
            )
        warning = "; ".join(session.errors) or None
        return VideoRecordingResult(
            success=True,
            message=f"iOS recording stopped, saved {len(session.segments)} segments",
            video_path=final_video_path,
            video_id=session.video_id,
            generation=session.generation,
            sealed_until=session.sealed_until,
            source_revision=f"{session.video_id}:{session.generation}:ready",
            warning=warning,
        )

    async def _extract_ios_segment(
        self,
        session: IosRecordingSession,
        start_time: float,
        end_time: float | None,
        output_path: Path | None,
    ) -> VideoRecordingResult:
        """Render a clip from sealed iOS MP4 segments only.

        The open .mov is never handed to FFmpeg: the driver seals the segment
        at the requested recording-relative end first, then finalized segments
        render through the shared timeline renderer. Recorder restarts leave
        their gaps black rather than stretching adjacent frames.
        """
        device_id = self._get_device_id()
        anchor = session.data_engine_start_time
        shift = (session.start_time - anchor) if anchor is not None else 0.0
        recording_start = float(start_time) - shift
        recording_end = float(end_time) - shift if end_time is not None else None

        driver = self._ios_recording_driver()
        if session.is_active and recording_end is not None and driver is not None:
            await driver.seal_recording_segment(through_time=recording_end)
        if session.conversion_tasks:
            await asyncio.gather(*session.conversion_tasks, return_exceptions=True)

        warning = None
        available_end = session.sealed_until
        if recording_end is None or recording_end > available_end:
            if recording_end is not None:
                warning = (
                    "Requested end clipped to latest sealed iOS capture "
                    f"({available_end + shift:.1f}s of session time)"
                )
            recording_end = available_end

        records = [dict(record) for record in session.segments]
        first_capture = min((float(record["start"]) for record in records), default=None)
        if first_capture is not None and recording_start < first_capture:
            leading = "Requested start preceded the first captured frame; clipped to capture start"
            warning = f"{warning}; {leading}" if warning else leading
            recording_start = first_capture

        if not records or recording_end <= recording_start:
            message = "Requested range does not overlap sealed iOS recording"
            self._record_recording_failure(session, message)
            return VideoRecordingResult(success=False, message=message)

        timeline = [
            {
                "path": str(record["output_path"]),
                "start": float(record["start"]),
                "end": float(record["end"]),
            }
            for record in records
            if record.get("conversion_done")
            and Path(record["output_path"]).exists()
            and float(record["end"]) > recording_start
            and float(record["start"]) < recording_end
        ]
        timeline.sort(key=lambda segment: segment["start"])
        if not timeline:
            message = "No finalized iOS MP4 segments overlap the requested range"
            self._record_recording_failure(session, message)
            return VideoRecordingResult(success=False, message=message)

        trim_output_path = (
            Path(output_path)
            if output_path is not None
            else Path(tempfile.mkdtemp(prefix="video_trimmed_", dir=get_temp_dir("trimmed_videos")))
            / "segment.mp4"
        )
        success = await render_timeline_clip(
            timeline, recording_start, recording_end, trim_output_path
        )
        if not success or not trim_output_path.exists():
            message = "Failed to render clip from sealed iOS segments"
            self._record_recording_failure(session, message)
            return VideoRecordingResult(success=False, message=message)

        actual_start = recording_start + shift
        actual_end = recording_end + shift
        file_size_mb = trim_output_path.stat().st_size / (1024 * 1024)
        return VideoRecordingResult(
            success=True,
            message=(
                f"iOS clip rendered for session range {actual_start:.1f}s to {actual_end:.1f}s"
            ),
            video_path=trim_output_path,
            file_size_mb=round(file_size_mb, 2),
            duration_seconds=round(recording_end - recording_start, 2),
            actual_start_relative_time=actual_start,
            warning=warning,
            video_id=session.video_id,
            generation=session.generation,
            sealed_until=session.sealed_until,
            source_revision=(f"{session.video_id}:{session.generation}:{round(actual_end, 3)}"),
        )

    async def cleanup(self) -> None:
        await self._driver.disconnect()
