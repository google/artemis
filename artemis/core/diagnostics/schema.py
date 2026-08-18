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

"""Schema definitions for Artemis System Diagnostics & Readiness Engine."""

from enum import Enum
from typing import Any, Literal
from pydantic import BaseModel, Field


class ProbeStatus(str, Enum):
    """Execution status of a readiness probe."""

    PASS = "pass"  # 🟢 Ready for execution
    WARN = "warn"  # 🟡 Warning / Non-blocking prerequisite missing (e.g. no devices attached)
    FAIL = "fail"  # 🔴 Blocking prerequisite failure (e.g. ADB missing, no API key)
    SKIPPED = "skipped"  # ⚪ Skipped probe


class ProbeCategory(str, Enum):
    """Category of system readiness check."""

    DEVICE = "device"  # Hardware & Device Driver (ADB, Emulator, Cloud device)
    CREDENTIALS = "auth"  # LLM API Keys & OCR Credentials
    TOOLCHAIN = "toolchain"  # External tools (FFmpeg, scrcpy, etc.)
    RUNTIME = "runtime"  # Python runtime & Config health


class ProbeAction(BaseModel):
    """Actionable guidance payload for quick remediation or user guidance."""

    action_type: Literal["command", "hint", "link"] = Field(
        description="Type of remediation action ('command' to copy/run, 'hint' for guidance, 'link' for docs)"
    )
    label: str = Field(description="Display label for the action button or badge")
    payload: str = Field(description="Command string to execute/copy or explanation hint")


class DeviceInfo(BaseModel):
    """Structured Android device / emulator metadata."""

    serial: str = Field(description="Device serial number or emulator port ID (e.g. emulator-5554)")
    state: str = Field(
        default="device", description="Connection state ('device', 'unauthorized', 'offline')"
    )
    model: str | None = Field(
        default=None, description="Device model name (e.g. Pixel 8, sdk_gphone64_arm64)"
    )
    product: str | None = Field(default=None, description="Product code name")
    android_version: str | None = Field(
        default=None, description="Android OS release version (e.g. 14)"
    )
    screen_resolution: str | None = Field(
        default=None, description="Screen width x height (e.g. 1080x2400)"
    )
    is_emulator: bool = Field(default=False, description="Whether the device is an emulator")
    installed_packages: list[str] = Field(
        default_factory=list, description="List of recognized installed package names"
    )


class ProbeResult(BaseModel):
    """Outcome of a single readiness probe check."""

    id: str = Field(
        description="Unique identifier for the probe (e.g. 'android_adb', 'gemini_api_key')"
    )
    category: ProbeCategory = Field(description="Category of the probe")
    title: str = Field(description="Human-readable title (e.g. 'Device / Emulator Connected')")
    status: ProbeStatus = Field(description="Current status (pass, warn, fail)")
    is_blocker: bool = Field(
        default=True, description="Whether passing this probe is strictly required to run tasks"
    )
    summary: str = Field(
        description="Short badge summary text (e.g. 'Connected', 'No Device', 'Active & Ready')"
    )
    description: str = Field(description="Detailed explanation of the status")
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Arbitrary extra metadata (e.g. device list)"
    )
    actions: list[ProbeAction] = Field(
        default_factory=list, description="List of actionable remediation steps"
    )


class SystemReadinessReport(BaseModel):
    """Aggregated system readiness report across all probes."""

    overall_ready: bool = Field(description="True if all blocker probes are passing")
    blocker_count: int = Field(description="Total count of required blocker probes")
    passed_blocker_count: int = Field(
        description="Count of required blocker probes currently passing"
    )
    probes: list[ProbeResult] = Field(
        default_factory=list, description="Ordered list of probe results"
    )
    active_device: DeviceInfo | None = Field(
        default=None, description="Currently selected/active target device"
    )
    os_type: str = Field(
        default="linux", description="Operating system type ('linux', 'darwin', 'windows')"
    )
    timestamp: float = Field(description="Unix timestamp of when the report was generated")
