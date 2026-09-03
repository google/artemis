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

"""Cancelling a task must let the worker finalize its recording, or recover it."""

import asyncio
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

from apps.admin_console.core.state import state
from apps.admin_console.services.task_queue_service import TaskQueueService, task_queue_service
from artemis.runtime import cancel_requests
from artemis.runtime.device_lock import DeviceLockOwner

TQS = "apps.admin_console.services.task_queue_service"


def _reset_state() -> None:
    state.clear_queue()
    state.queue_items.clear()
    state.active_connections.clear()
    state.cancelled_session_ids.clear()
    state.manually_stopped_run_ids.clear()
    state.current_process = None
    state.active_session_id = None
    state.current_goal = None
    state.current_profile = None


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    marker_dir = tmp_path / "cancel-requests"
    lock_dir = tmp_path / "device-locks"
    lock_dir.mkdir()
    monkeypatch.setattr(cancel_requests, "get_temp_dir", lambda _subfolder=None: marker_dir)
    monkeypatch.setattr(
        "artemis.runtime.device_lock.get_temp_dir", lambda _subfolder=None: lock_dir
    )
    monkeypatch.setenv("ARTEMIS_CANCEL_GRACE_SECONDS", "5")
    for task in list(TaskQueueService._forced_stop_tasks):
        task.cancel()
    TaskQueueService._forced_stop_tasks.clear()
    _reset_state()
    yield marker_dir
    for task in list(TaskQueueService._forced_stop_tasks):
        task.cancel()
    TaskQueueService._forced_stop_tasks.clear()
    _reset_state()


def _owner(pid: int = 24680, session_id: str = "sess-1") -> DeviceLockOwner:
    return DeviceLockOwner(
        pid=pid,
        process_created_at=1234.5,
        token="owner-token",
        device_id="emulator-5554",
        description="frontend task: demo",
        acquired_at="2026-08-24T00:00:00+00:00",
        session_id=session_id,
        ingress="frontend",
    )


def _stop_patches(stack: ExitStack, owner: DeviceLockOwner, alive: bool = True):
    stack.enter_context(patch.object(TaskQueueService, "ensure_worker_running"))
    stack.enter_context(
        patch(f"{TQS}.DeviceExecutionLock.get_active_owners", return_value={"dev": owner})
    )
    stack.enter_context(patch(f"{TQS}.DeviceExecutionLock.is_active_owner", return_value=True))
    stack.enter_context(patch(f"{TQS}.DeviceExecutionLock.cleanup_stale_locks"))
    stack.enter_context(patch(f"{TQS}.session_repo.update_session_status"))
    stack.enter_context(patch("artemis.runtime.trace_store.update_trace_status"))
    pid_alive = stack.enter_context(patch(f"{TQS}.pid_is_alive", return_value=alive))
    terminate = stack.enter_context(patch(f"{TQS}.process_supervisor.terminate_tree_verified"))
    return pid_alive, terminate


@pytest.mark.asyncio
async def test_stop_requests_graceful_cancel_and_skips_kill_when_worker_exits():
    owner = _owner()
    with ExitStack() as stack:
        pid_alive, terminate = _stop_patches(stack, owner)

        assert task_queue_service.stop_tasks(session_id="sess-1") is True

        # The worker is asked to cancel itself; nothing is killed yet.
        terminate.assert_not_called()
        assert cancel_requests.is_cancel_requested(session_id="sess-1")
        assert cancel_requests.is_cancel_requested(pid=24680, process_created_at=1234.5)
        assert len(TaskQueueService._forced_stop_tasks) == 1
        enforcer = next(iter(TaskQueueService._forced_stop_tasks))

        # The worker finalizes and exits on its own within the grace period.
        pid_alive.return_value = False
        await asyncio.wait_for(enforcer, timeout=5)

        terminate.assert_not_called()
    assert not cancel_requests.is_cancel_requested(session_id="sess-1")


@pytest.mark.asyncio
async def test_stop_kills_worker_that_ignores_the_cancel_request(monkeypatch):
    monkeypatch.setenv("ARTEMIS_CANCEL_GRACE_SECONDS", "0.6")
    owner = _owner()
    with ExitStack() as stack:
        _pid_alive, terminate = _stop_patches(stack, owner, alive=True)
        terminate.return_value = True

        assert task_queue_service.stop_tasks(session_id="sess-1") is True
        terminate.assert_not_called()
        enforcer = next(iter(TaskQueueService._forced_stop_tasks))

        await asyncio.wait_for(enforcer, timeout=5)

        terminate.assert_called_once_with(24680, 1234.5)
    assert not cancel_requests.is_cancel_requested(session_id="sess-1")


def test_zero_grace_keeps_the_immediate_kill(monkeypatch):
    monkeypatch.setenv("ARTEMIS_CANCEL_GRACE_SECONDS", "0")
    owner = _owner()
    with ExitStack() as stack:
        _pid_alive, terminate = _stop_patches(stack, owner)
        terminate.return_value = True

        assert task_queue_service.stop_tasks(session_id="sess-1") is True

    terminate.assert_called_once_with(24680, 1234.5)
    assert not cancel_requests.is_cancel_requested(session_id="sess-1")
    assert not TaskQueueService._forced_stop_tasks


def test_stop_of_dead_worker_writes_no_marker():
    owner = _owner()
    with ExitStack() as stack:
        _pid_alive, terminate = _stop_patches(stack, owner, alive=False)

        assert task_queue_service.stop_tasks(session_id="sess-1") is True

    terminate.assert_not_called()
    assert not cancel_requests.is_cancel_requested(session_id="sess-1")
    assert not TaskQueueService._forced_stop_tasks


@pytest.mark.asyncio
async def test_recover_or_fail_recording_finalizes_orphaned_raw_file(tmp_path):
    final = tmp_path / "web_x_FAIL_2026-09-02T16-31-32" / "recording.mp4"
    final.parent.mkdir()
    final.write_bytes(b"video")
    raw_path = str(tmp_path / "web_x" / "recording.mkv")
    repo = MagicMock()
    repo.get_video_recording_for_session.return_value = {
        "status": "recording",
        "local_video_path": raw_path,
        "start_time": 1788391895.4,
    }
    media = MagicMock()
    media.recover_orphaned_recording.return_value = final
    media.path_to_video_url.return_value = "/videos/traces/web_x_FAIL/recording.mp4"
    events: list[tuple[str, dict]] = []

    with (
        patch(f"{TQS}.session_repo", repo),
        patch(f"{TQS}.media_service", media),
        patch.object(
            TaskQueueService, "_broadcast_event", side_effect=lambda t, d: events.append((t, d))
        ),
    ):
        await TaskQueueService._recover_or_fail_recording("sess-1")

    media.recover_orphaned_recording.assert_called_once_with(raw_path, 1788391895.4)
    repo.mark_recording_ready.assert_called_once_with("sess-1", str(final))
    repo.mark_recording_failed_if_pending.assert_not_called()
    assert events == [
        (
            "recording_ready",
            {"session_id": "sess-1", "video_url": "/videos/traces/web_x_FAIL/recording.mp4"},
        )
    ]


@pytest.mark.asyncio
async def test_recover_or_fail_recording_marks_failed_when_nothing_usable():
    repo = MagicMock()
    repo.get_video_recording_for_session.return_value = {
        "status": "recording",
        "local_video_path": "/nowhere/recording.mkv",
        "start_time": 1.0,
    }
    repo.get_video_recordings_map.return_value = {}
    repo.mark_recording_failed_if_pending.return_value = True
    media = MagicMock()
    media.recover_orphaned_recording.return_value = None
    media.build_video_index.return_value = {}
    media.resolve_video_url.return_value = None
    events: list[tuple[str, dict]] = []

    with (
        patch(f"{TQS}.session_repo", repo),
        patch(f"{TQS}.media_service", media),
        patch.object(
            TaskQueueService, "_broadcast_event", side_effect=lambda t, d: events.append((t, d))
        ),
    ):
        await TaskQueueService._recover_or_fail_recording("sess-2")

    repo.mark_recording_ready.assert_not_called()
    repo.mark_recording_failed_if_pending.assert_called_once()
    assert events[0][0] == "recording_failed"


def test_startup_sweep_publishes_orphaned_recordings(tmp_path):
    final = tmp_path / "web_a_FAIL_2026-09-02T16-31-32" / "recording.mp4"
    final.parent.mkdir()
    final.write_bytes(b"video")
    repo = MagicMock()
    repo.get_unfinalized_video_recordings.return_value = [
        {
            "session_id": "sess-a",
            "local_video_path": str(tmp_path / "web_a" / "recording.mkv"),
            "start_time": 10.0,
            "session_status": "cancelled",
        },
        {
            "session_id": "sess-b",
            "local_video_path": str(tmp_path / "web_b" / "recording.mkv"),
            "start_time": None,
            "session_start_time": 20.0,
            "session_status": "failed",
        },
    ]
    repo.mark_recording_ready.return_value = True
    media = MagicMock()
    media.recover_orphaned_recording.side_effect = [final, None]

    with patch(f"{TQS}.session_repo", repo), patch(f"{TQS}.media_service", media):
        assert TaskQueueService.recover_orphaned_recordings_on_launch() == 1

    assert media.recover_orphaned_recording.call_args_list[0].args == (
        str(tmp_path / "web_a" / "recording.mkv"),
        10.0,
    )
    assert media.recover_orphaned_recording.call_args_list[1].args == (
        str(tmp_path / "web_b" / "recording.mkv"),
        20.0,
    )
    repo.mark_recording_ready.assert_called_once_with("sess-a", str(final))


@pytest.mark.asyncio
async def test_recover_or_fail_recording_skips_rows_the_worker_finalized():
    repo = MagicMock()
    repo.get_video_recording_for_session.return_value = {"status": "ready"}
    media = MagicMock()

    with patch(f"{TQS}.session_repo", repo), patch(f"{TQS}.media_service", media):
        await TaskQueueService._recover_or_fail_recording("sess-3")

    media.recover_orphaned_recording.assert_not_called()
    repo.mark_recording_failed_if_pending.assert_not_called()
