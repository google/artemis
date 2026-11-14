# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Native iOS Simulator recording via ``xcrun simctl io recordVideo``.

simctl writes a variable frame-rate H.264 ``.mov`` and announces the first
processed frame on stderr as ``Recording started``; that marker anchors the
recording timeline, not process spawn. Each owned ``.mov`` is finalized to a
browser-safe CFR MP4 whose last frame is cloned across the segment's static
tail, so recorded event timestamps are never stretched. iOS capture has no
audio. Segments roll on display-dimension change, max duration, or recorder
exit; restart gaps stay gaps in rendered clips.
"""

import asyncio
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time
from typing import Any
from uuid import uuid4

from artemis.config.paths import get_temp_dir
from artemis.drivers.ios.discovery import reap_process
from artemis.utils.video import (
    get_ffmpeg_path,
    probe_video_segment,
    write_recording_manifest,
)
from third_party.mobile_use.utils.logger import get_logger
from third_party.mobile_use.utils.video import RecordingSession

logger = get_logger(__name__)

RECORDING_STARTED_MARKER = "Recording started"
# Longest a single iOS capture may run before auto-stopping (15 minutes).
DEFAULT_MAX_DURATION_SECONDS = 900
STARTUP_TIMEOUT_SECONDS = 30.0
SIGINT_FLUSH_TIMEOUT_SECONDS = 10.0
TERMINATE_TIMEOUT_SECONDS = 3.0
PROBES_TIMEOUT_SECONDS = 3.0
STALE_RECORDER_GRACE_SECONDS = 3.0
SEGMENT_PROBE_TIMEOUT_SECONDS = 30.0
# Finalization re-encodes the whole capture, so the budget scales with the
# segment's wall span: encode on a loaded host can run slower than realtime.
FFMPEG_TIMEOUT_SECONDS = 180.0
FFMPEG_TIMEOUT_PER_SPAN_SECOND = 2.0
WATCHDOG_INTERVAL_SECONDS = 0.5
MAX_CONSECUTIVE_FAILURES = 3
MIN_HEALTHY_SEGMENT_SECONDS = 10.0
MAX_CONCURRENT_CONVERSIONS = 2
STDERR_BUFFER_LINES = 200


class IosRecordingSession(RecordingSession):
    """Recording session for native iOS ``simctl`` capture."""

    segments: list[dict[str, Any]] = []
    segment_index: int = 0
    segment_started_at: float | None = None
    segment_started_monotonic: float | None = None
    anchor_monotonic: float | None = None
    stderr_task: asyncio.Task | None = None
    stderr_lines: list[str] = []
    conversion_tasks: list[asyncio.Task] = []


def _parse_display_dimensions(text: str) -> tuple[int, int] | None:
    """Largest ``IOSurface port`` (width, height) from ``simctl io enumerate``.

    ``recordVideo`` captures the device LCD without ``--display``; only that
    framebuffer reports an ``IOSurface port`` (external scene displays carry
    only ``Default width``/``height``), and its dimensions swap on rotation,
    so the largest reported surface is the segment's coded size.
    """
    best: tuple[int, int] | None = None
    best_area = 0
    for match in re.finditer(
        r"IOSurface port:\s*\n\s*width\s*=\s*(\d+)\s*\n\s*height\s*=\s*(\d+)", text
    ):
        width, height = int(match.group(1)), int(match.group(2))
        if width * height > best_area:
            best, best_area = (width, height), width * height
    return best


async def probe_display_dimensions(device_id: str) -> tuple[int, int] | None:
    """Read the recordVideo target display's pixel size via simctl."""
    try:
        process = await asyncio.create_subprocess_exec(
            "xcrun",
            "simctl",
            "io",
            device_id,
            "enumerate",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return None
    try:
        stdout, _stderr = await asyncio.wait_for(process.communicate(), PROBES_TIMEOUT_SECONDS)
    except TimeoutError:
        await _reap_probe(process)
        return None
    except asyncio.CancelledError:
        await _reap_probe(process)
        raise
    if process.returncode != 0:
        return None
    return _parse_display_dimensions(stdout.decode(errors="replace"))


_reap_probe = reap_process


async def finalize_mov_to_mp4(
    source_path: Path,
    output_path: Path,
    width: int,
    height: int,
    wall_span_seconds: float,
) -> bool:
    """Atomically finalize an owned .mov to a fixed-canvas CFR MP4.

    ``tpad`` clones the final frame across the segment's remaining wall span
    so sparse VFR tails pad without stretching any recorded event time.
    """
    if not source_path.exists() or source_path.stat().st_size == 0:
        return False
    width = max(2, int(width)) // 2 * 2
    height = max(2, int(height)) // 2 * 2
    span = max(0.001, float(wall_span_seconds))
    # fps resamples the raw VFR timeline before scale: scale buffers a frame
    # and feeding it to fps first drops the final frame on sparse sources.
    video_filter = (
        "setpts=PTS-STARTPTS,fps=30,"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:"
        f"force_divisible_by=2,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:"
        f"color=black,setsar=1,"
        f"tpad=stop_mode=clone:stop_duration={span:.3f}"
    )
    temporary_path = output_path.with_name(f"{output_path.stem}.part.mp4")
    if temporary_path.exists():
        temporary_path.unlink()
    try:
        process = await asyncio.create_subprocess_exec(
            get_ffmpeg_path(),
            "-y",
            "-i",
            str(source_path),
            "-map",
            "0:v:0",
            "-vf",
            video_filter,
            "-t",
            f"{span:.3f}",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(temporary_path),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            timeout = max(FFMPEG_TIMEOUT_SECONDS, span * FFMPEG_TIMEOUT_PER_SPAN_SECOND)
            _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
            metadata = await probe_video_segment(
                temporary_path, timeout_seconds=SEGMENT_PROBE_TIMEOUT_SECONDS
            )
        except TimeoutError:
            await reap_process(process)
            raise RuntimeError("iOS recording finalization timed out")
        except asyncio.CancelledError:
            # Reap the owned ffmpeg child and drop the partial .mp4 before
            # cancellation propagates — never leave an orphan encoder behind.
            await reap_process(process)
            if temporary_path.exists():
                temporary_path.unlink()
            raise
        valid = (
            process.returncode == 0
            and temporary_path.exists()
            and metadata.get("duration", 0) > 0
            and metadata.get("width", 0) > 0
            and metadata.get("height", 0) > 0
        )
        if valid:
            temporary_path.replace(output_path)
            return True
        logger.error(
            f"iOS recording finalization failed (code {process.returncode}): "
            f"{stderr.decode(errors='replace')[-2000:]}"
        )
    except OSError as exc:
        logger.error(f"iOS recording finalization failed: {exc}")
    except RuntimeError as exc:
        logger.error(f"iOS recording finalization failed: {exc}")
    if temporary_path.exists():
        temporary_path.unlink()
    return False


class IosScreenRecorder:
    """Owns one simctl recording lifecycle for one pinned simulator UDID."""

    def __init__(self, device_id: str):
        self._device_id = device_id
        self._session: IosRecordingSession | None = None
        self._output_dir: Path | None = None
        self._lock = asyncio.Lock()
        self._max_duration_seconds = DEFAULT_MAX_DURATION_SECONDS
        self._consecutive_failures = 0
        self._conversion_semaphore = asyncio.Semaphore(MAX_CONCURRENT_CONVERSIONS)

    @property
    def session(self) -> IosRecordingSession | None:
        """The latest session, kept after stop/failure for error reporting."""
        return self._session

    def _output_root(self) -> Path:
        if self._output_dir is None:
            raise RuntimeError("iOS recording has no output directory")
        return self._output_dir

    def _segment_source_path(self, session: IosRecordingSession, index: int) -> Path:
        output_dir = self._output_root()
        while True:
            name = "recording.mov" if index == 0 else f"recording_{index:03d}.mov"
            candidate = output_dir / name
            if not candidate.exists():
                return candidate
            index += 1
            session.segment_index = index

    def _segment_output_path(self, session: IosRecordingSession, index: int) -> Path:
        output_dir = self._output_root()
        if index == 0:
            return output_dir / "recording.mp4"
        return output_dir / f"recording_{index:03d}.mp4"

    async def _drain_stderr(
        self,
        process: asyncio.subprocess.Process,
        session: IosRecordingSession,
        first_frame: asyncio.Future,
    ) -> None:
        try:
            assert process.stderr is not None
            while True:
                line = await process.stderr.readline()
                if not line:
                    if not first_frame.done():
                        tail = "; ".join(session.stderr_lines[-3:])
                        first_frame.set_exception(
                            RuntimeError(
                                "simctl recordVideo exited before 'Recording started'"
                                + (f": {tail}" if tail else "")
                            )
                        )
                    return
                text = line.decode(errors="replace").rstrip()
                session.stderr_lines.append(text)
                if len(session.stderr_lines) > STDERR_BUFFER_LINES:
                    del session.stderr_lines[: len(session.stderr_lines) - STDERR_BUFFER_LINES]
                if RECORDING_STARTED_MARKER in text and not first_frame.done():
                    first_frame.set_result((time.time(), time.monotonic()))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not first_frame.done():
                first_frame.set_exception(exc)

    async def _reap_stale_recorders(self, *, any_device: bool = False) -> list[int]:
        """SIGINT orphaned ``simctl recordVideo`` processes still holding the host lock.

        ``recordVideo``'s "Host recording is already in progress" lock is shared
        host-wide, not per-device, so a recorder orphaned by a session that ended
        without reaping it (a killed task or console) blocks every later spawn —
        on this device or a sibling simulator. SIGINT lets the orphan flush its
        ``.mov``; a survivor is escalated to SIGKILL.
        """
        if any_device:
            pattern = r"simctl io [0-9A-Fa-f-]+ recordVideo"
        else:
            pattern = f"simctl io {self._device_id} recordVideo"
        try:
            probe = await asyncio.to_thread(
                subprocess.run,
                ["pgrep", "-f", pattern],
                capture_output=True,
                text=True,
                timeout=PROBES_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        pids = [int(token) for token in probe.stdout.split() if token.isdigit()]
        reaped: list[int] = []
        for pid in pids:
            try:
                os.kill(pid, signal.SIGINT)
            except (ProcessLookupError, PermissionError):
                continue
            reaped.append(pid)
        if not reaped:
            return []
        logger.warning(
            f"Reaping {len(reaped)} stale simctl recordVideo process(es) on "
            f"{self._device_id}: {reaped}"
        )
        deadline = time.monotonic() + STALE_RECORDER_GRACE_SECONDS
        for pid in reaped:
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.1)
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        return reaped

    async def _reap(self, process: asyncio.subprocess.Process) -> float:
        """SIGINT to flush, escalate to terminate/kill; returns end monotonic."""
        end_monotonic = time.monotonic()
        if process.returncode is not None:
            return end_monotonic
        try:
            process.send_signal(signal.SIGINT)
            await asyncio.wait_for(process.wait(), SIGINT_FLUSH_TIMEOUT_SECONDS)
        except (ProcessLookupError, TimeoutError):
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    return end_monotonic
                try:
                    await asyncio.wait_for(process.wait(), TERMINATE_TIMEOUT_SECONDS)
                except (ProcessLookupError, TimeoutError):
                    if process.returncode is None:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                        await process.wait()
        return end_monotonic

    async def _spawn_recorder(self, session: IosRecordingSession) -> None:
        """Spawn simctl recordVideo and anchor at its first-frame marker."""
        await self._reap_stale_recorders()
        try:
            await self._spawn_once(session)
        except RuntimeError as exc:
            # The host recording lock is shared across simulators, so an orphan
            # on a different UDID can still be the contender; sweep all stale
            # recorders and give the spawn one more chance.
            if "already in progress" not in str(exc):
                raise
            await self._reap_stale_recorders(any_device=True)
            await self._spawn_once(session)

    async def _spawn_once(self, session: IosRecordingSession) -> None:
        index = session.segment_index
        source_path = self._segment_source_path(session, index)
        process = await asyncio.create_subprocess_exec(
            "xcrun",
            "simctl",
            "io",
            self._device_id,
            "recordVideo",
            "--codec=h264",
            str(source_path),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        first_frame: asyncio.Future = asyncio.get_running_loop().create_future()
        session.stderr_task = asyncio.create_task(self._drain_stderr(process, session, first_frame))
        started = False
        try:
            marker_wall, marker_monotonic = await asyncio.wait_for(
                asyncio.shield(first_frame), STARTUP_TIMEOUT_SECONDS
            )
            session.process = process
            session.local_video_path = source_path
            session.segment_started_at = marker_wall
            session.segment_started_monotonic = marker_monotonic
            if session.anchor_monotonic is None:
                session.start_time = marker_wall
                session.anchor_monotonic = marker_monotonic
            dimensions = await probe_display_dimensions(self._device_id)
            if dimensions:
                session.capture_width, session.capture_height = dimensions
            logger.info(
                f"iOS recording segment {index} first frame "
                f"{marker_wall - session.start_time:.2f}s after anchor on {self._device_id}"
            )
            started = True
        finally:
            if not started:
                # Startup never completed: the spawned recorder is still
                # owned by this coroutine — drain stderr and reap it before
                # the original exception/cancellation propagates.
                if not first_frame.done():
                    first_frame.cancel()
                session.stderr_task.cancel()
                try:
                    await session.stderr_task
                except asyncio.CancelledError:
                    pass
                except (OSError, RuntimeError, ValueError, TimeoutError) as stderr_error:
                    logger.debug(f"iOS recorder stderr drain ended with an error: {stderr_error}")
                reap = asyncio.ensure_future(self._reap(process))
                try:
                    await asyncio.shield(reap)
                except asyncio.CancelledError:
                    await reap  # finish reaping even when the caller was cancelled

    def _seal_current_segment(
        self, session: IosRecordingSession, end_monotonic: float
    ) -> dict[str, Any]:
        if session.segment_started_monotonic is None or session.anchor_monotonic is None:
            raise RuntimeError("No started iOS recording segment to seal")
        record = {
            "path": session.local_video_path,
            "output_path": self._segment_output_path(session, session.segment_index),
            "start": max(0.0, session.segment_started_monotonic - session.anchor_monotonic),
            "end": max(0.0, end_monotonic - session.anchor_monotonic),
            "width": session.capture_width,
            "height": session.capture_height,
            "generation": session.generation,
            "conversion_done": False,
            "conversion_error": None,
        }
        session.segments.append(record)
        session.sealed_until = max(session.sealed_until, record["end"])
        session.generation += 1
        session.segment_index += 1
        session.segment_started_monotonic = None
        session.segment_started_at = None
        return record

    async def _convert_record(self, session: IosRecordingSession, record: dict[str, Any]) -> None:
        source = Path(record["path"])
        output = Path(record["output_path"])
        span = max(0.001, float(record["end"]) - float(record["start"]))
        async with self._conversion_semaphore:
            ok = await finalize_mov_to_mp4(
                source, output, int(record["width"] or 1080), int(record["height"] or 1920), span
            )
        if ok:
            record["conversion_done"] = True
        else:
            record["conversion_error"] = "finalization produced no valid MP4"
            session.errors.append(
                f"Segment {record['output_path']} failed finalization; raw capture kept"
            )

    async def _roll(self, session: IosRecordingSession, end_monotonic: float, reason: str) -> None:
        """Seal the current segment and start the next recorder first."""
        record = None
        if session.segment_started_monotonic is not None:
            process = session.process
            if process is not None and process.returncode is None:
                end_monotonic = await self._reap(process)
            if session.stderr_task and not session.stderr_task.done():
                session.stderr_task.cancel()
            record = self._seal_current_segment(session, end_monotonic)
            logger.info(f"Rolling iOS recording segment after {reason}")
        restarted = False
        if session.is_active:
            if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                # Spawns that keep dying young are a crash loop: stop
                # respawning (the sealed segment still converts below).
                session.is_active = False
                session.errors.append("Recording recovery limit reached; session stopped")
            else:
                try:
                    await self._spawn_recorder(session)
                    restarted = True
                except asyncio.CancelledError:
                    raise
                except (OSError, RuntimeError, TimeoutError) as exc:
                    self._consecutive_failures += 1
                    session.errors.append(f"Recorder restart failed: {exc}")
        if record is not None:
            session.conversion_tasks.append(
                asyncio.create_task(self._convert_record(session, record))
            )
        if (
            not restarted
            and session.is_active
            and self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES
        ):
            session.errors.append("Recording recovery limit reached; session stopped")
            session.is_active = False

    async def _watchdog(self, session: IosRecordingSession) -> None:
        try:
            while True:
                await asyncio.sleep(WATCHDOG_INTERVAL_SECONDS)
                async with self._lock:
                    if not session.is_active:
                        return
                    # Re-verify the trigger under the lock: a concurrent roll
                    # may have already replaced the dead process.
                    process = session.process
                    crashed = process is None or process.returncode is not None
                    dimensions = await probe_display_dimensions(self._device_id)
                    rotated = bool(
                        dimensions
                        and session.capture_width
                        and session.capture_height
                        and dimensions != (session.capture_width, session.capture_height)
                    )
                    age = time.monotonic() - (session.segment_started_monotonic or time.monotonic())
                    overdue = age >= self._max_duration_seconds
                    if crashed and age < MIN_HEALTHY_SEGMENT_SECONDS:
                        # A respawn that dies before surviving a healthy interval
                        # is a crash loop, not recovery — count it even though a
                        # successful respawn would otherwise reset the counter.
                        self._consecutive_failures += 1
                    elif not crashed and age >= MIN_HEALTHY_SEGMENT_SECONDS:
                        self._consecutive_failures = 0
                    if not crashed and not rotated and not overdue:
                        continue
                    reason = (
                        "recorder exit" if crashed else "rotation" if rotated else "duration limit"
                    )
                    await self._roll(session, time.monotonic(), reason)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.error(f"iOS recording supervisor failed: {exc}")
            session.errors.append(f"recording supervisor failed: {exc}")

    async def start(
        self,
        output_dir: Path | None = None,
        max_duration_seconds: int = DEFAULT_MAX_DURATION_SECONDS,
    ) -> IosRecordingSession:
        async with self._lock:
            if self._session is not None and self._session.is_active:
                raise RuntimeError(f"iOS recording is already active on {self._device_id}")
            output = (
                Path(output_dir)
                if output_dir is not None
                else Path(tempfile.mkdtemp(prefix="ios_recording_", dir=get_temp_dir("recordings")))
            )
            output.mkdir(parents=True, exist_ok=True)
            self._output_dir = output
            self._max_duration_seconds = max_duration_seconds
            self._consecutive_failures = 0
            session = IosRecordingSession(
                video_id=uuid4(),
                device_id=self._device_id,
                start_time=time.time(),
                local_video_path=output / "recording.mov",
                is_active=True,
            )
            self._session = session
            started = False
            try:
                await self._spawn_recorder(session)
                session.watchdog_task = asyncio.create_task(self._watchdog(session))
                started = True
                return session
            finally:
                if not started:
                    session.is_active = False

    async def seal(self, through_time: float | None = None) -> None:
        """Seal the current segment through ``through_time`` (recording-relative)."""
        async with self._lock:
            session = self._session
            if session is None or not session.is_active:
                return
            if session.anchor_monotonic is None:
                return
            if through_time is not None and session.sealed_until >= through_time:
                return
            end_monotonic = (
                session.anchor_monotonic + through_time
                if through_time is not None
                else time.monotonic()
            )
            end_monotonic = min(end_monotonic, time.monotonic())
            if (
                session.segment_started_monotonic is not None
                and end_monotonic <= session.segment_started_monotonic
            ):
                return
            await self._roll(session, end_monotonic, "seal")

    async def _finalize(self, session: IosRecordingSession) -> Path | None:
        if session.is_active:
            session.is_active = False
        if session.watchdog_task and not session.watchdog_task.done():
            session.watchdog_task.cancel()
            try:
                await session.watchdog_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.debug(f"iOS recording watchdog ended with an error: {exc}")
        end_monotonic = time.monotonic()
        if session.process is not None and session.process.returncode is None:
            end_monotonic = await self._reap(session.process)
        if session.stderr_task and not session.stderr_task.done():
            session.stderr_task.cancel()
        if session.segment_started_monotonic is not None:
            record = self._seal_current_segment(session, end_monotonic)
            session.conversion_tasks.append(
                asyncio.create_task(self._convert_record(session, record))
            )
        if session.conversion_tasks:
            await asyncio.gather(*session.conversion_tasks, return_exceptions=True)
        mp4_paths = [
            Path(record["output_path"])
            for record in session.segments
            if record.get("conversion_done")
            and Path(record["output_path"]).exists()
            and Path(record["output_path"]).stat().st_size > 0
        ]
        if not mp4_paths:
            session.errors.append("No finalized iOS recording segments")
            return None
        shift = 0.0
        if session.data_engine_start_time is not None:
            shift = session.start_time - session.data_engine_start_time
        offsets = {
            Path(record["output_path"]): max(0.0, float(record["start"]) + shift)
            for record in session.segments
            if record.get("conversion_done") and Path(record["output_path"]).exists()
        }
        output_dir = mp4_paths[0].parent
        try:
            manifest = await write_recording_manifest(
                output_dir,
                mp4_paths,
                offsets,
                probe_timeout_seconds=SEGMENT_PROBE_TIMEOUT_SECONDS,
            )
        except (OSError, TimeoutError) as exc:
            session.errors.append(f"Recording manifest probe failed: {exc}")
            return None
        if manifest is None:
            session.errors.append("Recording manifest has no valid segments")
            return None
        return mp4_paths[0]

    async def stop(self) -> Path | None:
        """Finalize all owned segments and write the version-2 manifest."""
        async with self._lock:
            session = self._session
            if session is None:
                return None
            finalize = asyncio.ensure_future(self._finalize(session))
            try:
                return await asyncio.shield(finalize)
            except asyncio.CancelledError:
                await finalize  # let cleanup finish before propagating
                raise
