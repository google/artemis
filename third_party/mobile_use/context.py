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

"""Context variables for global state management.

Uses ContextVar to avoid prop drilling and maintain clean function signatures.
"""

from pathlib import Path
from typing import Any, Literal

try:
    from enum import StrEnum
except ImportError:
    from enum import Enum

    class StrEnum(str, Enum):
        pass


from pydantic import BaseModel, ConfigDict


class AppLaunchResult(BaseModel):
    """Result of initial app launch attempt."""

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    locked_app_package: str
    locked_app_initial_launch_success: bool | None
    locked_app_initial_launch_error: str | None


class DevicePlatform(StrEnum):
    """Mobile device platform enumeration."""

    ANDROID = "android"


class DeviceContext(BaseModel):
    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="allow",
    )

    host_platform: Literal["WINDOWS", "LINUX", "DARWIN", "MACOS"] | str = "DARWIN"
    mobile_platform: DevicePlatform = DevicePlatform.ANDROID
    device_id: str = "default-device"

    device_width: int = 1080
    device_height: int = 2400

    def to_str(self):
        return (
            f"Host platform: {self.host_platform}\n"
            f"Mobile platform: {self.mobile_platform.value}\n"
            f"Device ID: {self.device_id}\n"
            f"Device width: {self.device_width}\n"
            f"Device height: {self.device_height}\n"
        )


class ExecutionSetupBase(BaseModel):
    """Execution setup for a task."""

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="allow",
    )

    traces_path: Path | None = None
    trace_name: str | None = None
    enable_remote_tracing: bool = False
    app_lock_status: AppLaunchResult | None = None

    def get_locked_app_package(self) -> str | None:
        """Get the locked app package name if app locking is enabled.

        Returns:
            The locked app package name, or None if app locking is not enabled.
        """
        if self.app_lock_status:
            return self.app_lock_status.locked_app_package
        return None


class DeviceClientAccessors:
    """Checked accessors for the device clients held by a context model.

    The inheriting model declares the ``adb_client`` and ``ui_adb_client`` fields.
    """

    adb_client: Any
    ui_adb_client: Any

    def get_adb_client(self) -> Any:
        if self.adb_client is None:
            raise ValueError("No ADB client in context.")
        return self.adb_client

    def get_ui_adb_client(self) -> Any:
        if self.ui_adb_client is None:
            raise ValueError("No UIAutomator client in context.")
        return self.ui_adb_client
