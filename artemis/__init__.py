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

"""☕ ARTEMIS: Autonomous Multimodal Android Agent & Testing Framework."""

__version__ = "0.2.0"

from artemis.interfaces.sdk.client import ArtemisClient, ConcurrencyMode
from artemis.interfaces.sdk.task import StreamEvent, StreamEventType, Task
from artemis_client import Capabilities, Device, TaskHandle, TaskResult
from artemis.sdk.agent import Agent
from artemis.sdk.builders import Builders
from artemis.drivers.base import BaseDeviceDriver
from artemis.tools.base import artemis_tool, ToolRegistry

__all__ = [
    "__version__",
    "ArtemisClient",
    "Capabilities",
    "ConcurrencyMode",
    "Device",
    "StreamEvent",
    "StreamEventType",
    "Agent",
    "Builders",
    "Task",
    "TaskHandle",
    "TaskResult",
    "BaseDeviceDriver",
    "artemis_tool",
    "ToolRegistry",
]
