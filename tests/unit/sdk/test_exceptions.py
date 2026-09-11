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

"""Unit tests validating Artemis SDK exception hierarchy and diagnostics."""

from artemis.sdk.types.exceptions import (
    AgentError,
    AgentNotInitializedError,
    AgentProfileNotFoundError,
    AgentTaskRequestError,
    ArtemisError,
    DeviceError,
    DeviceNotFoundError,
    ExecutableNotFoundError,
    ServerError,
    ServerStartupError,
)


def test_exception_hierarchy():
    """Verify all custom exceptions inherit properly from ArtemisError."""
    assert issubclass(DeviceError, ArtemisError)
    assert issubclass(DeviceNotFoundError, DeviceError)
    assert issubclass(ServerError, ArtemisError)
    assert issubclass(ServerStartupError, ServerError)
    assert issubclass(AgentError, ArtemisError)
    assert issubclass(AgentNotInitializedError, AgentError)
    assert issubclass(AgentTaskRequestError, AgentError)
    assert issubclass(AgentProfileNotFoundError, AgentTaskRequestError)
    assert issubclass(ExecutableNotFoundError, ArtemisError)


def test_executable_not_found_error_adb_guidance():
    """Verify executable error formats download instructions for known tools."""
    err = ExecutableNotFoundError("adb")
    assert "adb" in str(err)
    assert "developer.android.com" in str(err)
    assert err.executable_name == "adb"


def test_server_startup_error_formatting():
    """Verify server startup error encapsulates service identifier."""
    err = ServerStartupError(server_name="mcp_daemon")
    assert "mcp_daemon" in str(err)
    assert err.server_name == "mcp_daemon"


def test_agent_profile_not_found_error():
    """Verify profile error retains profile name."""
    err = AgentProfileNotFoundError("nonexistent_profile")
    assert "nonexistent_profile" in str(err)
    assert err.profile_name == "nonexistent_profile"
