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

"""macOS Platform Implementation (Apple Filesystem Hierarchy & Homebrew Integration)."""

import os
from pathlib import Path
import shutil
import signal
import tempfile
import time

from artemis.platform.base import IPlatform, IPlatformPaths, IPlatformProcess, OSType


class DarwinPlatformPaths(IPlatformPaths):
    """macOS paths adhering to Apple standard Library/Application Support structure."""

    def __init__(self):
        self._home = Path.home()

    @property
    def config_dir(self) -> Path:
        p = self._home / "Library" / "Application Support" / "artemis"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def data_dir(self) -> Path:
        env_dir = os.getenv("ARTEMIS_APP_DIR") or os.getenv("ANTIGRAVITY_APP_DIR")
        if env_dir:
            p = Path(env_dir)
            p.mkdir(parents=True, exist_ok=True)
            return p

        # Check legacy ~/.artemis or ~/.gemini/jetski directory if it exists
        legacy_artemis = self._home / ".artemis"
        if legacy_artemis.exists():
            return legacy_artemis

        legacy_jetski = self._home / ".gemini" / "jetski"
        if legacy_jetski.exists():
            return legacy_jetski

        p = self._home / "Library" / "Application Support" / "artemis"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def cache_dir(self) -> Path:
        p = self._home / "Library" / "Caches" / "artemis"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def logs_dir(self) -> Path:
        p = self._home / "Library" / "Logs" / "artemis"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def temp_dir(self, subfolder: str | None = None) -> Path:
        base = Path(tempfile.gettempdir()) / "artemis"
        target = base / subfolder if subfolder else base
        target.mkdir(parents=True, exist_ok=True)
        return target

    def resolve_app_dir(self) -> Path:
        return self.data_dir


class DarwinPlatformProcess(IPlatformProcess):
    """macOS process management."""

    @property
    def path_separator(self) -> str:
        return ":"

    def terminate_process_tree(self, pid: int, timeout_seconds: float = 3.0) -> bool:
        try:
            import psutil

            parent = psutil.Process(pid)
            children = parent.children(recursive=True)
            for child in children:
                try:
                    child.send_signal(signal.SIGTERM)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

            parent.send_signal(signal.SIGTERM)

            # Avoid calling wait_procs on direct children of the current process.
            # In POSIX, psutil.Process.wait() on a child of os.getpid() invokes
            # os.waitpid(), which reaps the zombie and prevents asyncio / uvloop
            # event loops from detecting that the child exited, causing proc.wait()
            # to hang indefinitely.
            current_pid = os.getpid()
            external_procs = [
                p
                for p in children + [parent]
                if p.pid != current_pid and getattr(p, "ppid", lambda: None)() != current_pid
            ]
            direct_children = [
                p
                for p in children + [parent]
                if getattr(p, "ppid", lambda: None)() == current_pid
            ]

            if external_procs:
                gone, alive = psutil.wait_procs(external_procs, timeout=timeout_seconds)
                for p in alive:
                    try:
                        p.kill()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass

            deadline = time.time() + timeout_seconds
            for p in direct_children:
                while time.time() < deadline:
                    try:
                        if not p.is_running() or p.status() == psutil.STATUS_ZOMBIE:
                            break
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        break
                    time.sleep(0.05)
                else:
                    try:
                        p.kill()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass

            return True
        except Exception:
            try:
                os.kill(pid, signal.SIGKILL)
                return True
            except OSError:
                return False

    def setup_utf8_io(self) -> None:
        pass


class DarwinPlatform(IPlatform):
    """macOS Platform Adapter."""

    def __init__(self):
        self._paths = DarwinPlatformPaths()
        self._process = DarwinPlatformProcess()

    @property
    def os_type(self) -> OSType:
        return OSType.MACOS

    @property
    def paths(self) -> IPlatformPaths:
        return self._paths

    @property
    def process(self) -> IPlatformProcess:
        return self._process

    def get_package_manager_name(self) -> str | None:
        if shutil.which("brew"):
            return "brew"
        return None

    def get_install_command(self, tool_name: str) -> str:
        pkg_map = {
            "adb": "android-platform-tools",
            "ffmpeg": "ffmpeg",
            "scrcpy": "scrcpy",
        }
        pkg = pkg_map.get(tool_name, tool_name)
        return f"brew install {pkg}"
