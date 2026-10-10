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

"""scrcpy recording, remuxing, timeline rendering and frame extraction.

Recording session types and the session registry live in
``third_party.mobile_use.utils.video``.
"""

import asyncio
import importlib.util
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any

import cv2

from artemis.utils.cython_compat import CyFunctionDetector
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

ANDROID_RECORDING_SEGMENT_SECONDS = 1800
# The recording marker closely tracks the first frame; startup measurements
# put the fallback near three quarters of a second after process creation.
SCRCPY_RECORDING_STARTED_MARKER = "Recording started"
SCRCPY_STARTUP_FALLBACK_SECONDS = 0.75
SCRCPY_STARTUP_TIMEOUT_SECONDS = 6.0
# Ignore small timing differences at segment boundaries.
TIMELINE_GAP_EPSILON_SECONDS = 0.05


def build_scrcpy_record_command(
    scrcpy_executable: str,
    device_id: str,
    output_path: Path,
    video_bit_rate: str = "2M",
    lock_capture_orientation: bool = True,
) -> list[str]:
    """Build the shared scrcpy command used by all recording paths.

    Each recording segment locks the orientation present when scrcpy starts.
    The recording supervisor starts a new segment when Android rotates, so a
    segment never contains multiple coded sizes while still displaying the app
    in its natural orientation.
    """
    command = [
        scrcpy_executable,
        "--serial",
        device_id,
        "--no-window",
        "--record",
        str(output_path),
        "--record-format",
        "mkv",
        "--video-bit-rate",
        video_bit_rate,
    ]
    if lock_capture_orientation:
        command.append("--capture-orientation=@")
    return command


async def await_scrcpy_first_frame(
    process: Any,
    spawned_at: float,
    *,
    timeout: float = SCRCPY_STARTUP_TIMEOUT_SECONDS,
) -> float:
    """Estimate when the first frame of a scrcpy recording reached the host."""
    fallback = spawned_at + SCRCPY_STARTUP_FALLBACK_SECONDS
    reader = getattr(process, "stdout", None)
    if not isinstance(reader, asyncio.StreamReader):
        await asyncio.sleep(0.8)
        return fallback

    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.warning(
                f"scrcpy did not report '{SCRCPY_RECORDING_STARTED_MARKER}' within "
                f"{timeout:.1f}s; assuming the first frame at spawn + "
                f"{SCRCPY_STARTUP_FALLBACK_SECONDS:.2f}s"
            )
            return fallback
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=remaining)
        except TimeoutError:
            continue
        if not line:
            try:
                await asyncio.wait_for(process.wait(), timeout=1.0)
            except (AttributeError, ProcessLookupError, TimeoutError):
                pass
            return fallback
        if SCRCPY_RECORDING_STARTED_MARKER in line.decode(errors="replace"):
            return max(spawned_at, time.time())


def is_ffmpeg_installed() -> bool:
    """Check if ffmpeg is available via imageio_ffmpeg or system PATH."""

    if importlib.util.find_spec("imageio_ffmpeg") is not None:
        return True
    return shutil.which("ffmpeg") is not None


def get_ffmpeg_path() -> str:
    """Get the path to the ffmpeg executable."""
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return "ffmpeg"


def get_ffprobe_path() -> str:
    """Get ffprobe for lightweight segment metadata inspection."""
    return shutil.which("ffprobe") or "ffprobe"


async def normalize_recording_to_mp4(
    source_path: Path,
    output_path: Path,
    capture_width: int | None,
    capture_height: int | None,
) -> bool:
    """Re-encode a recording onto one fixed canvas for browser playback."""
    if not source_path.exists():
        return False

    width = max(2, int(capture_width or 1080)) // 2 * 2
    height = max(2, int(capture_height or 1920)) // 2 * 2
    video_filter = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:"
        f"force_divisible_by=2,pad={width}:{height}:"
        "(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
    )

    try:
        process = await asyncio.create_subprocess_exec(
            get_ffmpeg_path(),
            "-y",
            "-fflags",
            "+genpts",
            "-i",
            str(source_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-vf",
            video_filter,
            "-r",
            "30",
            "-fps_mode",
            "cfr",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(output_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await process.communicate()
        if process.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
            return True

        error_detail = stderr.decode(errors="replace")[-4000:]
        logger.error(
            f"Failed to normalize recording to MP4 (code {process.returncode}): {error_detail}"
        )
        if output_path.exists():
            output_path.unlink()
        return False
    except Exception as e:
        logger.error(f"Failed to normalize recording to MP4: {e}")
        if output_path.exists():
            output_path.unlink()
        return False


async def remux_recording_to_mp4(source_path: Path, output_path: Path) -> bool:
    """Atomically publish a fixed-dimension recording segment as MP4."""
    if not source_path.exists() or source_path.stat().st_size == 0:
        return False
    temporary_path = output_path.with_name(f"{output_path.stem}.part{output_path.suffix}")
    if temporary_path.exists():
        temporary_path.unlink()
    try:
        process = await asyncio.create_subprocess_exec(
            get_ffmpeg_path(),
            "-y",
            "-fflags",
            "+genpts",
            "-i",
            str(source_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(temporary_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await process.communicate()
        valid = (
            process.returncode == 0
            and temporary_path.exists()
            and temporary_path.stat().st_size > 0
        )
        if valid:
            temporary_path.replace(output_path)
            return True

        logger.warning(
            f"Direct remux failed for {source_path} (code {process.returncode}), "
            "attempting transcode fallback with ffmpeg..."
        )
        # Fallback to ultrafast re-encoding in case MKV container was truncated or has timestamp irregularities
        fallback_proc = await asyncio.create_subprocess_exec(
            get_ffmpeg_path(),
            "-y",
            "-fflags",
            "+genpts+discardcorrupt",
            "-i",
            str(source_path),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(temporary_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _fout, _ferr = await fallback_proc.communicate()
        if (
            fallback_proc.returncode == 0
            and temporary_path.exists()
            and temporary_path.stat().st_size > 0
        ):
            temporary_path.replace(output_path)
            return True

        logger.error(
            f"Failed to remux recording segment (code {process.returncode}): "
            f"{stderr.decode(errors='replace')[-2000:]}"
        )
    except Exception as e:
        logger.error(f"Failed to remux recording segment: {e}")
    if temporary_path.exists():
        temporary_path.unlink()
    return False


def _probe_video_segment_cv2(video_path: Path) -> dict[str, float | int]:
    """Read metadata from a finalized CFR video when ffprobe is unavailable."""
    capture = cv2.VideoCapture(str(video_path))
    try:
        width = capture.get(cv2.CAP_PROP_FRAME_WIDTH)
        height = capture.get(cv2.CAP_PROP_FRAME_HEIGHT)
        fps = capture.get(cv2.CAP_PROP_FPS)
        frame_count = capture.get(cv2.CAP_PROP_FRAME_COUNT)
    finally:
        capture.release()
    if not (
        math.isfinite(width)
        and math.isfinite(height)
        and math.isfinite(fps)
        and math.isfinite(frame_count)
        and width > 0
        and height > 0
        and fps > 0
        and frame_count > 0
    ):
        return {}
    return {
        "duration": frame_count / fps,
        "width": int(width),
        "height": int(height),
    }


async def probe_video_segment(
    video_path: Path, *, timeout_seconds: float | None = None
) -> dict[str, float | int]:
    """Read duration and coded dimensions for a finalized segment.

    ``timeout_seconds`` bounds the owned ffprobe child: on timeout or
    cancellation the subprocess is killed and drained before propagating.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            get_ffprobe_path(),
            "-v",
            "error",
            "-show_entries",
            "stream=width,height,duration,codec_type:format=duration",
            "-of",
            "json",
            str(video_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return await asyncio.to_thread(_probe_video_segment_cv2, video_path)
    if timeout_seconds is None:
        # Default callers keep the original unbounded communicate behaviour.
        stdout, _stderr = await process.communicate()
    else:
        try:
            stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout_seconds)
        except (TimeoutError, asyncio.CancelledError):
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            try:
                await process.communicate()
            except (OSError, RuntimeError, TimeoutError) as drain_error:
                logger.debug(f"ffprobe drain after kill failed: {drain_error}")
            raise
    if process.returncode != 0:
        return {}
    try:
        payload = json.loads(stdout)
        streams = payload.get("streams") or []
        video_stream = next(
            (s for s in streams if s.get("width") and s.get("height")),
            next(
                (s for s in streams if s.get("codec_type") == "video"),
                streams[0] if streams else {},
            ),
        )
        duration = float(
            (payload.get("format") or {}).get("duration") or video_stream.get("duration") or 0
        )
        return {
            "duration": duration,
            "width": int(video_stream.get("width") or 0),
            "height": int(video_stream.get("height") or 0),
        }
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


async def get_android_display_state(device_id: str) -> tuple[int, int, int] | None:
    """Return Android's current (rotation, width, height)."""
    try:
        process = await asyncio.create_subprocess_exec(
            "adb",
            "-s",
            device_id,
            "shell",
            "dumpsys",
            "window",
            "displays",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=3.0)
        text = stdout.decode(errors="replace")
        rotation_match = re.search(r"\bmRotation=(\d+)", text)
        size_match = re.search(r"\bcur=(\d+)x(\d+)", text)
        if rotation_match and size_match:
            return (
                int(rotation_match.group(1)),
                int(size_match.group(1)),
                int(size_match.group(2)),
            )
    except (OSError, TimeoutError):
        pass
    return None


RECORDING_MANIFEST_VERSION = 2


async def write_recording_manifest(
    output_dir: Path,
    mp4_paths: list[Path],
    segment_offsets: dict[Path, float] | None = None,
    probe_timeout_seconds: float | None = None,
) -> Path | None:
    """Write the browser playlist used for orientation-aware playback.

    ``segment_offsets`` maps each MP4 path to the wall-clock offset (seconds)
    of its first frame relative to the DataEngine session start. Segments are
    still listed back to back via ``start``/``duration`` for the legacy player
    timeline, and additionally carry ``offset_ms``/``duration_ms`` so the UI
    can map a session-relative step time onto the right segment even though
    scrcpy restarts (rotation, crash recovery) leave gaps between segments.
    A segment without a known offset is assumed to follow the previous one
    without a gap.
    """
    segments = []
    timeline = 0.0
    session_cursor_ms = 0
    offsets = {
        Path(path).resolve(): float(offset) for path, offset in (segment_offsets or {}).items()
    }
    for path in mp4_paths:
        if not path.exists():
            continue
        # Forward the timeout only when supplied so default callers keep the
        # historical probe signature.
        metadata = await (
            probe_video_segment(path, timeout_seconds=probe_timeout_seconds)
            if probe_timeout_seconds is not None
            else probe_video_segment(path)
        )
        duration = float(metadata.get("duration", 0))
        width = int(metadata.get("width", 0))
        height = int(metadata.get("height", 0))
        if duration <= 0 or width <= 0 or height <= 0:
            logger.warning(f"Recording segment skipped due to invalid metadata: {path}")
            continue
        duration_ms = int(round(duration * 1000))
        known_offset = offsets.get(path.resolve())
        offset_ms = (
            max(0, int(round(known_offset * 1000)))
            if known_offset is not None
            else session_cursor_ms
        )
        segments.append(
            {
                "file": path.name,
                "start": round(timeline, 3),
                "duration": round(duration, 3),
                "offset_ms": offset_ms,
                "duration_ms": duration_ms,
                "width": width,
                "height": height,
            }
        )
        timeline += duration
        session_cursor_ms = offset_ms + duration_ms
    if not segments:
        return None
    manifest_path = output_dir / "recording.json"
    temporary_manifest_path = output_dir / "recording.part.json"
    temporary_manifest_path.write_text(
        json.dumps(
            {
                "version": RECORDING_MANIFEST_VERSION,
                "duration": round(timeline, 3),
                "session_offset_ms": segments[0]["offset_ms"],
                "session_end_ms": session_cursor_ms,
                "segments": segments,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    temporary_manifest_path.replace(manifest_path)
    return manifest_path


def plan_timeline_pieces(
    segments: list[dict[str, Any]], start_time: float, end_time: float
) -> list[tuple[Any, ...]]:
    """Split a recording window into file ranges and gaps between segments.

    File pieces are ``("file", path, local_start, local_end)``; gaps are
    ``("gap", seconds)``. Include leading and trailing gaps to preserve timeline
    offsets, ignoring gaps below the timing tolerance. Return an empty list if
    no existing segment overlaps the window.
    """
    pieces: list[tuple[Any, ...]] = []
    cursor = start_time
    for segment in sorted(segments, key=lambda item: float(item["start"])):
        path = Path(segment["path"])
        seg_start = float(segment["start"])
        seg_end = float(segment["end"])
        overlap_start = max(start_time, seg_start)
        overlap_end = min(end_time, seg_end)
        if not path.exists() or overlap_end <= overlap_start:
            continue
        if overlap_start - cursor > TIMELINE_GAP_EPSILON_SECONDS:
            pieces.append(("gap", overlap_start - cursor))
        pieces.append(("file", path, overlap_start - seg_start, overlap_end - seg_start))
        cursor = max(cursor, overlap_end)
    if not any(piece[0] == "file" for piece in pieces):
        return []
    if end_time - cursor > TIMELINE_GAP_EPSILON_SECONDS:
        pieces.append(("gap", end_time - cursor))
    return pieces


async def render_timeline_clip(
    segments: list[dict[str, Any]],
    start_time: float,
    end_time: float,
    output_path: Path,
    canvas_width: int = 720,
    canvas_height: int = 1280,
    fps: int = 15,
) -> bool:
    """Render one continuous analyzer clip from orientation-aware segments.

    Decode only the requested window and fill recording gaps with black frames
    to keep clip timestamps aligned with the recording timeline.
    """
    pieces = plan_timeline_pieces(segments, start_time, end_time)
    if not pieces:
        return False

    command = [get_ffmpeg_path(), "-y", "-fflags", "+genpts"]
    filter_parts = []
    labels = []
    for index, piece in enumerate(pieces):
        label = f"v{index}"
        if piece[0] == "gap":
            duration = float(piece[1])
            command.extend(
                [
                    "-f",
                    "lavfi",
                    "-i",
                    f"color=c=black:s={canvas_width}x{canvas_height}:r={fps}:d={duration:.3f}",
                ]
            )
            filter_parts.append(
                f"[{index}:v:0]trim=duration={duration:.6f},setpts=PTS-STARTPTS,"
                f"setsar=1,format=yuv420p[{label}]"
            )
        else:
            _kind, path, local_start, local_end = piece
            command.extend(["-i", str(path)])
            filter_parts.append(
                f"[{index}:v:0]trim=start={local_start:.6f}:end={local_end:.6f},"
                "setpts=PTS-STARTPTS,"
                f"scale={canvas_width}:{canvas_height}:force_original_aspect_ratio=decrease:"
                "force_divisible_by=2,"
                f"pad={canvas_width}:{canvas_height}:(ow-iw)/2:(oh-ih)/2:color=black,"
                f"setsar=1,fps={fps},format=yuv420p[{label}]"
            )
        labels.append(f"[{label}]")
    # ``concat`` does not advertise a frame rate on its output link, so without an
    # explicit rate ffmpeg falls back to 25 fps CFR and duplicates frames, breaking
    # the ``start_time + frame_index / fps`` mapping the analyzer relies on.
    if len(labels) == 1:
        filter_parts.append(f"{labels[0]}fps={fps}[outv]")
    else:
        filter_parts.append(f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0,fps={fps}[outv]")

    command.extend(
        [
            "-filter_complex",
            ";".join(filter_parts),
            "-map",
            "[outv]",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await process.communicate()
        if process.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
            return True
        logger.error(
            f"Failed to render analyzer timeline clip (code {process.returncode}): "
            f"{stderr.decode(errors='replace')[-3000:]}"
        )
    except Exception as e:
        logger.error(f"Failed to render analyzer timeline clip: {e}")
    if output_path.exists():
        output_path.unlink()
    return False


def is_scrcpy_installed() -> bool:
    """Check if scrcpy is available in the system PATH."""

    return shutil.which("scrcpy") is not None


def detect_video_tools_enabled(platform: str = "android") -> bool:
    """Check whether the platform's recording toolchain is available locally.

    iOS uses native ``xcrun simctl io recordVideo`` capture plus the bundled
    FFmpeg for post-processing; Android requires scrcpy plus FFmpeg.
    """
    if str(platform).strip().lower() == "ios":
        return (
            sys.platform == "darwin" and shutil.which("xcrun") is not None and is_ffmpeg_installed()
        )
    return is_ffmpeg_installed() and is_scrcpy_installed()


_drawtext_supported: bool | None = None


def is_ffmpeg_drawtext_supported() -> bool:
    """Check if ffmpeg supports the drawtext filter (requires libfreetype/libharfbuzz)."""
    global _drawtext_supported
    if _drawtext_supported is not None:
        return _drawtext_supported
    try:
        res = subprocess.run(
            [get_ffmpeg_path(), "-filters"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        _drawtext_supported = " drawtext " in res.stdout
    except Exception:
        _drawtext_supported = False
    return _drawtext_supported


def extract_keyframes_from_video(
    video_path: Path | str,
    fps: float = 1.0,
    max_frames: int = 45,
    max_dimension: int = 1080,
) -> list[tuple[float, bytes]]:
    """Extracts keyframes sampled at `fps` intervals.

    Returns:
        List of tuples: (timestamp_seconds, jpeg_bytes).
    """

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        logger.error(f"Failed to open video file for keyframe extraction: {video_path}")
        return []

    video_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    step = max(1, int(round(video_fps / fps)))

    frames: list[tuple[float, bytes]] = []
    frame_idx = 0

    while cap.isOpened() and len(frames) < max_frames:
        ret, frame = cap.read()
        if not ret:
            break

        if frame_idx % step == 0:
            timestamp_sec = frame_idx / video_fps
            h, w = frame.shape[:2]
            if max(h, w) > max_dimension:
                scale = max_dimension / max(h, w)
                frame = cv2.resize(
                    frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA
                )

            _, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            frames.append((timestamp_sec, buf.tobytes()))

        frame_idx += 1

    cap.release()
    return frames


def extract_frames_at_timestamps(
    video_path: Path | str,
    timestamps: list[float],
    *,
    max_frames: int = 30,
    max_dimension: int = 1080,
) -> list[tuple[float, bytes]]:
    """Seek to exact video-relative timestamps and return JPEG evidence frames."""

    if max_frames <= 0:
        return []
    requested = sorted(
        {
            round(float(timestamp), 3)
            for timestamp in timestamps
            if isinstance(timestamp, (int, float)) and float(timestamp) >= 0.0
        }
    )[:max_frames]
    if not requested:
        return []

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        logger.error(f"Failed to open video file for targeted frame extraction: {video_path}")
        return []

    frames: list[tuple[float, bytes]] = []
    try:
        for timestamp in requested:
            cap.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000.0)
            ret, frame = cap.read()
            if not ret:
                continue
            h, w = frame.shape[:2]
            if max(h, w) > max_dimension:
                scale = max_dimension / max(h, w)
                frame = cv2.resize(
                    frame,
                    (max(1, int(w * scale)), max(1, int(h * scale))),
                    interpolation=cv2.INTER_AREA,
                )
            encoded, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
            if encoded:
                frames.append((timestamp, buffer.tobytes()))
    finally:
        cap.release()
    return frames
