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

"""Toolchain descriptors and metadata for cross-platform binary discovery."""

from collections.abc import Callable
from dataclasses import dataclass


def _get_embedded_adb() -> str | None:
    """Fallback to adb binary bundled with adbutils if available."""
    try:
        import adbutils

        if hasattr(adbutils, "adb_path"):
            return adbutils.adb_path()
    except Exception:
        # Optional dependency probe: not installed or no bundled binary.
        pass
    return None


def _get_embedded_ffmpeg() -> str | None:
    """Fallback to ffmpeg binary bundled with imageio_ffmpeg if available."""
    try:
        import imageio_ffmpeg

        if hasattr(imageio_ffmpeg, "get_ffmpeg_exe"):
            return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        # Optional dependency probe: not installed, or imageio_ffmpeg raises
        # RuntimeError when it ships no binary for this platform.
        pass
    return None


@dataclass(frozen=True)
class ToolDescriptor:
    """Metadata and resolution strategies for an external binary/toolchain."""

    name: str
    binary_name: str
    win_binary_name: str
    sdk_relative_path: str | None = None
    embedded_fallback: Callable[[], str | None] | None = None
    description: str = ""


# Standard supported toolchains
TOOLS: dict[str, ToolDescriptor] = {
    "adb": ToolDescriptor(
        name="adb",
        binary_name="adb",
        win_binary_name="adb.exe",
        sdk_relative_path="platform-tools/adb",
        embedded_fallback=_get_embedded_adb,
        description="Android Debug Bridge client",
    ),
    "ffmpeg": ToolDescriptor(
        name="ffmpeg",
        binary_name="ffmpeg",
        win_binary_name="ffmpeg.exe",
        embedded_fallback=_get_embedded_ffmpeg,
        description="FFmpeg multimedia processing framework",
    ),
    "scrcpy": ToolDescriptor(
        name="scrcpy",
        binary_name="scrcpy",
        win_binary_name="scrcpy.exe",
        description="Screen Copy display and control tool",
    ),
    "uv": ToolDescriptor(
        name="uv",
        binary_name="uv",
        win_binary_name="uv.exe",
        description="Fast Python package manager",
    ),
}
