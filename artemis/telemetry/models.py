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

"""Telemetry Data Models and Schema Definitions."""

from enum import Enum
import time
from typing import Any
from uuid import uuid4
from pydantic import BaseModel, Field


class SpanType(str, Enum):
    AGENT = "agent"
    LLM_CALL = "llm_call"
    TOOL_EXECUTION = "tool_execution"
    DEVICE_ACTION = "device_action"
    SCREEN_CAPTURE = "screen_capture"
    CUSTOM = "custom"


class TokenUsage(BaseModel):
    """LLM token consumption metrics."""

    prompt_tokens: int = Field(default=0, description="Input prompt tokens")
    completion_tokens: int = Field(default=0, description="Output generated tokens")
    total_tokens: int = Field(default=0, description="Total tokens consumed")


class TelemetrySpan(BaseModel):
    """Structured telemetry span representing a single unit of agentic execution."""

    span_id: str = Field(default_factory=lambda: str(uuid4()))
    trace_id: str = Field(..., description="Root trace/session identifier")
    parent_id: str | None = Field(default=None, description="Parent span identifier")
    name: str = Field(..., description="Human-readable span operation name")
    type: SpanType = Field(default=SpanType.CUSTOM, description="Span categorization")
    start_time: float = Field(default_factory=time.time, description="Start timestamp")
    end_time: float | None = Field(default=None, description="Completion timestamp")
    duration_ms: float | None = Field(default=None, description="Elapsed execution time in ms")
    status: str = Field(default="running", description="Status ('running', 'success', 'failed')")
    payload: dict[str, Any] = Field(
        default_factory=dict, description="Custom parameters and payloads"
    )
    error: str | None = Field(default=None, description="Exception message if failed")
    token_usage: TokenUsage | None = Field(default=None, description="Token consumption metrics")

    def finish(self, status: str = "success", error: str | None = None) -> None:
        """Closes the span and calculates duration."""
        self.end_time = time.time()
        self.duration_ms = (self.end_time - self.start_time) * 1000.0
        self.status = status
        self.error = error
