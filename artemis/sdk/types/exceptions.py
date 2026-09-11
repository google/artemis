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

"""Hierarchical exception taxonomy for the ARTEMIS client and automation runtime.

Establishes categorized exception types covering host device discovery,
daemon lifecycle management, agent operational faults, and toolchain dependencies.
"""

from __future__ import annotations

from typing import Literal

_TOOLCHAIN_INSTALL_URLS: dict[str, str] = {
    "adb": "https://developer.android.com/tools/adb",
}


class ArtemisError(Exception):
    """Root ancestor of all exceptions originating from ARTEMIS operations."""

    def __init__(self, message: str = "An unexpected error occurred within the ARTEMIS runtime.") -> None:
        self.message = message
        super().__init__(self.message)


class DeviceError(ArtemisError):
    """Raised when hardware interaction or ADB communication fails."""

    def __init__(self, message: str = "A mobile device error occurred.") -> None:
        super().__init__(message)


class DeviceNotFoundError(DeviceError):
    """Raised when no authorized target Android device or emulator is accessible."""

    def __init__(self, message: str = "No connected and authorized Android device found.") -> None:
        super().__init__(message)


class ServerError(ArtemisError):
    """Raised when an internal ARTEMIS daemon or background service encounters an error."""

    def __init__(self, message: str = "An ARTEMIS daemon service failure occurred.") -> None:
        super().__init__(message)


class ServerStartupError(ServerError):
    """Raised when a background process (e.g. Scrcpy, MCP, or local server) fails to boot."""

    def __init__(self, server_name: str | None = None, message: str | None = None) -> None:
        resolved_msg = message or (f"Service '{server_name}' failed to start." if server_name else "Service startup failed.")
        super().__init__(resolved_msg)
        self.server_name = server_name


class AgentError(ArtemisError):
    """Raised when reasoning, task scheduling, or graph traversal fails."""

    def __init__(self, message: str = "An agent operational failure occurred.") -> None:
        super().__init__(message)


class AgentNotInitializedError(AgentError):
    """Raised when invoking automation actions before initialization."""

    def __init__(self, message: str = "Agent runtime has not been initialized.") -> None:
        super().__init__(message)


class AgentTaskRequestError(AgentError):
    """Raised when task parameters violate execution bounds or constraints."""

    def __init__(self, message: str = "Invalid task request configuration.") -> None:
        super().__init__(message)


class AgentProfileNotFoundError(AgentTaskRequestError):
    """Raised when requested execution profile is not registered in system catalog."""

    def __init__(self, profile_name: str) -> None:
        super().__init__(f"Agent profile '{profile_name}' is not registered.")
        self.profile_name = profile_name


class ExecutableNotFoundError(ArtemisError):
    """Raised when a mandatory host CLI toolchain executable is missing from PATH."""

    def __init__(self, executable_name: Literal["adb"] | str) -> None:
        guidance = _TOOLCHAIN_INSTALL_URLS.get(executable_name, "")
        msg = f"Required tool '{executable_name}' was not detected in system PATH."
        if guidance:
            msg += f" Install instructions: {guidance}"
        super().__init__(msg)
        self.executable_name = executable_name
