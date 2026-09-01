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

"""Clean Execution Context for ARTEMIS."""

from pathlib import Path
from typing import Any
from uuid import uuid4
from pydantic import BaseModel, Field


class DeviceInfoContext(BaseModel):
    """Device metadata and configuration."""

    device_id: str = Field(default="default-device", description="ADB serial or device identifier")
    platform: str = Field(
        default="android", description="Platform type ('android', 'cloud', 'mock')"
    )
    width: int = Field(default=1080, description="Screen width in pixels")
    height: int = Field(default=2400, description="Screen height in pixels")


class ExecutionContext:
    """Central context holding device driver, session identifiers, and telemetry tracer."""

    def __init__(
        self,
        task_goal: str,
        device_id: str = "default-device",
        trace_id: str | None = None,
        traces_path: Path | str | None = None,
        locked_package: str | None = None,
        platform: str = "android",
    ):
        self.task_goal = task_goal
        self.trace_id = trace_id or str(uuid4())
        self.traces_path = Path(traces_path) if traces_path else Path("traces") / self.trace_id
        self.locked_package = locked_package
        self.device = DeviceInfoContext(device_id=device_id, platform=platform)
        self.driver: Any | None = None
        self.telemetry: Any | None = None
        # Fully-wired ArtemisContext (data_engine, adb clients, execution setup).
        # Required by the live (non-mock) paths of GraphRunner/ReactiveRunner.
        self.artemis_context: Any | None = None
        self.custom_attributes: dict[str, Any] = {}

    def set_attribute(self, key: str, value: Any) -> None:
        self.custom_attributes[key] = value

    def get_attribute(self, key: str, default: Any = None) -> Any:
        return self.custom_attributes.get(key, default)
