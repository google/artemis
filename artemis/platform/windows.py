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

"""Windows Platform Implementation (KnownFolders/AppData, Win32 Process Tree & UTF-8 Injection)."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from artemis.platform.base import IPlatform, IPlatformPaths, IPlatformProcess, OSType


class WindowsPlatformPaths(IPlatformPaths):
    """Windows paths adhering to KnownFolders (%APPDATA% / %LOCALAPPDATA%)."""

    def __init__(self):
        self._home = Path.home()

    @property
    def config_dir(self) -> Path:
        appdata = os.getenv("APPDATA")
        if appdata:
            p = Path(appdata) / "Artemis"
        else:
            p = self._home / "AppData" / "Roaming" / "Artemis"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def data_dir(self) -> Path:
        env_dir = os.getenv("ARTEMIS_APP_DIR") or os.getenv("ANTIGRAVITY_APP_DIR")
        if env_dir:
            p = Path(env_dir)
            p.mkdir(parents=True, exist_ok=True)
            return p

        local_appdata = os.getenv("LOCALAPPDATA")
        if local_appdata:
            p = Path(local_appdata) / "Artemis"
            p.mkdir(parents=True, exist_ok=True)
            return p

        legacy_jetski = self._home / ".gemini" / "jetski"
        if legacy_jetski.exists():
            return legacy_jetski

        legacy_artemis = self._home / ".artemis"
        if legacy_artemis.exists():
            return legacy_artemis

        p = self._home / "AppData" / "Local" / "Artemis"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def cache_dir(self) -> Path:
        p = self.data_dir / "cache"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def logs_dir(self) -> Path:
        p = self.data_dir / "logs"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def temp_dir(self, subfolder: str | None = None) -> Path:
        base = Path(tempfile.gettempdir()) / "artemis"
        target = base / subfolder if subfolder else base
        target.mkdir(parents=True, exist_ok=True)
        return target

    def resolve_app_dir(self) -> Path:
        return self.data_dir


class WindowsPlatformProcess(IPlatformProcess):
    """Windows process management with tree termination and UTF-8 console streams."""

    @property
    def path_separator(self) -> str:
        return ";"

    def terminate_process_tree(self, pid: int, timeout_seconds: float = 3.0) -> bool:
        # 1. Try taskkill /F /T /PID on Windows (fastest and cleanest for process trees)
        try:
            res = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=timeout_seconds,
            )
            if res.returncode == 0:
                return True
        except Exception:
            pass

        # 2. Fallback to psutil
        try:
            import psutil

            parent = psutil.Process(pid)
            children = parent.children(recursive=True)
            for child in children:
                try:
                    child.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            parent.kill()
            return True
        except Exception:
            return False

    def setup_utf8_io(self) -> None:
        """Configures Windows stdout/stderr to UTF-8 mode to prevent encoding crashes."""
        if hasattr(sys.stdout, "reconfigure"):
            try:
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
        if hasattr(sys.stderr, "reconfigure"):
            try:
                sys.stderr.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
        os.environ["PYTHONUTF8"] = "1"


class WindowsPlatform(IPlatform):
    """Windows Platform Adapter."""

    def __init__(self):
        self._paths = WindowsPlatformPaths()
        self._process = WindowsPlatformProcess()

    @property
    def os_type(self) -> OSType:
        return OSType.WINDOWS

    @property
    def paths(self) -> IPlatformPaths:
        return self._paths

    @property
    def process(self) -> IPlatformProcess:
        return self._process

    def get_package_manager_name(self) -> str | None:
        for pm in ("winget", "choco", "scoop"):
            if shutil.which(pm):
                return pm
        return None

    def get_install_command(self, tool_name: str) -> str:
        pm = self.get_package_manager_name() or "winget"
        pkg_map = {
            "adb": {
                "winget": "winget install Google.PlatformTools",
                "choco": "choco install adb",
                "scoop": "scoop install adb",
            },
            "ffmpeg": {
                "winget": "winget install Gyan.FFmpeg",
                "choco": "choco install ffmpeg",
                "scoop": "scoop install ffmpeg",
            },
            "scrcpy": {
                "winget": "winget install Genymobile.scrcpy",
                "choco": "choco install scrcpy",
                "scoop": "scoop install scrcpy",
            },
        }
        if tool_name in pkg_map and pm in pkg_map[tool_name]:
            return pkg_map[tool_name][pm]
        return f"winget install {tool_name} (or download official installer)"
