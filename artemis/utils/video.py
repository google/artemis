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

"""Video recording utilities for mobile devices.

Provides shared types and utilities for video recording across platforms.
"""

import asyncio
import importlib.util
import json
from pathlib import Path
import platform
import re
import shutil
import subprocess
from typing import Any
from uuid import UUID

import cv2
from pydantic import BaseModel, ConfigDict

from artemis.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_MAX_DURATION_SECONDS = 900  # 15 minutes
ANDROID_RECORDING_SEGMENT_SECONDS = 1800
VIDEO_READY_DELAY_SECONDS = 1
ANDROID_DEVICE_VIDEO_PATH = "/sdcard/screen_recording.mp4"
ANDROID_MAX_RECORDING_DURATION_SECONDS = 180  # Android screenrecord limit

# Expanded for Gemini File API (Supports up to 2GB).
# Target 100MB to allow 3-5min crisp video and prevent blurring for long durations.
MAX_VIDEO_SIZE_MB = 500
MAX_VIDEO_SIZE_BYTES = MAX_VIDEO_SIZE_MB * 1024 * 1024


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


class _CyFunctionDetectorMeta(type):
    def __instancecheck__(self, instance):
        name = type(instance).__name__
        return (
            name
            in (
                "cyfunction",
                "cython_function_or_method",
                "builtin_function_or_method",
            )
            or "cyfunction" in name.lower()
        )


class CyFunctionDetector(metaclass=_CyFunctionDetectorMeta):
    pass


class RecordingSession(BaseModel):
    """Tracks an active video recording session."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    video_id: UUID
    device_id: str
    start_time: float
    process: Any = None
    data_engine_start_time: float | None = None
    local_video_path: Path | None = None
    capture_width: int | None = None
    capture_height: int | None = None
    android_device_path: str = ANDROID_DEVICE_VIDEO_PATH
    android_video_segments: list[Path] = []
    android_segment_index: int = 0
    android_restart_task: asyncio.Task | None = None
    watchdog_task: asyncio.Task | None = None
    android_rotation: int | None = None
    android_segment_started_at: float | None = None
    android_segment_records: list[dict[str, Any]] = []
    android_conversion_tasks: list[asyncio.Task] = []
    is_active: bool = True
    errors: list[str] = []


class VideoRecordingResult(BaseModel):
    """Result of a video recording operation."""

    success: bool
    message: str
    video_path: Path | None = None
    file_size_mb: float | None = None
    duration_seconds: float | None = None
    actual_start_relative_time: float | None = None
    warning: str | None = None

    model_config = ConfigDict(arbitrary_types_allowed=True)


# Global session storage - keyed by device_id
_active_recordings: dict[str, RecordingSession] = {}


def get_active_session(device_id: str) -> RecordingSession | None:
    """Get the active recording session for a device."""
    return _active_recordings.get(device_id)


def set_active_session(device_id: str, session: RecordingSession) -> None:
    """Set the active recording session for a device."""
    _active_recordings[device_id] = session


def remove_active_session(device_id: str) -> RecordingSession | None:
    """Remove and return the active recording session for a device."""
    return _active_recordings.pop(device_id, None)


def has_active_session(device_id: str) -> bool:
    """Check if there's an active recording session for a device."""
    return device_id in _active_recordings


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
        logger.error(
            f"Failed to remux recording segment (code {process.returncode}): "
            f"{stderr.decode(errors='replace')[-2000:]}"
        )
    except Exception as e:
        logger.error(f"Failed to remux recording segment: {e}")
    if temporary_path.exists():
        temporary_path.unlink()
    return False


async def probe_video_segment(video_path: Path) -> dict[str, float | int]:
    """Read duration and coded dimensions for a finalized segment."""
    process = await asyncio.create_subprocess_exec(
        get_ffprobe_path(),
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height:format=duration",
        "-of",
        "json",
        str(video_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _stderr = await process.communicate()
    if process.returncode != 0:
        return {}
    try:
        payload = json.loads(stdout)
        stream = (payload.get("streams") or [{}])[0]
        return {
            "duration": float((payload.get("format") or {}).get("duration") or 0),
            "width": int(stream.get("width") or 0),
            "height": int(stream.get("height") or 0),
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


async def write_recording_manifest(output_dir: Path, mp4_paths: list[Path]) -> Path | None:
    """Write the browser playlist used for orientation-aware playback."""
    segments = []
    timeline = 0.0
    for path in mp4_paths:
        if not path.exists():
            continue
        metadata = await probe_video_segment(path)
        duration = float(metadata.get("duration", 0))
        width = int(metadata.get("width", 0))
        height = int(metadata.get("height", 0))
        if duration <= 0 or width <= 0 or height <= 0:
            logger.error(f"Recording segment failed validation: {path}")
            return None
        segments.append(
            {
                "file": path.name,
                "start": round(timeline, 3),
                "duration": round(duration, 3),
                "width": width,
                "height": height,
            }
        )
        timeline += duration
    if not segments:
        return None
    manifest_path = output_dir / "recording.json"
    temporary_manifest_path = output_dir / "recording.part.json"
    temporary_manifest_path.write_text(
        json.dumps(
            {"version": 1, "duration": round(timeline, 3), "segments": segments},
            indent=2,
        ),
        encoding="utf-8",
    )
    temporary_manifest_path.replace(manifest_path)
    return manifest_path


async def render_timeline_clip(
    segments: list[dict[str, Any]],
    start_time: float,
    end_time: float,
    output_path: Path,
    canvas_width: int = 720,
    canvas_height: int = 1280,
) -> bool:
    """Render one continuous analyzer clip from orientation-aware segments.

    Only the requested time window is decoded. This keeps the video analyzer's
    single-MP4 contract without ever re-encoding the full recording.
    """
    overlaps = []
    for segment in segments:
        path = Path(segment["path"])
        seg_start = float(segment["start"])
        seg_end = float(segment["end"])
        overlap_start = max(start_time, seg_start)
        overlap_end = min(end_time, seg_end)
        if path.exists() and overlap_end > overlap_start:
            overlaps.append((path, overlap_start - seg_start, overlap_end - seg_start))
    if not overlaps:
        return False

    command = [get_ffmpeg_path(), "-y", "-fflags", "+genpts"]
    for path, _start, _end in overlaps:
        command.extend(["-i", str(path)])

    filter_parts = []
    labels = []
    for index, (_path, local_start, local_end) in enumerate(overlaps):
        label = f"v{index}"
        filter_parts.append(
            f"[{index}:v:0]trim=start={local_start:.6f}:end={local_end:.6f},"
            "setpts=PTS-STARTPTS,"
            f"scale={canvas_width}:{canvas_height}:force_original_aspect_ratio=decrease:"
            "force_divisible_by=2,"
            f"pad={canvas_width}:{canvas_height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=1,fps=15,format=yuv420p[{label}]"
        )
        labels.append(f"[{label}]")
    if len(labels) == 1:
        filter_parts.append(f"{labels[0]}null[outv]")
    else:
        filter_parts.append(f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0[outv]")

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


def detect_video_tools_enabled() -> bool:
    """Check if both scrcpy and ffmpeg are available to enable automated video features."""
    return is_ffmpeg_installed() and is_scrcpy_installed()


class FFmpegNotInstalledError(Exception):
    """Raised when ffmpeg is required but not installed."""

    def __init__(self):
        os_name = platform.system().lower()
        if os_name == "darwin":  # macOS
            install_instructions = "brew install ffmpeg"
        elif os_name == "windows":
            install_instructions = "Download from https://www.ffmpeg.org/download.html"
        else:  # Linux and others
            install_instructions = (
                "Install via your package manager (e.g., apt install ffmpeg,"
                " dnf install ffmpeg) or download from"
                " https://www.ffmpeg.org/download.html"
            )

        message = (
            "\n\n❌ ffmpeg is required for video recording but is not"
            " installed.\n\nPlease install ffmpeg first:\n  →"
            f" {install_instructions}\n\nAfter installation, restart Artemis.\n"
        )
        super().__init__(message)


def check_ffmpeg_available() -> None:
    """Check if ffmpeg is installed and raise an error if not.

    Raises:
        FFmpegNotInstalledError: If ffmpeg is not found in PATH.
    """
    if not is_ffmpeg_installed():
        raise FFmpegNotInstalledError()


async def concatenate_videos(segments: list[Path], output_path: Path) -> bool:
    """Concatenate multiple video segments using ffmpeg."""
    if not segments:
        return False

    if len(segments) == 1:
        shutil.move(segments[0], output_path)
        return True

    list_file = output_path.parent / "segments.txt"
    with open(list_file, "w") as f:
        for segment in segments:
            f.write(f"file '{segment}'\n")

    try:
        process = await asyncio.create_subprocess_exec(
            get_ffmpeg_path(),
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-c",
            "copy",
            str(output_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.wait()
        return output_path.exists()
    except Exception as e:
        logger.error(f"Failed to concatenate videos: {e}")
        return False
    finally:
        if list_file.exists():
            list_file.unlink()


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


async def trim_video(
    input_path: Path,
    start_time: float,
    end_time: float | None,
    output_path: Path,
    fast_copy: bool = True,
) -> bool:
    """Trim a video file using ffmpeg.

    Uses fast stream copy (-c copy) by default with fallback to re-encoding.
    """
    try:
        cmd = [
            get_ffmpeg_path(),
            "-y",
            "-ss",
            str(start_time),
            "-i",
            str(input_path),
        ]
        if end_time is not None:
            duration = end_time - start_time
            cmd.extend(["-t", str(duration)])

        if fast_copy:
            cmd.extend(["-c", "copy", str(output_path)])
        else:
            cmd.extend(
                [
                    "-vf",
                    "setpts=PTS-STARTPTS",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    str(output_path),
                ]
            )

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode == 0 and output_path.exists():
            return True

        if fast_copy:
            logger.warning(
                f"ffmpeg fast copy trim failed (code {process.returncode}),"
                f" falling back to re-encoding: {stderr.decode()[:200]}"
            )
            return await trim_video(input_path, start_time, end_time, output_path, fast_copy=False)

        logger.error(f"ffmpeg trim failed (code {process.returncode}): {stderr.decode()}")
        return False
    except Exception as e:
        logger.error(f"Failed to trim video: {e}")
        return False


def cleanup_video_segments(segments: list[Path], keep_path: Path | None = None) -> None:
    """Clean up temporary video segments, optionally keeping one path."""
    for segment in segments:
        try:
            if segment.exists() and segment != keep_path:
                segment.unlink()
                if segment.parent.exists() and not any(segment.parent.iterdir()):
                    segment.parent.rmdir()
        except Exception:
            pass


async def compress_video_for_api(
    input_path: Path,
    target_size_bytes: int = MAX_VIDEO_SIZE_BYTES,
    force_compress: bool = False,
    start_offset_seconds: float = 0.0,
    slowdown_factor: float = 1.0,
) -> Path:
    """Compress a video to fit within API size limits using ffmpeg.

    Uses a two-pass approach:
    1. First check if video is already small enough
    2. If not, compress with reduced resolution and bitrate

    Args:
        input_path: Path to the input video file
        target_size_bytes: Target maximum file size in bytes
        force_compress: If True, always perform compression (e.g., to extract
          frames at 15fps)
        start_offset_seconds: Offset to add to the burned-in timestamp
        slowdown_factor: Factor to slow down the video by (e.g. 5.0 for 5x
          slower) to increase API frame sampling rate

    Returns:
        Path to the compressed video (may be same as input if no compression
        needed)
    """
    if not input_path.exists():
        raise FileNotFoundError(f"Video file not found: {input_path}")

    current_size = input_path.stat().st_size
    logger.info(f"Video size: {current_size / 1024 / 1024:.2f} MB")

    if current_size <= target_size_bytes and not force_compress and slowdown_factor == 1.0:
        logger.info(
            "Video already within size limit and force_compress=False, no compression needed"
        )
        return input_path

    logger.info(
        f"Compressing video to fit within {target_size_bytes / 1024 / 1024:.1f}"
        f" MB (slowdown={slowdown_factor})"
    )

    output_path = input_path.parent / f"compressed_{input_path.name}"

    # Use Constant Rate Factor (CRF) for high-fidelity UI text readability.
    # CRF dynamically allocates bitrates depending on scene motion.
    logger.info("Compressing video using CRF=26 for high-fidelity UI text readability.")

    # Compress with ffmpeg: reduce resolution to 720p max, use CRF
    # and check if drawtext is supported
    vf_parts = ["setpts=PTS-STARTPTS", "scale='min(720,iw)':'-2'"]
    if is_ffmpeg_drawtext_supported():
        vf_parts.append(
            "drawtext=text='TS\\:"
            f" %{{expr_int_format\\:trunc(t+{start_offset_seconds})\\:d}}"
            " s':x=w-tw-30:y=120+th+20:fontcolor=white@0.6:fontsize=44:borderw=4:"
            "bordercolor=red@0.6:box=1:boxcolor=yellow@0.4:boxborderw=10:font='Sans"
            " Bold'"
        )
    else:
        logger.warning(
            "ffmpeg 'drawtext' filter not supported on this system. Skipping"
            " burned-in timestamp overlay."
        )

    if slowdown_factor != 1.0:
        vf_parts.append(f"setpts={slowdown_factor}*PTS")

    vf_filter = ",".join(vf_parts)

    compress_cmd = [
        get_ffmpeg_path(),
        "-y",
        "-i",
        str(input_path),
        "-vf",
        vf_filter,
        "-r",
        "15",  # Set framerate to 15 fps
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "26",  # High quality for UI elements
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "64k",
        str(output_path),
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *compress_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            err_msg = stderr.decode().strip()
            logger.error(f"ffmpeg compression failed (code {proc.returncode}): {err_msg}")
            if force_compress or slowdown_factor != 1.0:
                raise RuntimeError(
                    f"Video compression/slowdown failed (code {proc.returncode}): {err_msg}"
                )
            return input_path  # Return original if compression fails and was optional

        new_size = output_path.stat().st_size
        logger.info(
            f"Compressed: {current_size / 1024 / 1024:.2f} MB -> {new_size / 1024 / 1024:.2f} MB"
        )

        return output_path

    except Exception as e:
        logger.error(f"Video compression failed: {e}")
        if force_compress or slowdown_factor != 1.0:
            raise RuntimeError(f"Video compression/slowdown failed: {e}")
        return input_path  # Return original if compression fails and was optional


async def extract_audio_from_video(input_path: Path) -> Path:
    """Extract audio from a video file using ffmpeg.

    Saves as .mp3.
    """
    if not input_path.exists():
        raise FileNotFoundError(f"Video file not found: {input_path}")

    output_path = input_path.parent / f"audio_{input_path.stem}.mp3"
    logger.info(f"Extracting audio from {input_path} to {output_path}")

    cmd = [
        get_ffmpeg_path(),
        "-y",
        "-i",
        str(input_path),
        "-vn",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "128k",
        str(output_path),
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            err_msg = stderr.decode().strip()
            logger.error(f"ffmpeg audio extraction failed: {err_msg}")
            raise RuntimeError(
                f"ffmpeg audio extraction failed (code {proc.returncode}): {err_msg}"
            )

        return output_path

    except Exception as e:
        logger.error(f"Audio extraction failed: {e}")
        raise e


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
