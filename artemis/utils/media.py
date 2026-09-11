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
#
# Portions of this file are derived from mobile-use (https://github.com/minitap-ai/mobile-use)
# Copyright 2025-2026 Minitap, Inc. Licensed under the Apache License 2.0.

"""Media synthesis and trace asset management for ARTEMIS.

Handles chronological trace GIF generation, frame quantization, and step snapshot
compilation while preserving active session recording manifests.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any

from PIL import Image

from artemis.utils.logger import get_logger

logger = get_logger(__name__)

_PREFER_FFMPEG = os.environ.get("USE_FFMPEG_GIF", "").strip().lower() in ("1", "true", "yes")


def quantize_and_save_gif_from_paths(
    image_paths: list[Path],
    output_path: Path,
    colors: int = 128,
    duration: int = 100,
) -> None:
    """Compile ordered image snapshots into a quantized animation asset."""
    if not image_paths:
        raise ValueError("Cannot assemble animation from an empty image sequence.")

    if _PREFER_FFMPEG and shutil.which("ffmpeg"):
        try:
            _render_gif_with_ffmpeg(image_paths, output_path, colors=colors, frame_delay_ms=duration)
            return
        except Exception as exc:
            logger.warning(f"FFmpeg GIF rendering encountered an error ({exc}); falling back to Pillow.")

    _render_gif_with_pillow(image_paths, output_path, colors=colors, frame_delay_ms=duration)


def _render_gif_with_pillow(
    paths: list[Path],
    output_path: Path,
    colors: int,
    frame_delay_ms: int,
) -> None:
    """Internal Pillow quantization loop."""
    frames: list[Image.Image] = []
    for p in paths:
        try:
            with Image.open(p) as img:
                rgb_img = img.convert("RGB") if img.mode != "RGB" else img.copy()
                frames.append(rgb_img.quantize(colors=colors, method=Image.Quantize.MEDIANCUT))
        except Exception as exc:
            logger.debug(f"Skipping damaged frame {p}: {exc}")

    if not frames:
        return

    first_frame = frames[0]
    first_frame.save(
        output_path,
        save_all=True,
        append_images=frames[1:],
        loop=0,
        optimize=True,
        duration=frame_delay_ms,
    )


def _render_gif_with_ffmpeg(
    paths: list[Path],
    output_path: Path,
    colors: int,
    frame_delay_ms: int,
) -> None:
    """Streamlined two-pass palette-generated GIF rendering via FFmpeg."""
    fps = max(1.0, 1000.0 / max(1, frame_delay_ms))
    with tempfile.TemporaryDirectory() as tmpdir:
        list_file = Path(tmpdir) / "frames.txt"
        palette = Path(tmpdir) / "palette.png"

        lines = [f"file '{p.resolve()}'\nduration {frame_delay_ms / 1000.0}" for p in paths]
        if paths:
            lines.append(f"file '{paths[-1].resolve()}'")
        list_file.write_text("\n".join(lines), encoding="utf-8")

        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_file),
                "-vf",
                f"palettegen=max_colors={min(colors, 256)}",
                str(palette),
            ],
            capture_output=True,
            check=True,
        )

        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_file),
                "-i",
                str(palette),
                "-lavfi",
                f"fps={fps},paletteuse",
                str(output_path),
            ],
            capture_output=True,
            check=True,
        )


def create_gif_from_trace_folder(trace_folder_path: Path) -> None:
    """Scan and synthesize sequential JPEG frames in a trace directory to trace.gif."""
    if not trace_folder_path.is_dir():
        return

    frames = sorted(
        [f for f in trace_folder_path.iterdir() if f.suffix.lower() in (".jpeg", ".jpg") and f.stem.isdigit()],
        key=lambda x: int(x.stem),
    )
    if not frames:
        return

    target_gif = trace_folder_path / "trace.gif"
    quantize_and_save_gif_from_paths(frames, target_gif)
    logger.info(f"Assembled trace animation: {target_gif}")


def remove_images_from_trace_folder(trace_folder_path: Path) -> None:
    """Purge temporary individual JPEG frame captures once compiled."""
    if not trace_folder_path.is_dir():
        return
    for item in trace_folder_path.iterdir():
        if item.suffix.lower() in (".jpeg", ".jpg") and item.stem.isdigit():
            try:
                item.unlink(missing_ok=True)
            except OSError:
                pass


def create_steps_json_from_trace_folder(trace_folder_path: Path) -> None:
    """Collate discrete numeric step payload dumps into steps.json preserving metadata manifests."""
    if not trace_folder_path.is_dir():
        return

    numeric_step_files = sorted(
        [f for f in trace_folder_path.iterdir() if f.suffix == ".json" and f.stem.isdigit()],
        key=lambda x: int(x.stem),
    )

    records: list[dict[str, Any]] = []
    for step_file in numeric_step_files:
        try:
            records.append({
                "timestamp": int(step_file.stem),
                "data": step_file.read_text(encoding="utf-8", errors="ignore"),
            })
        except Exception as exc:
            logger.debug(f"Unable to read step file {step_file}: {exc}")

    output_file = trace_folder_path / "steps.json"
    output_file.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")


def remove_steps_json_from_trace_folder(trace_folder_path: Path) -> None:
    """Clean up standalone numeric step files, leaving steps.json and recording manifests untouched."""
    if not trace_folder_path.is_dir():
        return
    for item in trace_folder_path.iterdir():
        if item.suffix == ".json" and item.stem.isdigit():
            try:
                item.unlink(missing_ok=True)
            except OSError:
                pass
