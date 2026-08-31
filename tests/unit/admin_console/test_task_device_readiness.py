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

from types import SimpleNamespace
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


@pytest.mark.asyncio
async def test_idempotent_retry_skips_device_probe_for_active_session(monkeypatch):
    run_probe = AsyncMock()
    monkeypatch.setattr(
        tasks.readiness_engine, "run_device_submission_probe", run_probe
    )
    monkeypatch.setattr(tasks.state, "active_session_id", "sdk-task-1")
    monkeypatch.setattr(tasks.state, "active_connections", {})
    monkeypatch.setattr(
        tasks.session_repo,
        "get_session_by_id",
        lambda session_id: None,
    )

    result = await tasks.run_task(
        RunRequest(
            goal="Open Settings",
            session_id="sdk-task-1",
            device_serial="pixel-10",
            ingress="python_sdk",
        )
    )

    assert result["status"] == "running"
    assert result["enqueued_count"] == 0
    assert result["tasks"][0]["session_id"] == "sdk-task-1"
    run_probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_explicit_device_is_rejected_before_readiness_probe(monkeypatch):
    run_probe = AsyncMock()
    monkeypatch.setattr(tasks.readiness_engine, "run_device_submission_probe", run_probe)
    monkeypatch.setattr(tasks.state, "active_session_id", None)
    monkeypatch.setattr(tasks.state, "active_connections", {})
    monkeypatch.setattr(tasks.state, "queue_items", [])
    monkeypatch.setattr(tasks.session_repo, "get_session_by_id", lambda session_id: None)
    monkeypatch.setattr(
        tasks.device_pool,
        "try_list_devices_async",
        AsyncMock(
            return_value=[
                SimpleNamespace(serial="pixel-10", state="device"),
            ]
        ),
    )

    result = await tasks.run_task(
        RunRequest(
            goal="Must not run",
            device_serial="missing-device",
            session_id="00000000-0000-4000-8000-000000000099",
        )
    )

    assert result["status"] == "rejected"
    assert result["tasks"] == []
    assert "not connected" in result["error"]
    run_probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_device_is_rejected_when_attached_but_not_ready(monkeypatch):
    run_probe = AsyncMock()
    monkeypatch.setattr(tasks.readiness_engine, "run_device_submission_probe", run_probe)
    monkeypatch.setattr(tasks.state, "active_session_id", None)
    monkeypatch.setattr(tasks.state, "active_connections", {})
    monkeypatch.setattr(tasks.state, "queue_items", [])
    monkeypatch.setattr(tasks.session_repo, "get_session_by_id", lambda session_id: None)
    monkeypatch.setattr(
        tasks.device_pool,
        "try_list_devices_async",
        AsyncMock(
            return_value=[
                SimpleNamespace(serial="pixel-10", state="unauthorized"),
            ]
        ),
    )

    result = await tasks.run_task(
        RunRequest(
            goal="Must not run",
            device_serial="pixel-10",
            session_id="00000000-0000-4000-8000-000000000100",
        )
    )

    assert result["status"] == "rejected"
    assert "unauthorized" in result["error"]
    run_probe.assert_not_awaited()


@pytest.mark.parametrize("enumeration", [None, []])
@pytest.mark.asyncio
async def test_explicit_device_proceeds_when_enumeration_is_indeterminate(
    monkeypatch, enumeration
):
    """A failed (None) or empty enumeration must never hard-reject an explicit
    serial: the submission queues and fails downstream if the device is truly
    absent. This is the startup-storm regression guard."""
    run_probe = AsyncMock(return_value=None)
    enqueue_tasks = AsyncMock(return_value={"status": "queued", "tasks": []})
    monkeypatch.setattr(tasks.readiness_engine, "run_device_submission_probe", run_probe)
    monkeypatch.setattr(tasks.task_queue_service, "enqueue_tasks", enqueue_tasks)
    monkeypatch.setattr(tasks.state, "active_session_id", None)
    monkeypatch.setattr(tasks.state, "active_connections", {})
    monkeypatch.setattr(tasks.state, "queue_items", [])
    monkeypatch.setattr(tasks.session_repo, "get_session_by_id", lambda session_id: None)
    monkeypatch.setattr(
        tasks.device_pool,
        "try_list_devices_async",
        AsyncMock(return_value=enumeration),
    )

    result = await tasks.run_task(
        RunRequest(
            goal="Queue through the startup storm",
            device_serial="pixel-10",
            session_id="00000000-0000-4000-8000-000000000101",
        )
    )

    assert result["status"] == "queued"
    enqueue_tasks.assert_awaited_once()
    _, kwargs = enqueue_tasks.call_args
    assert kwargs.get("device_serial") == "pixel-10"

