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

"""Fluent specification builders for ARTEMIS automation task requests.

Provides type-safe configuration chaining for task objectives, execution bounds,
device sandboxing constraints, and structured schema validations.
"""

from __future__ import annotations

from pathlib import Path
from typing import Generic, Self, TypeVar, cast

from pydantic import BaseModel

from artemis.constants import RECURSION_LIMIT
from artemis.sdk.types.agent import AgentProfile
from artemis.sdk.types.task import TaskRequest, TaskRequestCommon

TIn = TypeVar("TIn", bound=BaseModel | None)
TOut = TypeVar("TOut", bound=BaseModel)


class TaskRequestCommonBuilder:
    """Configurator for common execution bounds and sandbox constraints."""

    def __init__(self) -> None:
        self._max_steps: int = RECURSION_LIMIT
        self._record_trace: bool = True
        self._trace_path: Path = Path("traces")
        self._llm_output_path: Path | None = None
        self._locked_app_package: str | None = None
        self._app_path: Path | None = None

    def with_max_steps(self, max_steps: int) -> Self:
        """Constrain the maximum interaction turns allowed."""
        self._max_steps = max_steps
        return self

    def with_trace_recording(self, enabled: bool = True, path: str | Path | None = None) -> Self:
        """Toggle telemetry snapshot capture and set output storage directory."""
        self._record_trace = enabled
        if enabled and path:
            self._trace_path = Path(path)
        return self

    def with_llm_output_saving(self, path: str | Path) -> Self:
        """Designate destination path for persisting raw reasoning payloads."""
        self._llm_output_path = Path(path)
        return self

    def with_locked_app_package(self, package_name: str) -> Self:
        """Bind test execution strictly to the target Android package namespace."""
        self._locked_app_package = package_name.strip()
        return self

    def with_app_path(self, app_path: str | Path) -> Self:
        """Provide path to local APK for automatic installation prior to run."""
        self._app_path = Path(app_path)
        return self

    def build(self) -> TaskRequestCommon:
        """Materialize configured constraints into a TaskRequestCommon contract."""
        return TaskRequestCommon(
            max_steps=self._max_steps,
            record_trace=self._record_trace,
            trace_path=self._trace_path,
            llm_output_path=self._llm_output_path,
            locked_app_package=self._locked_app_package,
            app_path=self._app_path,
        )


class TaskRequestBuilder(TaskRequestCommonBuilder, Generic[TIn]):
    """Fluent configurator constructing immutable TaskRequest specifications."""

    def __init__(self, goal: str) -> None:
        super().__init__()
        self._goal: str = goal
        self._profile: str | AgentProfile | None = None
        self._name: str | None = None
        self._output_description: str | None = None
        self._output_format: type[TIn] | None = None

    @classmethod
    def from_common(cls, goal: str, common: TaskRequestCommon) -> TaskRequestBuilder[None]:
        """Derive a new task builder initialized with existing environment constraints."""
        builder = TaskRequestBuilder[None](goal=goal)
        builder._max_steps = common.max_steps
        builder._record_trace = common.record_trace
        builder._trace_path = common.trace_path
        builder._llm_output_path = common.llm_output_path
        builder._locked_app_package = common.locked_app_package
        builder._app_path = common.app_path
        return builder

    def using_profile(self, profile: str | AgentProfile) -> Self:
        """Select execution profile (e.g., 'flash', 'pro', or custom AgentProfile)."""
        self._profile = profile
        return self

    def with_name(self, name: str) -> Self:
        """Assign human-readable identifier to this test task."""
        self._name = name.strip()
        return self

    def without_llm_output_saving(self) -> Self:
        """Omit persisting standalone model reasoning outputs."""
        self._llm_output_path = None
        return self

    def with_output_description(self, description: str) -> Self:
        """Supply natural language extraction instructions for unformatted deliverables."""
        self._output_description = description
        return self

    def with_output_format(self, output_format: type[TOut]) -> TaskRequestBuilder[TOut]:
        """Bind output schema to a structured Pydantic model class."""
        self._output_format = cast(type[TIn], output_format)
        return cast(TaskRequestBuilder[TOut], self)

    def build(self) -> TaskRequest[TIn]:
        """Validate criteria and construct final TaskRequest instance."""
        if not self._goal or not self._goal.strip():
            raise ValueError("Task goal must be a non-empty natural language objective.")

        if self._output_format is not None and self._output_description is not None:
            raise ValueError("Cannot specify both output_format and output_description simultaneously.")

        profile_name = self._profile.name if isinstance(self._profile, AgentProfile) else self._profile

        return TaskRequest(
            goal=self._goal.strip(),
            profile=profile_name,
            task_name=self._name,
            output_description=self._output_description,
            output_format=self._output_format,
            max_steps=self._max_steps,
            record_trace=self._record_trace,
            trace_path=self._trace_path,
            llm_output_path=self._llm_output_path,
            locked_app_package=self._locked_app_package,
            app_path=self._app_path,
        )
