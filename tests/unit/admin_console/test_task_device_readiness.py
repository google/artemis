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

from unittest.mock import AsyncMock

from fastapi import HTTPException
import pytest

from apps.admin_console.routers import tasks
from apps.admin_console.schemas.task_schema import RunRequest
from artemis.core.diagnostics.schema import (
    ProbeCategory,
    ProbeResult,
    ProbeStatus,
)


@pytest.mark.asyncio
async def test_run_task_rejects_locked_device(monkeypatch):
    locked_probe = ProbeResult(
        id="android_adb",
        category=ProbeCategory.DEVICE,
        title="Device / Emulator Connected",
        status=ProbeStatus.WARN,
        is_blocker=True,
        summary="Device Locked",
        description="Unlock the device.",
    )
    run_probe = AsyncMock(return_value=locked_probe)
    enqueue_tasks = AsyncMock()
    monkeypatch.setattr(
        tasks.readiness_engine, "run_device_submission_probe", run_probe
    )
    monkeypatch.setattr(tasks.task_queue_service, "enqueue_tasks", enqueue_tasks)

    with pytest.raises(HTTPException) as exc_info:
        await tasks.run_task(RunRequest(goal="Open Settings"))

    assert exc_info.value.status_code == 409
    assert "locked" in exc_info.value.detail.lower()
    enqueue_tasks.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_task_enqueues_when_device_is_unlocked(monkeypatch):
    unlocked_probe = ProbeResult(
        id="android_adb",
        category=ProbeCategory.DEVICE,
        title="Device / Emulator Connected",
        status=ProbeStatus.PASS,
        is_blocker=True,
        summary="Connected",
        description="Ready.",
    )
    run_probe = AsyncMock(return_value=unlocked_probe)
    enqueue_tasks = AsyncMock(return_value={"status": "started", "tasks": []})
    monkeypatch.setattr(
        tasks.readiness_engine, "run_device_submission_probe", run_probe
    )
    monkeypatch.setattr(tasks.task_queue_service, "enqueue_tasks", enqueue_tasks)

    result = await tasks.run_task(RunRequest(goal="Open Settings"))

    assert result["status"] == "started"
    enqueue_tasks.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_task_switches_to_unlocked_device_and_syncs_engine(monkeypatch):
    """When default device is locked, run_task should enqueue on verified fallback and update engine."""
    unlocked_probe = ProbeResult(
        id="android_adb",
        category=ProbeCategory.DEVICE,
        title="Device / Emulator Connected",
        status=ProbeStatus.PASS,
        is_blocker=True,
        summary="Connected",
        description="Ready.",
        metadata={
            "installed": True,
            "submission_probe": True,
            "active_device": {"serial": "emulator-5556", "is_locked": False},
        },
    )
    run_probe = AsyncMock(return_value=unlocked_probe)
    enqueue_tasks = AsyncMock(return_value={"status": "started", "tasks": []})
    monkeypatch.setattr(
        tasks.readiness_engine, "run_device_submission_probe", run_probe
    )
    monkeypatch.setattr(tasks.task_queue_service, "enqueue_tasks", enqueue_tasks)
    monkeypatch.setattr(tasks.readiness_engine, "get_active_device_serial", lambda: "device-locked-1")
    set_active_mock = AsyncMock()
    monkeypatch.setattr(tasks.readiness_engine, "set_active_device_serial", set_active_mock)

    result = await tasks.run_task(RunRequest(goal="Run on any ready device"))

    assert result["status"] == "started"
    enqueue_tasks.assert_awaited_once()
    # Check that device_serial passed to enqueue_tasks is the verified unlocked device
    _, kwargs = enqueue_tasks.call_args
    assert kwargs.get("device_serial") == "emulator-5556"
    set_active_mock.assert_called_once_with("emulator-5556")

