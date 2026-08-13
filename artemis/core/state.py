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

"""Core Execution State and Lifecycle Data Structures."""

from enum import Enum
import time
from typing import Any
from pydantic import BaseModel, Field


class ExecutionStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    PAUSED = "paused"
    INTERRUPTED = "interrupted"


class StepRecord(BaseModel):
    """Encapsulates a single step's observation, thought, and action."""

    step_number: int = Field(..., description="1-indexed step turn counter")
    thought: str | None = Field(default=None, description="Reasoning text")
    action_name: str | None = Field(default=None, description="Triggered action name")
    action_params: dict[str, Any] = Field(default_factory=dict, description="Action arguments")
    result: str | None = Field(default=None, description="Action execution result output")
    screenshot_path: str | None = Field(default=None, description="Saved step screenshot file URI")
    timestamp: float = Field(default_factory=time.time, description="Timestamp of step start")
    duration_seconds: float = Field(default=0.0, description="Step processing duration in seconds")


class ExecutionContextState(BaseModel):
    """Full execution state passed across agents and runners."""

    task_goal: str = Field(..., description="Initial user task objective")
    trace_id: str = Field(..., description="Unique task trace identifier")
    status: ExecutionStatus = Field(
        default=ExecutionStatus.RUNNING, description="Current execution lifecycle status"
    )
    current_turn: int = Field(default=0, description="Current step turn count")
    max_turns: int = Field(default=30, description="Maximum allowed step turns before timeout")
    steps: list[StepRecord] = Field(
        default_factory=list, description="Chronological execution step history"
    )
    active_plan: list[str] = Field(
        default_factory=list, description="Decomposed milestone plan items"
    )
    current_step_index: int = Field(default=0, description="Current index within active_plan")
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Custom metadata and state parameters"
    )
    error_message: str | None = Field(
        default=None, description="Failure reason if status is FAILED"
    )
    final_output: Any | None = Field(
        default=None, description="Final synthesized structured output from Outputter or runner"
    )
