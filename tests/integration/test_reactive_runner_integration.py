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

"""Integration tests for ReactiveRunner and Pipeline."""

import pytest
from artemis.core.context import ExecutionContext
from artemis.core.state import ExecutionStatus
from artemis.drivers.mock.mock_driver import MockDeviceDriver
from artemis.engine.pipeline import Pipeline
from artemis.engine.reactive_runner import ReactiveRunner


@pytest.mark.asyncio
async def test_reactive_runner_execution():
    """Verify ReactiveRunner executes workflow cycles on mock driver."""
    ctx = ExecutionContext(task_goal="Turn on Bluetooth in Settings", device_id="test-emulator")
    driver = MockDeviceDriver(device_id="test-emulator")

    runner = ReactiveRunner(ctx=ctx, driver=driver)
    state = await runner.run(max_turns=5)

    assert state.status == ExecutionStatus.SUCCESS
    assert state.current_turn >= 1
    assert len(state.steps) >= 1
    assert state.steps[0].action_name == "wait_for_delay"


@pytest.mark.asyncio
async def test_pipeline_dispatch():
    """Verify Pipeline executes with default mock driver."""
    state = await Pipeline.execute(goal="Open YouTube and play video", profile="flash")
    assert state.status == ExecutionStatus.SUCCESS
    assert state.task_goal == "Open YouTube and play video"
