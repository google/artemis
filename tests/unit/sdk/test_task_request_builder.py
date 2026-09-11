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

"""Unit tests for Artemis SDK TaskRequestBuilder."""

from pathlib import Path
from pydantic import BaseModel
import pytest

from artemis.sdk.builders.task_request_builder import TaskRequestBuilder, TaskRequestCommonBuilder
from artemis.sdk.types.task import TaskRequest


class DummyOutputSchema(BaseModel):
    summary: str
    item_count: int


def test_task_request_builder_fluent_chaining():
    """Verify basic fluent configuration and build."""
    req = (
        TaskRequestBuilder(goal="Open Settings and verify battery level")
        .with_max_steps(25)
        .with_trace_recording(True, path="custom_traces")
        .with_locked_app_package("com.android.settings")
        .with_app_path("build/app.apk")
        .using_profile("flash")
        .with_name("Battery Check")
        .build()
    )

    assert isinstance(req, TaskRequest)
    assert req.goal == "Open Settings and verify battery level"
    assert req.max_steps == 25
    assert req.record_trace is True
    assert req.trace_path == Path("custom_traces")
    assert req.locked_app_package == "com.android.settings"
    assert req.app_path == Path("build/app.apk")
    assert req.profile == "flash"
    assert req.task_name == "Battery Check"


def test_task_request_builder_with_output_format():
    """Verify builder binds structured schema properly."""
    req = (
        TaskRequestBuilder(goal="Extract items")
        .with_output_format(DummyOutputSchema)
        .build()
    )

    assert req.output_format is DummyOutputSchema
    assert req.output_description is None


def test_task_request_builder_with_output_description():
    """Verify builder accepts unformatted string deliverables."""
    req = (
        TaskRequestBuilder(goal="Extract items")
        .with_output_description("Return a numbered list")
        .build()
    )

    assert req.output_description == "Return a numbered list"
    assert req.output_format is None


def test_task_request_builder_conflicting_outputs_raises():
    """Verify that specifying both output_format and output_description raises ValueError."""
    builder = (
        TaskRequestBuilder(goal="Extract items")
        .with_output_format(DummyOutputSchema)
        .with_output_description("Description text")
    )
    with pytest.raises(ValueError, match="Cannot specify both output_format and output_description"):
        builder.build()


def test_task_request_builder_empty_goal_raises():
    """Verify empty or whitespace-only goal raises ValueError."""
    with pytest.raises(ValueError, match="Task goal must be a non-empty"):
        TaskRequestBuilder(goal="   ").build()


def test_task_request_builder_from_common():
    """Verify task request initialization derived from common base settings."""
    common_settings = (
        TaskRequestCommonBuilder()
        .with_max_steps(42)
        .with_trace_recording(False)
        .with_locked_app_package("org.artemis.target")
        .with_app_path(Path("/tmp/target.apk"))
        .build()
    )

    derived = TaskRequestBuilder.from_common("Derived goal", common_settings).build()
    assert derived.goal == "Derived goal"
    assert derived.max_steps == 42
    assert derived.record_trace is False
    assert derived.locked_app_package == "org.artemis.target"
    assert derived.app_path == Path("/tmp/target.apk")
