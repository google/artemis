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
#
# Portions of this file are derived from mobile-use (https://github.com/minitap-ai/mobile-use)
# Copyright 2025-2026 Minitap, Inc. Licensed under the Apache License 2.0.

"""Task specifications, execution states, and outcome schemas for the ARTEMIS SDK.

Defines the declarative configuration contract for mobile automation goals,
agent operational profiles, telemetry preferences, and asynchronous result contracts.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from datetime import datetime
from pathlib import Path
from typing import Any, Generic, Literal, TypeVar, overload

from pydantic import BaseModel, ConfigDict, Field

from artemis.config import LLMConfig, get_default_llm_config
from artemis.constants import RECURSION_LIMIT
from artemis.context import DeviceContext
from artemis.sdk.utils import load_llm_config_override

TaskRunStatus = Literal["pending", "running", "completed", "failed", "cancelled"]


class AgentProfile(BaseModel):
    """Runtime configuration profile governing LLM model selection and reasoning policies."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str = Field(description="Identifier for referencing this operational agent profile.")
    llm_config: LLMConfig = Field(default_factory=get_default_llm_config)

    @overload
    def __init__(self, *, name: str, llm_config: LLMConfig): ...

    @overload
    def __init__(self, *, name: str, from_file: str | Path): ...

    def __init__(
        self,
        *,
        name: str,
        llm_config: LLMConfig | None = None,
        from_file: str | Path | None = None,
        **kwargs: Any,
    ):
        kwargs["name"] = name
        if from_file:
            kwargs["llm_config"] = load_llm_config_override(Path(from_file))
        elif llm_config:
            kwargs["llm_config"] = llm_config
        else:
            raise ValueError("Either llm_config or from_file must be specified.")
        super().__init__(**kwargs)

    def __str__(self) -> str:
        return f"AgentProfile({self.name}): {self.llm_config}"


T = TypeVar("T", bound=BaseModel)
TOutput = TypeVar("TOutput", bound=BaseModel | None)


class TaskRequestBase(BaseModel):
    """Baseline runtime limits and telemetry recording settings for automation runs."""

    model_config = ConfigDict(extra="ignore", arbitrary_types_allowed=True)

    max_steps: int = RECURSION_LIMIT
    record_trace: bool = True
    trace_path: Path = Field(default_factory=lambda: Path("traces"))
    llm_output_path: Path | None = None


class TaskRequestCommon(TaskRequestBase):
    """Common automation constraints including application isolation and pre-install packages."""

    locked_app_package: str | None = None
    app_path: Path | None = None


class TaskRequest(TaskRequestCommon, Generic[TOutput]):
    """Declarative request specifying the high-level mobile testing or automation objective."""

    goal: str = Field(description="Natural language instruction outlining target behavior or assertions.")
    profile: str | None = Field(default=None, description="Execution profile (e.g. 'flash' or 'pro').")
    task_name: str | None = Field(default=None, description="Optional label identifying this task execution.")
    output_description: str | None = Field(default=None, description="Formatting prompt for expected deliverables.")
    output_format: type[TOutput] | None = Field(default=None, description="Target Pydantic schema for structured output.")
    enable_remote_tracing: bool = False


class TaskResult(BaseModel):
    """Immutable outcome payload produced upon automation task completion."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    content: Any = None
    error: str | None = None
    execution_time_seconds: float = 0.0
    steps_taken: int = 0

    @property
    def succeeded(self) -> bool:
        """True if the task terminated without error."""
        return self.error is None

    def get_as_model(self, model_class: type[T]) -> T:
        """Parse structured content into the requested Pydantic model instance."""
        if self.content is None:
            raise ValueError("No content available to parse into the requested schema.")
        if isinstance(self.content, model_class):
            return self.content
        return model_class.model_validate(self.content)


class Task(BaseModel):
    """Stateful execution container tracking lifecycle progress, status transitions, and final results."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    device: DeviceContext
    status: TaskRunStatus = "pending"
    status_message: str | None = None
    on_status_changed: Callable[[TaskRunStatus, str | None, Any | None], Coroutine] | None = None
    request: TaskRequest[Any]
    created_at: datetime = Field(default_factory=datetime.now)
    ended_at: datetime | None = None
    result: TaskResult | None = None

    def get_name(self) -> str:
        return self.request.task_name or self.id

    async def set_status(
        self,
        status: TaskRunStatus,
        message: str | None = None,
        output: Any | None = None,
    ) -> None:
        self.status = status
        self.status_message = message
        if self.on_status_changed is not None:
            await self.on_status_changed(status, message, output)

    async def finalize(
        self,
        content: Any | None = None,
        state: dict[str, Any] | None = None,
        error: str | None = None,
        cancelled: bool = False,
    ) -> None:
        if cancelled:
            resolved_status: TaskRunStatus = "cancelled"
            default_msg = f"Task cancelled: {error}" if error else "Task cancelled."
        elif error:
            resolved_status = "failed"
            default_msg = f"Task failed: {error}"
        else:
            resolved_status = "completed"
            default_msg = "Task completed successfully."

        await self.set_status(status=resolved_status, message=default_msg, output=content or error)
        self.ended_at = datetime.now()

        elapsed = (self.ended_at - self.created_at).total_seconds()
        steps = -1
        if state is not None:
            metadata = state.get("metadata")
            if isinstance(metadata, dict):
                steps = metadata.get("step_count", -1)

        self.result = TaskResult(
            content=content,
            error=error,
            execution_time_seconds=max(0.0, elapsed),
            steps_taken=steps,
        )
