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

"""Platform Abstraction Layer (PAL) - Base Protocols and Types."""

from enum import Enum
from pathlib import Path
from typing import Protocol


class OSType(str, Enum):
    """Operating System Type."""

    LINUX = "linux"
    MACOS = "darwin"
    WINDOWS = "windows"
    UNKNOWN = "unknown"


class IPlatformPaths(Protocol):
    """Standardized directory and path resolver for the target operating system."""

    @property
    def config_dir(self) -> Path:
        """Directory for persistent user/app configuration files."""
        ...

    @property
    def data_dir(self) -> Path:
        """Directory for persistent data (traces, SQLite DBs, replays)."""
        ...

    @property
    def cache_dir(self) -> Path:
        """Directory for non-essential cached data."""
        ...

    @property
    def logs_dir(self) -> Path:
        """Directory for persistent runtime logs."""
        ...

    def temp_dir(self, subfolder: str | None = None) -> Path:
        """Unified temporary files directory for screenshots, recordings, and scratchpads."""
        ...

    def resolve_app_dir(self) -> Path:
        """Resolved central application root directory for backwards compatibility."""
        ...


class IPlatformProcess(Protocol):
    """Platform-specific process management, signals, and stream handling."""

    @property
    def path_separator(self) -> str:
        """Separator character used for PATH and PYTHONPATH (':' on POSIX, ';' on Windows)."""
        ...

    def terminate_process_tree(self, pid: int, timeout_seconds: float = 3.0) -> bool:
        """Safely and recursively terminate a process and all its child processes."""
        ...

    def setup_utf8_io(self) -> None:
        """Ensure standard I/O and process streams operate in UTF-8 mode."""
        ...


class IPlatform(Protocol):
    """Unified Platform Abstraction Layer interface."""

    @property
    def os_type(self) -> OSType:
        """Operating system type identifier."""
        ...

    @property
    def paths(self) -> IPlatformPaths:
        """Platform paths resolver."""
        ...

    @property
    def process(self) -> IPlatformProcess:
        """Platform process and execution manager."""
        ...

    def get_package_manager_name(self) -> str | None:
        """Name of the detected preferred package manager (e.g. apt, brew, winget)."""
        ...

    def get_install_command(self, tool_name: str) -> str:
        """Generate platform-specific installation command for a missing tool."""
        ...
