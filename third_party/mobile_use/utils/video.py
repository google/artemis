# Copyright 2025-2026 Minitap, Inc.
# Modifications Copyright 2026 Google LLC
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
#
# Originally from mobile-use (https://github.com/minitap-ai/mobile-use).
# See third_party/mobile_use/METADATA for the upstream source and local modifications.

"""Video recording utilities for mobile devices.

Provides shared types and utilities for video recording across platforms.
"""

import asyncio
from pathlib import Path
import platform
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from artemis.utils.video import (
    get_ffmpeg_path,
    get_ffprobe_path,
    is_ffmpeg_drawtext_supported,
    is_ffmpeg_installed,
)
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_MAX_DURATION_SECONDS = 900  # 15 minutes
ANDROID_DEVICE_VIDEO_PATH = "/sdcard/screen_recording.mp4"

# Expanded for Gemini File API (Supports up to 2GB).
# Target 100MB to allow 3-5min crisp video and prevent blurring for long durations.
MAX_VIDEO_SIZE_MB = 500
MAX_VIDEO_SIZE_BYTES = MAX_VIDEO_SIZE_MB * 1024 * 1024


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
    generation: int = 0
    sealed_until: float = 0.0
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
    video_id: UUID | None = None
    generation: int | None = None
    sealed_until: float | None = None
    source_revision: str | None = None

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


def recording_already_active(device_id: str) -> VideoRecordingResult | None:
    """Failure result if the device is already recording, otherwise ``None``."""
    if has_active_session(device_id):
        return VideoRecordingResult(
            success=False,
            message=f"Recording already in progress for device {device_id}",
        )
    return None


def no_active_recording(device_id: str) -> VideoRecordingResult:
    """Failure result for an operation that needs an active recording."""
    return VideoRecordingResult(
        success=False,
        message=f"No active recording for device {device_id}",
    )


def abort_recording(device_id: str, action: str, error: Exception) -> VideoRecordingResult:
    """Drop the device's session after a failed ``action`` ("start"/"stop") and report it."""
    remove_active_session(device_id)
    return VideoRecordingResult(
        success=False,
        message=f"Failed to {action} recording: {error}",
    )


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


async def probe_duration(input_path: Path) -> float:
    """Return the container duration of a video in seconds, using ffprobe.

    Raises:
        ValueError: If ffprobe reports no parseable duration.
        OSError: If ffprobe cannot be started.
    """
    duration_cmd = [
        get_ffprobe_path(),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(input_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *duration_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return float(stdout.decode().strip())


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

    probe_cmd = [
        get_ffprobe_path(),
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=index",
        "-of",
        "csv=p=0",
        str(input_path),
    ]
    probe = await asyncio.create_subprocess_exec(
        *probe_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    probe_stdout, _ = await probe.communicate()
    if probe.returncode != 0 or not probe_stdout.strip():
        raise ValueError("Video has no audio stream")

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
            err_msg = stderr.decode(errors="replace").strip()
            concise_error = "\n".join(err_msg.splitlines()[-8:])
            logger.error(f"ffmpeg audio extraction failed: {concise_error}")
            raise RuntimeError(
                f"ffmpeg audio extraction failed (code {proc.returncode}): {concise_error}"
            )

        return output_path

    except Exception:
        raise
