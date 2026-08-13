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
from typing import Any
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field


class SessionMetadata(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    session_id: UUID | str
    initial_goal: str
    start_time: float = Field(default_factory=time.time)
    end_time: float | None = None
    status: str = "running"  # running, success, failed
    device_info: dict[str, Any] = Field(default_factory=dict)
    pid: int | None = None
    video_filepath: str | None = None


class ImageRecord(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    image_name: str  # SHA-256 hash
    timestamp: float = Field(default_factory=time.time)
    ocr_result: Any | None = None
    ui_tree: Any | None = None
    extra_metadata: dict[str, Any] = Field(default_factory=dict)


class StepRecord(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    step_id: UUID | str
    session_id: UUID | str
    step_number: int
    timestamp: float = Field(default_factory=time.time)
    pre_image_name: str | None = None
    post_image_name: str | None = None
    summary: str | None = None
    action_taken: Any | None = None
    operator_raw_thinking: str | None = None
    operator_native_thinking: str | None = None
    last_execution_result: Any | None = None
    extra_metadata: dict[str, Any] = Field(default_factory=dict)


class TraceRecord(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    trace_id: UUID | str
    session_id: UUID | str
    step_id: UUID | str | None = None
    parent_trace_id: UUID | str | None = None
    type: str  # "agent", "tool", "log"
    name: str
    timestamp: float = Field(default_factory=time.time)
    duration: float | None = None
    status: str = "success"  # success, failed, running
    payload: dict[str, Any] = Field(default_factory=dict)


class FailedOutputRecord(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    session_id: UUID | str
    trace_id: UUID | str
    model_name: str
    prompt: str
    raw_output: str
    error_message: str
    timestamp: float = Field(default_factory=time.time)


class BackgroundTaskRecord(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    task_id: str
    session_id: UUID
    summary: str
    status: str = "running"  # running, completed, failed
    start_time: float = Field(default_factory=time.time)
    end_time: float | None = None
    trace_id: str | None = None
    logs: str | None = None


class VideoRecordingRecord(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    video_id: UUID
    session_id: UUID | None = None
    device_id: str
    start_time: float
    end_time: float | None = None
    local_video_path: str | None = None
