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

"""Multi-tier cross-platform binary resolution and discovery engine."""

import logging
import os
from pathlib import Path
import shutil

from artemis.platform import OSType, platform
from artemis.toolchain.descriptors import TOOLS, ToolDescriptor

logger = logging.getLogger(__name__)


class ToolchainResolver:
    """Discovers, validates, and resolves platform-specific binaries with graceful fallbacks."""

    def __init__(self):
        self._cache: dict[str, str | None] = {}

    def clear_cache(self, tool_name: str | None = None) -> None:
        """Invalidate cached tool paths to force fresh discovery."""
        if tool_name:
            self._cache.pop(tool_name.lower(), None)
        else:
            self._cache.clear()

    def resolve(self, tool_name: str, force_refresh: bool = False) -> str | None:
        """Resolve absolute path to a tool across all discovery tiers."""
        key = tool_name.lower()
        if not force_refresh and key in self._cache:
            cached_val = self._cache[key]
            if cached_val is not None and Path(cached_val).exists():
                return cached_val
            # Cache miss or binary removed: proceed to re-discover

        desc = TOOLS.get(key)
        if desc is None:
            # Generic binary lookup
            res = shutil.which(tool_name)
            if res and Path(res).exists():
                self._cache[key] = str(Path(res).resolve())
                return self._cache[key]
            self._cache.pop(key, None)
            return None

        binary_name = (
            desc.win_binary_name if platform.os_type == OSType.WINDOWS else desc.binary_name
        )

        # Tier 1: Explicit environment variable override (e.g. ARTEMIS_ADB_PATH)
        env_var = f"ARTEMIS_{desc.name.upper()}_PATH"
        env_override = os.getenv(env_var)
        if env_override and Path(env_override).exists():
            resolved = str(Path(env_override).resolve())
            self._cache[key] = resolved
            return resolved

        # Tier 2: SDK Environment Variables (for Android tools)
        if desc.sdk_relative_path:
            for sdk_env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
                sdk_dir = os.getenv(sdk_env)
                if sdk_dir:
                    candidate = Path(sdk_dir) / desc.sdk_relative_path
                    if platform.os_type == OSType.WINDOWS:
                        candidate = candidate.with_suffix(".exe")
                    if candidate.exists():
                        resolved = str(candidate.resolve())
                        self._cache[key] = resolved
                        return resolved

        # Tier 3: Common OS default installation paths
        common_candidates = self._get_os_common_paths(desc)
        for cand in common_candidates:
            if cand.exists():
                resolved = str(cand.resolve())
                self._cache[key] = resolved
                return resolved

        # Tier 4: System PATH
        which_path = shutil.which(binary_name) or shutil.which(desc.name)
        if which_path:
            resolved = str(Path(which_path).resolve())
            self._cache[key] = resolved
            return resolved

        # Tier 5: Python library embedded binary fallback
        if desc.embedded_fallback:
            try:
                embedded = desc.embedded_fallback()
                if embedded and Path(embedded).exists():
                    resolved = str(Path(embedded).resolve())
                    self._cache[key] = resolved
                    return resolved
            except Exception as exc:  # pylint: disable=broad-exception-caught
                # Probing an optional third-party embedded binary; any failure
                # simply means "tool not found" and the caller handles None.
                logger.debug(
                    "Embedded binary fallback for %s skipped: %s", desc.name, exc, exc_info=True
                )

        # Do not permanently cache negative lookups so dynamic installs are detected immediately
        self._cache.pop(key, None)
        return None

    def _get_os_common_paths(self, desc: ToolDescriptor) -> list[Path]:
        candidates: list[Path] = []
        home = Path.home()

        if platform.os_type == OSType.WINDOWS:
            local_appdata = os.getenv("LOCALAPPDATA", "")
            program_files = os.getenv("ProgramFiles", "C:\\Program Files")
            program_data = os.getenv("ProgramData", "C:\\ProgramData")

            if desc.name == "adb":
                if local_appdata:
                    candidates.append(
                        Path(local_appdata) / "Android" / "Sdk" / "platform-tools" / "adb.exe"
                    )
                candidates.append(Path(program_files) / "Android" / "platform-tools" / "adb.exe")
            elif desc.name == "scrcpy":
                candidates.append(Path(program_data) / "chocolatey" / "bin" / "scrcpy.exe")
                if local_appdata:
                    candidates.append(
                        Path(local_appdata) / "Microsoft" / "WinGet" / "Links" / "scrcpy.exe"
                    )
            elif desc.name == "ffmpeg":
                candidates.append(Path(program_data) / "chocolatey" / "bin" / "ffmpeg.exe")
                if local_appdata:
                    candidates.append(
                        Path(local_appdata) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe"
                    )

        elif platform.os_type == OSType.MACOS:
            if desc.name == "adb":
                candidates.append(home / "Library" / "Android" / "sdk" / "platform-tools" / "adb")
                candidates.append(Path("/opt/homebrew/bin/adb"))
                candidates.append(Path("/usr/local/bin/adb"))
            elif desc.name in ("ffmpeg", "scrcpy"):
                candidates.append(Path(f"/opt/homebrew/bin/{desc.name}"))
                candidates.append(Path(f"/usr/local/bin/{desc.name}"))

        elif platform.os_type == OSType.LINUX:
            if desc.name == "adb":
                candidates.append(home / "Android" / "Sdk" / "platform-tools" / "adb")
                candidates.append(Path("/usr/lib/android-sdk/platform-tools/adb"))
                candidates.append(Path("/usr/bin/adb"))
            elif desc.name in ("ffmpeg", "scrcpy"):
                candidates.append(Path(f"/usr/bin/{desc.name}"))
                candidates.append(Path(f"/usr/local/bin/{desc.name}"))

        return candidates

    def find_adb(self) -> str:
        """Find ADB executable or fallback to default name."""
        return self.resolve("adb") or "adb"

    def find_ffmpeg(self) -> str:
        """Find FFmpeg executable or fallback to default name."""
        return self.resolve("ffmpeg") or "ffmpeg"

    def find_scrcpy(self) -> str:
        """Find scrcpy executable or fallback to default name."""
        return self.resolve("scrcpy") or "scrcpy"

    def is_installed(self, tool_name: str) -> bool:
        """Check whether a tool is found on this system."""
        return self.resolve(tool_name) is not None


# Global toolchain resolver singleton
toolchain: ToolchainResolver = ToolchainResolver()
