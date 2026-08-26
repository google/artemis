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

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from artemis.sdk.agent import Agent
from artemis.context import DeviceContext, DevicePlatform
from artemis.runtime.device_lock import DeviceBusyError
from artemis.sdk.types.exceptions import AgentError


@pytest.mark.asyncio
async def test_trace_finalization_failure_does_not_escape():
    agent = object.__new__(Agent)
    agent._finalize_tracing = AsyncMock(side_effect=ValueError("bad recording manifest"))
    task = MagicMock(status="completed")
    task.get_name.return_value = "successful-task"
    context = MagicMock()

    await agent._finalize_tracing_safely(task=task, context=context)

    agent._finalize_tracing.assert_awaited_once_with(task=task, context=context)


@pytest.mark.asyncio
async def test_agent_clean_disconnects_the_shared_ui_client():
    agent = object.__new__(Agent)
    agent._initialized = True
    agent._ui_adb_client = MagicMock()

    await agent.clean()

    agent._ui_adb_client.disconnect.assert_called_once_with()
    assert agent._initialized is False


@pytest.mark.asyncio
async def test_device_context_uses_adb_size_without_starting_ui_client():
    agent = object.__new__(Agent)
    agent._adb_client = MagicMock()
    agent._adb_client.device.return_value.window_size.return_value = (1080, 2424)
    agent._ui_adb_client = MagicMock()

    from artemis.context import DevicePlatform

    context = await agent._get_device_context("device-123", DevicePlatform.ANDROID)

    assert context.device_width == 1080
    assert context.device_height == 2424
    agent._ui_adb_client.get_screen_data.assert_not_called()


@pytest.mark.asyncio
async def test_task_that_never_acquires_queue_does_not_create_trace_session():
    agent = Agent()
    agent._initialized = True
    agent._device_context = DeviceContext(
        host_platform="WINDOWS",
        mobile_platform=DevicePlatform.ANDROID,
        device_id="device-123",
        device_width=1080,
        device_height=2424,
    )
    agent._adb_client = MagicMock()
    agent._ui_adb_client = MagicMock()
    agent._prepare_tracing = MagicMock()

    with patch(
        "artemis.sdk.agent.DeviceExecutionLock.acquire",
        side_effect=DeviceBusyError("queue cancelled"),
    ):
        with pytest.raises(DeviceBusyError, match="queue cancelled"):
            await agent.run_task(goal="queued task", profile="flash")

    agent._prepare_tracing.assert_not_called()


@pytest.mark.asyncio
async def test_secure_keyguard_is_rejected_without_guessing_credentials():
    agent = object.__new__(Agent)
    agent._device_context = MagicMock(device_id="device-123")
    agent._adb_client = MagicMock()
    agent._adb_client.device.return_value.shell.return_value = (
        'User "Owner" (id=0): deviceLocked=1\n'
        'User "Work" (id=10): deviceLocked=1\n'
    )

    with pytest.raises(AgentError, match="Unlock the device manually"):
        await agent._ensure_device_unlocked()


@pytest.mark.asyncio
async def test_locked_work_profile_does_not_block_unlocked_device_owner():
    agent = object.__new__(Agent)
    agent._device_context = MagicMock(device_id="device-123")
    agent._adb_client = MagicMock()
    agent._adb_client.device.return_value.shell.return_value = (
        'User "Owner" (id=0): deviceLocked=0\n'
        'User "Work" (id=10): deviceLocked=1\n'
    )

    await agent._ensure_device_unlocked()


@pytest.mark.asyncio
async def test_agent_inherits_session_id_from_env_and_propagates_to_tracing(monkeypatch):
    import uuid

    canonical_sid = "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d"
    monkeypatch.setenv("ARTEMIS_SESSION_ID", canonical_sid)

    agent = Agent()
    assert agent._session_id == canonical_sid

    task = MagicMock()
    task.id = "fallback-id"
    task.get_name.return_value = "task-name"
    task.request.goal = "test goal"
    task.request.profile = "flash"
    task.request.task_name = "task-name"
    task.request.trace_path = "traces"

    context = MagicMock()
    context.device = None
    with patch("artemis.sdk.agent.DataEngine") as mock_data_engine_cls:
        mock_data_engine = MagicMock()
        mock_data_engine_cls.return_value = mock_data_engine

        agent._prepare_tracing(task=task, context=context)

        mock_data_engine.start_session.assert_called_once()
        call_kwargs = mock_data_engine.start_session.call_args.kwargs
        assert call_kwargs["session_id"] == uuid.UUID(canonical_sid)
