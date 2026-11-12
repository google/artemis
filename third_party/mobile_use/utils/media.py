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

import json
import os
from pathlib import Path
from typing import Any
import subprocess
import tempfile

from third_party.mobile_use.utils.logger import get_logger
from PIL import Image

logger = get_logger(__name__)

USE_FFMPEG_GIF = os.environ.get("USE_FFMPEG_GIF", "").lower() in (
    "1",
    "true",
    "yes",
)


def quantize_and_save_gif_from_paths(
    image_paths: list[Path],
    output_path: Path,
    colors: int = 128,
    duration: int = 100,
) -> None:
    """Create an optimized GIF from image file paths.

    By default uses PIL (loads all frames into memory).
    Set USE_FFMPEG_GIF=1 env var to use ffmpeg for memory-efficient streaming.

    Args:
        image_paths: List of paths to image files (must be sorted in desired
          order)
        output_path: Path where the GIF will be saved
        colors: Number of colors to use in quantization (lower = smaller file)
        duration: Duration of each frame in milliseconds

    Raises:
        ValueError: If image_paths list is empty
        RuntimeError: If ffmpeg fails (when USE_FFMPEG_GIF is enabled)
    """
    if not image_paths:
        raise ValueError("image_paths must not be empty")

    if USE_FFMPEG_GIF:
        _save_gif_ffmpeg(image_paths, output_path, colors, duration)
    else:
        _save_gif_pillow(image_paths, output_path, colors, duration)


def _save_gif_pillow(
    image_paths: list[Path],
    output_path: Path,
    colors: int,
    duration: int,
) -> None:
    """Create GIF using PIL (loads all frames into memory)."""

    def frame_generator():
        for path in image_paths:
            with Image.open(path) as img:
                if img.mode != "RGB":
                    img = img.convert("RGB")
                yield img.quantize(colors=colors, method=2)

    frames = frame_generator()
    first_frame = next(frames)
    first_frame.save(
        output_path,
        save_all=True,
        append_images=frames,
        loop=0,
        optimize=True,
        duration=duration,
    )


def _save_gif_ffmpeg(
    image_paths: list[Path],
    output_path: Path,
    colors: int,
    duration: int,
) -> None:
    """Create GIF using ffmpeg (memory-efficient streaming)."""
    fps = 1000 / duration

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        for i, path in enumerate(image_paths):
            f.write(f"file '{path.absolute()}'\n")
            # Last file needs no duration (ffmpeg concat demuxer uses it as the final frame)
            if i < len(image_paths) - 1:
                f.write(f"duration {duration / 1000}\n")
        # Repeat last file to ensure it's included (ffmpeg concat demuxer quirk)
        f.write(f"file '{image_paths[-1].absolute()}'\n")
        concat_file = Path(f.name)

    palette_path = output_path.with_suffix(".palette.png")

    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_file),
                "-vf",
                f"palettegen=max_colors={min(colors, 256)}:stats_mode=diff",
                str(palette_path),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg palette generation failed: {result.stderr}")

        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_file),
                "-i",
                str(palette_path),
                "-lavfi",
                f"fps={fps},paletteuse=dither=bayer:bayer_scale=5",
                "-loop",
                "0",
                str(output_path),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg GIF creation failed: {result.stderr}")

    finally:
        concat_file.unlink(missing_ok=True)
        if palette_path.exists():
            palette_path.unlink()


def create_gif_from_trace_folder(trace_folder_path: Path):
    image_files: list[Path] = []

    for file in trace_folder_path.iterdir():
        if file.suffix == ".jpeg":
            image_files.append(file)

    image_files.sort(key=lambda f: int(f.stem))

    logger.info(f"Found {len(image_files)} images to compile")

    if not image_files:
        return

    gif_path = trace_folder_path / "trace.gif"
    quantize_and_save_gif_from_paths(image_files, gif_path)
    logger.info(f"GIF created at {gif_path}")


def remove_images_from_trace_folder(trace_folder_path: Path):
    for file in trace_folder_path.iterdir():
        if file.suffix == ".jpeg":
            file.unlink()


def create_steps_json_from_trace_folder(trace_folder_path: Path) -> int:
    """Compile legacy timestamp-named step files without consuming manifests.

    Returns the number of compiled steps.
    """
    steps = []
    for file in trace_folder_path.iterdir():
        if file.suffix == ".json" and file.stem.isdigit():
            with open(file, encoding="utf-8", errors="ignore") as f:
                json_content = f.read()
                steps.append({"timestamp": int(file.stem), "data": json_content})

    steps.sort(key=lambda f: f["timestamp"])

    logger.info(f"Found {len(steps)} steps to compile")

    with open(trace_folder_path / "steps.json", "w", encoding="utf-8", errors="ignore") as f:
        f.write(json.dumps(steps))
    return len(steps)


def _step_action_label(action_taken: Any) -> str:
    if isinstance(action_taken, dict):
        for key in ("action", "name", "tool"):
            value = action_taken.get(key)
            if isinstance(value, str) and value:
                return value
    if isinstance(action_taken, str) and action_taken:
        return action_taken
    return "-"


def create_steps_json_from_step_records(trace_folder_path: Path, steps: list[Any]) -> int:
    """Write ``steps.json`` from persisted DataEngine step records.

    Steps are stored in the session database rather than as per-step files in
    the trace folder, so this is the source of the compiled step history. Each
    entry carries the ``step`` / ``action`` / ``motivation`` keys that
    ``artemis trace view`` renders, plus the raw recorded fields.
    """
    entries = []
    for step in steps:
        motivation = getattr(step, "summary", None) or getattr(step, "operator_raw_thinking", None)
        entries.append(
            {
                "step": step.step_number,
                "timestamp": step.timestamp,
                "action": _step_action_label(step.action_taken),
                "motivation": motivation or "-",
                "action_taken": step.action_taken,
                "last_execution_result": step.last_execution_result,
            }
        )

    with open(trace_folder_path / "steps.json", "w", encoding="utf-8", errors="ignore") as f:
        f.write(json.dumps(entries, default=str))

    logger.info(f"Compiled {len(entries)} steps from the session database")
    return len(entries)


def remove_steps_json_from_trace_folder(trace_folder_path: Path):
    """Remove only legacy timestamp-named step files, preserving manifests."""
    for file in trace_folder_path.iterdir():
        if file.suffix == ".json" and file.stem.isdigit():
            file.unlink()
