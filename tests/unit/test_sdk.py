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


@pytest.mark.asyncio
async def test_client_device_specification():
    """Verify target device can be set via init, setters, or per-task run parameters."""
    client = ArtemisClient(device_serial="emulator-5554", default_profile="flash")
    assert client.device_id == "emulator-5554"
    assert client.device_serial == "emulator-5554"

    # Mutate via property setter
    client.device_serial = "pixel-9-pro"
    assert client.device_id == "pixel-9-pro"
    assert client.device_serial == "pixel-9-pro"

    # Mutate via chaining method
    client.set_device("pixel-8")
    assert client.device_id == "pixel-8"

    # Override device per run call using device_serial
    res1 = await client.run("Task on dev 1", device_serial="target-device-alpha")
    assert res1.device_id == "target-device-alpha"

    # Override device per run call using device_id
    res2 = await client.run("Task on dev 2", device_id="target-device-beta")
    assert res2.device_id == "target-device-beta"


@pytest.mark.asyncio
async def test_client_stream_run_device_specification():
    """Verify stream_run propagates custom target device."""
    client = ArtemisClient(device_id="default-stream-dev", default_profile="flash")
    events = []
    async for ev in client.stream_run("Test goal", device_serial="explicit-stream-dev"):
        events.append(ev)

    assert len(events) >= 1
    assert events[0].payload.get("device_id") == "explicit-stream-dev"


@pytest.mark.asyncio
async def test_client_concurrency_mode_configuration():
    """Verify concurrency_mode can be configured as 'global' or 'per_device'."""
    from artemis import ConcurrencyMode

    client = ArtemisClient(concurrency_mode=ConcurrencyMode.GLOBAL)
    assert client.concurrency_mode == "global"

    client.set_concurrency_mode(ConcurrencyMode.PER_DEVICE)
    assert client.concurrency_mode == "per_device"

    client.concurrency_mode = "global"
    assert client.concurrency_mode == "global"


@pytest.mark.asyncio
async def test_sdk_multi_device_parallel_execution():
    """Verify per_device concurrency allows tasks on different devices to run concurrently."""
    import asyncio

    client_a = ArtemisClient(device_id="mock-dev-parallel-1", concurrency_mode="per_device")
    client_b = ArtemisClient(device_id="mock-dev-parallel-2", concurrency_mode="per_device")

    # Run tasks on two different devices concurrently
    task_a = asyncio.create_task(client_a.run("Task on device 1"))
    task_b = asyncio.create_task(client_b.run("Task on device 2"))

    res_a, res_b = await asyncio.gather(task_a, task_b)
    assert res_a.status in ("completed", "running", "success")
    assert res_b.status in ("completed", "running", "success")
    assert res_a.device_id == "mock-dev-parallel-1"
    assert res_b.device_id == "mock-dev-parallel-2"


@pytest.mark.asyncio
async def test_sdk_global_concurrency_serializes_across_devices():
    """Verify global concurrency restricts execution to 1 task across all devices."""
    import asyncio
    from artemis.runtime import DeviceBusyError, DeviceExecutionLock

    # Pre-acquire a lock on device A with global concurrency mode
    lock_a = DeviceExecutionLock("mock-global-dev-1", "active global task", concurrency_mode="global")
    lock_a.acquire()

    try:
        client_b = ArtemisClient(device_id="mock-global-dev-2", concurrency_mode="global")
        # Attempting non-blocking execution on device B should fail because global concurrency limit (1) is held by device A
        with pytest.raises(DeviceBusyError, match="Global task concurrency limit"):
            await client_b.run("Task on device 2", blocking=False)
    finally:
        lock_a.release()


@pytest.mark.asyncio
async def test_sdk_run_task_with_device_and_concurrency():
    """Verify Task object with device_serial and concurrency_mode executes via run_task."""
    client = ArtemisClient()
    task = Task(
        goal="Verify search functionality",
        profile="flash",
        device_serial="device-from-task-obj",
        concurrency_mode="per_device",
    )
    assert task.device_id == "device-from-task-obj"
    assert task.concurrency_mode == "per_device"

    res = await client.run_task(task)
    assert res.device_id == "device-from-task-obj"
    assert res.status in ("completed", "running", "success")


def test_builder_and_agent_concurrency_and_device_targeting():
    """Verify AgentConfigBuilder and Agent support device targeting and concurrency configuration."""
    from artemis import Agent, Builders

    # 1. Builder supports string for_device directly
    cfg1 = Builders.AgentConfig.for_device("emulator-9999").with_concurrency_mode("global").build()
    assert cfg1.device_id == "emulator-9999"
    assert cfg1.concurrency_mode == "global"

    # 2. Builder supports for_device_serial
    cfg2 = Builders.AgentConfig.for_device_serial("pixel-test-serial").with_concurrency_mode("per_device").build()
    assert cfg2.device_id == "pixel-test-serial"
    assert cfg2.concurrency_mode == "per_device"

    # 3. Agent supports device_serial and concurrency_mode directly in constructor
    agent = Agent(device_serial="pixel-constructor-dev", concurrency_mode="global")
    assert agent._config.device_id == "pixel-constructor-dev"
    assert agent._config.concurrency_mode == "global"
