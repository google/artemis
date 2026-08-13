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

"""Unit tests for the official Python SDK (ArtemisClient, Task, TaskResult)."""

import pytest
from artemis import ArtemisClient, Task, TaskResult


@pytest.mark.asyncio
async def test_artemis_client_run_mock():
    """Verify ArtemisClient runs a task through the execution pipeline."""
    client = ArtemisClient(device_id="mock-test-dev", default_profile="pro")
    assert client.device_id == "mock-test-dev"
    assert client.default_profile == "pro"

    result = await client.run(goal="Open Settings and enable Dark Theme")
    assert isinstance(result, TaskResult)
    assert result.status in ("completed", "running", "success")
    assert result.trace_id.startswith("trace_") or len(result.trace_id) > 0


def test_task_model_validation():
    """Verify Task validation and default arguments."""
    task = Task(goal="Search for flight prices", profile="flash", device_id="emulator-5554")
    assert task.goal == "Search for flight prices"
    assert task.profile == "flash"
    assert task.device_id == "emulator-5554"
    assert task.max_turns == 30
    assert task.locked_package is None


@pytest.mark.asyncio
async def test_artemis_client_stream_run():
    """Verify ArtemisClient.stream_run yields real-time StreamEvents."""
    from artemis import StreamEvent, StreamEventType

    client = ArtemisClient(device_id="mock-stream-dev", default_profile="flash")
    collected_events: list[StreamEvent] = []

    def on_event(ev: StreamEvent):
        collected_events.append(ev)

    events: list[StreamEvent] = []
    async for event in client.stream_run(
        goal="Test streaming UI action flow",
        profile="flash",
        callbacks=[on_event],
    ):
        events.append(event)

    assert len(events) >= 2
    assert events[0].event_type == StreamEventType.STATUS
    assert events[0].payload.get("status") == "starting"
    assert len(collected_events) == len(events)
