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

import time
from typing import Any, Literal
from pydantic import BaseModel, Field


class StreamEventType:
    THOUGHT = "thought"
    ACTION = "action"
    OBSERVATION = "observation"
    STEP_START = "step_start"
    STEP_END = "step_end"
    STATUS = "status"
    ERROR = "error"


class StreamEvent(BaseModel):
    """Real-time intermediate progress event emitted during task execution."""

    event_type: str = Field(
        ..., description="Type of event: thought, action, observation, status, etc."
    )
    step_number: int = Field(default=0, description="1-indexed execution step")
    payload: dict[str, Any] = Field(default_factory=dict, description="Event data dictionary")
    timestamp: float = Field(default_factory=time.time, description="Epoch timestamp")


class Task(BaseModel):
    """Configures a single autonomous mobile automation request."""

    goal: str = Field(..., description="High-level goal description")
    profile: Literal["flash", "pro"] = Field(default="pro", description="Execution profile")
    device_id: str = Field(default="default-device", description="Target device serial")
    locked_package: str | None = Field(default=None, description="Package lock restriction")
    max_turns: int = Field(default=30, description="Turn cutoff limit")


class TaskResult(BaseModel):
    """Final structured outcome of a task execution."""

    trace_id: str
    status: str
    turns: int
    output: Any = None
    error: str | None = None
