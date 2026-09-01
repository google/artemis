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

import asyncio
import importlib
import json
import os
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from apps.admin_console.core.state import state
from apps.admin_console.routers.tasks import get_status
from apps.admin_console.services.task_queue_service import TaskQueueService, task_queue_service
from artemis.runtime.device_lock import DeviceLockOwner
from artemis.runtime.adb_endpoint import AdbEndpoint


@pytest.fixture(autouse=True)
def clean_state(tmp_path, monkeypatch):
    """Reset global state between tests."""
    isolated_pause_file = tmp_path / ".artemis_paused"
    # Redirect DeviceExecutionLock's lock/queue directory into tmp_path so that
    # enqueue reservations never touch the real %TEMP%/artemis/device-locks dir
    # (a live daemon merges those tickets into its /api/status queue view).
    isolated_lock_dir = tmp_path / "device-locks"
    isolated_lock_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "artemis.runtime.device_lock.get_temp_dir",
        lambda _subfolder=None: isolated_lock_dir,
    )
    state_module = importlib.import_module("apps.admin_console.core.state")
    queue_module = importlib.import_module("apps.admin_console.services.task_queue_service")
    monkeypatch.setattr(state_module, "PAUSE_FILE", isolated_pause_file)
    monkeypatch.setattr(queue_module, "PAUSE_FILE", isolated_pause_file)
    state.clear_queue()
    state.queue_items.clear()
    state.current_process = None
    state.current_goal = None
    state.current_profile = None
    state.active_session_id = None
    state.active_connections.clear()
    state.was_stopped_manually = False
    state.cancelled_session_ids.clear()
    if state.worker_task and not state.worker_task.done():
        state.worker_task.cancel()
    state.worker_task = None
    yield
    state.clear_queue()
    state.queue_items.clear()
    state.active_connections.clear()
    state.cancelled_session_ids.clear()
    state.active_session_id = None
    state.current_process = None
    state.current_goal = None
    state.current_profile = None
    state.was_stopped_manually = False
    if hasattr(state, "recent_submissions"):
        state.recent_submissions.clear()
    if state.worker_task and not state.worker_task.done():
        state.worker_task.cancel()
    state.worker_task = None


def test_paused_error_reads_persisted_reason(tmp_path):
    pause_file = tmp_path / ".artemis_paused"
    pause_file.write_text("LLM Error: 503 UNAVAILABLE: model overloaded", encoding="utf-8")

    with patch("apps.admin_console.core.state.PAUSE_FILE", pause_file):
        assert state.is_paused is True
        assert state.paused_error == "503 UNAVAILABLE: model overloaded"


@pytest.mark.asyncio
async def test_get_next_pending_task():
    state.queue_items = [
        {"session_id": "s1", "goal": "Goal A", "status": "pending"},
        {"session_id": "s2", "goal": "Goal B", "status": "pending"},
    ]
    next_task = TaskQueueService._get_next_pending_task()
    assert next_task is not None
    assert next_task["goal"] == "Goal A"


@pytest.mark.asyncio
async def test_enqueued_task_keeps_its_adb_endpoint_snapshot():
    original = AdbEndpoint.create("127.0.0.1", 5038)
    changed = AdbEndpoint.local()
    with (
        patch.object(TaskQueueService, "ensure_worker_running"),
        patch(
            "apps.admin_console.services.task_queue_service.current_adb_endpoint",
            return_value=original,
        ),
        patch(
            "artemis.runtime.device_pool.device_pool.select_device",
            return_value="emulator-5554",
        ),
    ):
        result = await TaskQueueService.enqueue_tasks(["Keep endpoint"])

    task_item = result["tasks"][0]
    assert task_item["adb_endpoint"]["identity"] == original.identity

    with patch(
        "apps.admin_console.services.task_queue_service.current_adb_endpoint",
        return_value=changed,
    ):
        target = TaskQueueService._task_target(task_item)

    assert target.endpoint == original
    assert target.serial == "emulator-5554"
    assert target.lock_key == f"{original.identity}/emulator-5554"


@pytest.mark.parametrize(
    ("current_status", "returncode", "stopped", "expected"),
    [
        ("completed", 1, False, ("completed", False)),
        ("failed", 0, False, ("failed", False)),
        ("cancelled", 0, False, ("cancelled", False)),
        ("success", 1, False, ("completed", True)),
        ("running", 0, False, ("completed", True)),
        ("running", 1, False, ("failed", True)),
        ("completed", 0, True, ("cancelled", True)),
    ],
)
def test_resolve_terminal_status_preserves_authoritative_result(
    current_status, returncode, stopped, expected
):
    assert (
        TaskQueueService._resolve_terminal_status(current_status, returncode, stopped) == expected
    )


@pytest.mark.asyncio
async def test_remove_task():
    state.queue_items = [
        {"session_id": "s1", "goal": "Goal A", "status": "pending"},
        {"session_id": "s2", "goal": "Goal B", "status": "pending"},
    ]
    TaskQueueService._remove_task("s1")
    assert len(state.queue_items) == 1
    assert state.queue_items[0]["goal"] == "Goal B"


@pytest.mark.asyncio
async def test_stop_tasks_clear_all():
    state.queue_items = [
        {"session_id": "s1", "goal": "Goal 1", "status": "pending"},
        {"session_id": "s2", "goal": "Goal 2", "status": "pending"},
        {"session_id": "s3", "goal": "Goal 3", "status": "pending"},
    ]
    assert len(state.queue_tasks) == 3

    with patch.object(TaskQueueService, "ensure_worker_running"):
        stopped = task_queue_service.stop_tasks(clear_all=True)
        assert len(state.queue_tasks) == 0
        assert len(state.queue_items) == 0


@pytest.mark.asyncio
async def test_stop_tasks_keep_remaining():
    state.queue_items = [
        {"session_id": "s1", "goal": "Goal 1", "status": "running"},
        {"session_id": "s2", "goal": "Goal 2", "status": "pending"},
    ]
    state.active_session_id = "s1"

    mock_proc = MagicMock()
    mock_proc.pid = 12345
    mock_proc.returncode = None
    state.current_process = mock_proc

    with (
        patch(
            "apps.admin_console.services.task_queue_service.process_supervisor.terminate_tree"
        ) as mock_term,
        patch.object(TaskQueueService, "ensure_worker_running"),
        patch(
            "apps.admin_console.services.task_queue_service.session_repo.update_session_status"
        ) as update_status,
        patch(
            "apps.admin_console.services.task_queue_service.session_repo.mark_all_running_cancelled"
        ) as mark_all,
    ):
        stopped = task_queue_service.stop_tasks(clear_all=False)
        assert stopped is True
        mock_proc.kill.assert_called_once()
        mock_term.assert_called_once_with(12345)
        update_status.assert_called_once()
        assert update_status.call_args.args[:2] == ("s1", "cancelled")
        mark_all.assert_not_called()


@pytest.mark.asyncio
async def test_queue_worker_execution_lifecycle():
    executed_goals = []

    async def fake_subprocess_exec(*args, **kwargs):
        goal_arg = args[3]
        executed_goals.append(goal_arg)
        proc = MagicMock()
        proc.pid = 99999
        proc.wait = AsyncMock(return_value=0)
        proc.returncode = 0
        return proc

    with (
        patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess_exec),
        patch("apps.admin_console.services.task_queue_service.session_repo") as mock_repo,
        patch("apps.admin_console.services.task_queue_service.media_service"),
        patch(
            "artemis.runtime.device_pool.device_pool.select_device",
            return_value="emulator-5554",
        ),
    ):
        mock_repo.get_running_session_id.return_value = None
        mock_repo.get_video_recording_for_session.return_value = {"status": "ready"}
        # Enqueue two tasks
        res = await task_queue_service.enqueue_tasks(["First Task", "Second Task"])
        assert res["enqueued_count"] == 2

        # Give event loop time for queue_worker to execute both tasks
        for _ in range(40):
            if len(executed_goals) == 2 and len(state.queue_items) == 0:
                break
            await asyncio.sleep(0.05)

        assert executed_goals == ["First Task", "Second Task"]
        assert len(state.queue_items) == 0
        task = state.worker_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


@pytest.mark.asyncio
async def test_queue_worker_cmd_construction():
    executed_cmds = []
    executed_kwargs = []

    async def fake_subprocess_exec(*args, **kwargs):
        executed_cmds.append(list(args))
        executed_kwargs.append(kwargs)
        proc = MagicMock()
        proc.pid = 88888
        proc.wait = AsyncMock(return_value=0)
        proc.returncode = 0
        return proc

    with (
        patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess_exec),
        patch("apps.admin_console.services.task_queue_service.session_repo") as mock_repo,
        patch("apps.admin_console.services.task_queue_service.media_service"),
        patch(
            "artemis.runtime.device_pool.device_pool.select_device",
            return_value="emulator-5554",
        ),
    ):
        mock_repo.get_running_session_id.return_value = None
        mock_repo.get_video_recording_for_session.return_value = {"status": "ready"}

        enqueue_result = await task_queue_service.enqueue_tasks(
            ["Test Goal with Outputter"],
            profile="pro",
            expected_output="Final summary",
            enable_outputter=True,
            locked_app_package="com.google.android.apps.maps",
            app_path="/path/to/app.apk",
        )

        for _ in range(30):
            if len(executed_cmds) == 1 and len(state.queue_items) == 0:
                break
            await asyncio.sleep(0.05)

        assert len(executed_cmds) == 1
        cmd = executed_cmds[0]
        assert "--enable-outputter" in cmd
        assert "true" not in cmd
        assert "--session-id" in cmd
        assert "--output-description" in cmd
        assert "Final summary" in cmd
        assert "--locked-app" in cmd
        assert "com.google.android.apps.maps" in cmd
        assert "--app-path" in cmd
        assert "/path/to/app.apk" in cmd
        assert (
            executed_kwargs[0]["env"]["ARTEMIS_DEVICE_QUEUE_TICKET"]
            == (enqueue_result["tasks"][0]["queue_ticket"])
        )
        endpoint = enqueue_result["tasks"][0]["adb_endpoint"]
        assert executed_kwargs[0]["env"]["ADB_HOST"] == endpoint["host"]
        assert executed_kwargs[0]["env"]["ADB_PORT"] == str(endpoint["port"])
        assert executed_kwargs[0]["env"]["ADB_SERVER_SOCKET"] == endpoint["socket"]
        assert executed_kwargs[0]["env"]["ARTEMIS_ADB_ENDPOINT_ID"] == endpoint["identity"]
        if sys.platform == "win32":
            assert executed_kwargs[0]["creationflags"] == (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
            assert executed_kwargs[0]["stdout"] == asyncio.subprocess.PIPE
            assert executed_kwargs[0]["stderr"] == asyncio.subprocess.STDOUT
        else:
            assert "creationflags" not in executed_kwargs[0]

        task = state.worker_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


@pytest.mark.asyncio
async def test_forward_worker_output_preserves_split_utf8(capsys):
    stream = asyncio.StreamReader()
    # "worker output: \U0001f600\n" contains a 4-byte UTF-8 emoji (b'\xf0\x9f\x98\x80') starting at byte 15.
    # Splitting at index 17 cuts across the multi-byte sequence to verify partial UTF-8 buffering.
    encoded = "worker output: \U0001f600\n".encode("utf-8")
    stream.feed_data(encoded[:17])
    stream.feed_data(encoded[17:])
    stream.feed_eof()

    await TaskQueueService._forward_worker_output(stream)

    assert capsys.readouterr().out == "worker output: \U0001f600\n"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows console isolation only")
def test_windows_worker_has_no_inherited_console():
    probe = r"""
import ctypes
import json

buffer = (ctypes.c_uint32 * 32)()
count = ctypes.windll.kernel32.GetConsoleProcessList(buffer, len(buffer))
print(json.dumps(list(buffer[:min(count, len(buffer))])))
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        check=True,
        text=True,
        **TaskQueueService._subprocess_creation_kwargs(),
    )

    attached_processes = json.loads(result.stdout)
    assert os.getpid() not in attached_processes


@pytest.mark.asyncio
async def test_stop_tasks_dead_process_resets_state():
    state.queue_items = [
        {"session_id": "s1", "goal": "Goal 1", "status": "pending"},
    ]
    mock_proc = MagicMock()
    mock_proc.pid = 99999999
    mock_proc.returncode = None
    mock_proc.kill.side_effect = ProcessLookupError("No such process")
    state.current_process = mock_proc

    with patch.object(TaskQueueService, "ensure_worker_running"):
        stopped = task_queue_service.stop_tasks(clear_all=False)
        assert stopped is True
        assert state.current_process is None
        assert state.is_running is False
        assert len(state.queue_items) == 0


def test_stop_tasks_terminates_external_global_owner_and_preserves_local_waiter():
    external_owner = DeviceLockOwner(
        pid=24680,
        process_created_at=1234.5,
        token="external-owner-token",
        device_id="emulator-5554",
        description="MCP task: inspect settings",
        acquired_at="2026-08-24T00:00:00+00:00",
        session_id="mcp-session",
        ingress="mcp",
    )
    local_waiter = MagicMock(pid=13579, returncode=None)
    state.current_process = local_waiter
    state.queue_items = [
        {
            "session_id": "frontend-waiter",
            "goal": "Run after MCP",
            "status": "running",
            "queue_ticket": "frontend-ticket",
        }
    ]
    state.active_session_id = "mcp-session"
    state.active_connections["mcp-session"] = {"pid": 24680}

    with (
        patch.object(
            TaskQueueService,
            "ensure_worker_running",
        ),
        patch(
            "apps.admin_console.services.task_queue_service.DeviceExecutionLock.get_active_owner",
            return_value=external_owner,
        ),
        patch(
            "apps.admin_console.services.task_queue_service.DeviceExecutionLock.is_active_owner",
            return_value=True,
        ),
        patch(
            "apps.admin_console.services.task_queue_service.DeviceExecutionLock.cleanup_stale_locks"
        ),
        patch(
            "apps.admin_console.services.task_queue_service.process_supervisor.terminate_tree_verified",
            return_value=True,
        ) as terminate_verified,
        patch(
            "apps.admin_console.services.task_queue_service.session_repo.update_session_status"
        ) as update_status,
        patch(
            "mcp_server.utils.trace_store.update_trace_status"
        ) as update_trace_status,
    ):
        assert task_queue_service.stop_tasks(clear_all=False) is True

    terminate_verified.assert_called_once_with(24680, 1234.5)
    local_waiter.kill.assert_not_called()
    assert state.current_process is local_waiter
    assert state.queue_items[0]["session_id"] == "frontend-waiter"
    assert state.was_stopped_manually is False
    assert "frontend-waiter" not in state.cancelled_session_ids
    assert "mcp-session" not in state.active_connections
    update_status.assert_called_once()
    assert update_status.call_args.args[:2] == ("mcp-session", "cancelled")
    update_trace_status.assert_called_once_with(
        "mcp-session",
        "cancelled",
        error="Task stopped from the Artemis frontend.",
    )


def test_stop_tasks_does_not_kill_stale_reused_pid():
    stale_owner = DeviceLockOwner(
        pid=24680,
        process_created_at=1234.5,
        token="stale-token",
        device_id="emulator-5554",
        description="CLI task",
        acquired_at="2026-08-24T00:00:00+00:00",
        session_id="cli-session",
        ingress="sdk",
    )
    with (
        patch(
            "apps.admin_console.services.task_queue_service.DeviceExecutionLock.get_active_owner",
            return_value=stale_owner,
        ),
        patch(
            "apps.admin_console.services.task_queue_service.DeviceExecutionLock.is_active_owner",
            return_value=False,
        ),
        patch(
            "apps.admin_console.services.task_queue_service.process_supervisor.terminate_tree_verified"
        ) as terminate_verified,
    ):
        assert task_queue_service.stop_tasks(clear_all=False) is False

    terminate_verified.assert_not_called()


def test_stop_tasks_session_target_never_adopts_mismatched_fallback_owner():
    """No live owners is a legal state: a session-targeted stop must not
    terminate a fallback owner that belongs to a different session."""
    other_owner = DeviceLockOwner(
        pid=24680,
        process_created_at=1234.5,
        token="other-owner-token",
        device_id="emulator-5554",
        description="Task for another session",
        acquired_at="2026-08-24T00:00:00+00:00",
        session_id="other-session",
        ingress="cli",
    )
    with (
        patch(
            "apps.admin_console.services.task_queue_service.DeviceExecutionLock.get_active_owners",
            return_value={},
        ),
        patch(
            "apps.admin_console.services.task_queue_service.DeviceExecutionLock.get_active_owner",
            return_value=other_owner,
        ),
        patch(
            "apps.admin_console.services.task_queue_service.DeviceExecutionLock.is_active_owner",
            return_value=True,
        ),
        patch(
            "apps.admin_console.services.task_queue_service.process_supervisor.terminate_tree_verified"
        ) as terminate_verified,
        patch(
            "apps.admin_console.services.task_queue_service.session_repo.update_session_status"
        ),
        patch(
            "apps.admin_console.services.task_queue_service.session_repo.get_session_by_id",
            return_value=None,
        ),
    ):
        task_queue_service.stop_tasks(clear_all=False, session_id="wanted-session")

    terminate_verified.assert_not_called()


@pytest.mark.asyncio
async def test_status_reports_external_global_owner_without_ipc_connection():
    external_owner = DeviceLockOwner(
        pid=24680,
        process_created_at=1234.5,
        token="external-owner-token",
        device_id="emulator-5554",
        description="CLI task: inspect settings",
        acquired_at="2026-08-24T00:00:00+00:00",
        session_id="cli-session",
        ingress="cli",
    )
    with (
        patch.object(TaskQueueService, "ensure_worker_running"),
        patch(
            "apps.admin_console.routers.tasks.DeviceExecutionLock.get_active_owner",
            return_value=external_owner,
        ),
        patch("apps.admin_console.routers.tasks.session_repo") as repo,
        patch("apps.admin_console.routers.tasks.model_service") as models,
    ):
        repo.get_latest_session.return_value = None
        repo.get_session_by_id.return_value = None
        models.get_active_model_info.return_value = None
        result = await get_status()

    assert result["status"] == "running"
    assert result["session_id"] == "cli-session"
    assert result["pid"] == 24680
    assert result["goal"] == "CLI task: inspect settings"


@pytest.mark.asyncio
async def test_cancel_task_triggers_next_pending_task():
    executed_goals = []

    async def fake_subprocess_exec(*args, **kwargs):
        goal_arg = args[3]
        executed_goals.append(goal_arg)
        proc = MagicMock()
        proc.pid = 77777

        async def wait_side_effect():
            if goal_arg == "Task 1":
                # Simulate task 1 being stopped manually mid-execution
                await asyncio.sleep(0.05)
                task_queue_service.stop_tasks(clear_all=False)
                return -9
            await asyncio.sleep(0.02)
            return 0

        proc.wait = AsyncMock(side_effect=wait_side_effect)
        proc.returncode = 0
        return proc

    with (
        patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess_exec),
        patch("apps.admin_console.services.task_queue_service.session_repo") as mock_repo,
        patch("apps.admin_console.services.task_queue_service.media_service"),
        patch(
            "artemis.runtime.device_pool.device_pool.select_device",
            return_value="emulator-5554",
        ),
    ):
        mock_repo.get_running_session_id.return_value = None
        mock_repo.get_video_recording_for_session.return_value = {"status": "ready"}
        await task_queue_service.enqueue_tasks(["Task 1", "Task 2"])

        for _ in range(40):
            if len(executed_goals) == 2 and len(state.queue_items) == 0:
                break
            await asyncio.sleep(0.05)

        assert executed_goals == ["Task 1", "Task 2"]
        assert len(state.queue_items) == 0

        task = state.worker_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


@pytest.mark.asyncio
async def test_immediate_cancel_ignores_stale_ipc_and_runs_next_task():
    executed_goals = []
    task1_session_id = None

    async def fake_subprocess_exec(*args, **kwargs):
        goal_arg = args[3]
        executed_goals.append(goal_arg)
        proc = MagicMock()
        proc.pid = 88888

        async def wait_side_effect():
            if goal_arg == "Task 1":
                return -9
            await asyncio.sleep(0.02)
            return 0

        proc.wait = AsyncMock(side_effect=wait_side_effect)
        proc.returncode = 0
        return proc

    with (
        patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess_exec),
        patch("apps.admin_console.services.task_queue_service.session_repo") as mock_repo,
        patch("apps.admin_console.services.task_queue_service.media_service"),
        patch(
            "artemis.runtime.device_pool.device_pool.select_device",
            return_value="emulator-5554",
        ),
    ):
        mock_repo.get_running_session_id.return_value = None
        mock_repo.get_video_recording_for_session.return_value = {"status": "ready"}

        # Enqueue Task 1
        res1 = await task_queue_service.enqueue_tasks(["Task 1"])
        task1_session_id = res1["tasks"][0]["session_id"]

        # Immediately stop Task 1
        task_queue_service.stop_tasks(clear_all=False)
        assert task1_session_id in state.cancelled_session_ids

        # Simulate stale session_started arriving over IPC for the cancelled Task 1
        from apps.admin_console.services.ipc_service import ipc_service

        ipc_service.sanitize_event_data("session_started", {"session_id": task1_session_id})
        # If IPC handler checks cancelled_session_ids, state.active_session_id should not get stuck
        assert state.active_session_id != task1_session_id
        assert state.is_running is False

        # Enqueue Task 2 and verify it runs immediately without getting stuck in pending
        await task_queue_service.enqueue_tasks(["Task 2"])

        for _ in range(40):
            if "Task 2" in executed_goals and len(state.queue_items) == 0:
                break
            await asyncio.sleep(0.05)

        assert "Task 2" in executed_goals
        assert len(state.queue_items) == 0

        task = state.worker_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


@pytest.mark.asyncio
async def test_wait_for_worker_process_watchdog_handles_reaped_process():
    """Verify that _wait_for_worker_process never hangs even if the process was already reaped."""
    mock_proc = MagicMock()
    mock_proc.pid = 999999
    mock_proc.returncode = None

    async def hanging_wait():
        await asyncio.sleep(10)
        return 0

    mock_proc.wait = AsyncMock(side_effect=hanging_wait)

    with patch("psutil.Process") as mock_psutil_proc:
        mock_p = MagicMock()
        mock_p.is_running.return_value = False
        mock_psutil_proc.return_value = mock_p

        rc = await TaskQueueService._wait_for_worker_process(mock_proc)
        assert rc == -15


def test_darwin_terminate_process_tree_preserves_direct_child_for_asyncio():
    """Verify darwin terminate_process_tree does not pass direct children to psutil.wait_procs."""
    import os
    from artemis.platform.darwin import DarwinPlatformProcess

    process = DarwinPlatformProcess()
    current_pid = os.getpid()

    with (
        patch("psutil.Process") as mock_psutil_proc,
        patch("psutil.wait_procs") as mock_wait_procs,
    ):
        parent = MagicMock()
        parent.pid = 12345
        parent.ppid.return_value = current_pid
        parent.children.return_value = []
        parent.is_running.return_value = False

        mock_psutil_proc.return_value = parent
        mock_wait_procs.return_value = ([], [])

        success = process.terminate_process_tree(12345, timeout_seconds=0.1)
        assert success is True
        parent.send_signal.assert_called_once()
        for call_args in mock_wait_procs.call_args_list:
            procs_waited = call_args[0][0]
            assert parent not in procs_waited


@pytest.mark.asyncio
async def test_enqueue_tasks_unified_ingress():
    """Verify enqueue_tasks correctly propagates ingress, custom session_id, and conversation_id."""
    with (
        patch.object(TaskQueueService, "ensure_worker_running"),
        patch("apps.admin_console.services.task_queue_service.DeviceExecutionLock.reserve", return_value="mock-ticket-unified"),
    ):
        res = await task_queue_service.enqueue_tasks(
            ["Test unified goal"],
            profile="flash",
            ingress="mcp",
            session_id="custom-mcp-session-123",
            conversation_id="conv-456",
        )
        assert res["status"] in ("started", "queued")
        assert len(res["tasks"]) == 1
        task = res["tasks"][0]
        assert task["session_id"] == "custom-mcp-session-123"
        assert task["ingress"] == "mcp"
        assert task["conversation_id"] == "conv-456"
        assert task["goal"] == "Test unified goal"


@pytest.mark.asyncio
async def test_queue_worker_notifies_conversation():
    """Verify queue_worker calls notify() when conversation_id is attached to task."""
    executed_cmds = []

    async def fake_subprocess_exec(*args, **kwargs):
        executed_cmds.append(list(args))
        proc = MagicMock()
        proc.pid = 99999
        proc.wait = AsyncMock(return_value=0)
        proc.returncode = 0
        return proc

    with (
        patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess_exec),
        patch("apps.admin_console.services.task_queue_service.media_service"),
        patch("mcp_server.notifiers.notify") as mock_notify,
        patch("apps.admin_console.services.task_queue_service.session_repo") as mock_repo,
        patch(
            "artemis.runtime.device_pool.device_pool.try_list_devices_async",
            new=AsyncMock(return_value=[]),
        ),
    ):
        mock_repo.get_running_session_id.return_value = None

        await task_queue_service.enqueue_tasks(
            ["Notify goal"],
            profile="flash",
            ingress="mcp",
            conversation_id="conv-789",
            device_serial="test-mock-serial",
        )

        for _ in range(30):
            if mock_notify.called and len(state.queue_items) == 0:
                break
            await asyncio.sleep(0.05)

        mock_notify.assert_called_once()
        kwargs = mock_notify.call_args[1]
        assert kwargs["conversation_id"] == "conv-789"
        assert "Notify goal" in kwargs["message"]

        task = state.worker_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


def test_stop_tasks_by_session_id_targets_correct_task_among_multiple():
    """Stopping by session_id terminates only the targeted owner among multiple concurrent devices."""
    owner_a = DeviceLockOwner(
        pid=11111,
        process_created_at=100.0,
        token="token-a",
        device_id="device-a",
        description="Task on device A",
        acquired_at="2026-08-24T00:00:00+00:00",
        session_id="session-a",
        ingress="cli",
    )
    owner_b = DeviceLockOwner(
        pid=22222,
        process_created_at=200.0,
        token="token-b",
        device_id="device-b",
        description="Task on device B",
        acquired_at="2026-08-24T00:00:00+00:00",
        session_id="session-b",
        ingress="mcp",
    )

    with (
        patch(
            "apps.admin_console.services.task_queue_service.DeviceExecutionLock.get_active_owners",
            return_value={"device_a": owner_a, "device_b": owner_b},
        ),
        patch(
            "apps.admin_console.services.task_queue_service.DeviceExecutionLock.is_active_owner",
            return_value=True,
        ),
        patch(
            "apps.admin_console.services.task_queue_service.DeviceExecutionLock.cleanup_stale_locks"
        ) as cleanup_locks,
        patch(
            "apps.admin_console.services.task_queue_service.process_supervisor.terminate_tree_verified",
            return_value=True,
        ) as terminate_verified,
        patch(
            "apps.admin_console.services.task_queue_service.session_repo.update_session_status"
        ) as update_status,
        patch("mcp_server.utils.trace_store.update_trace_status") as update_trace,
    ):
        # Explicitly stop session-b
        assert task_queue_service.stop_tasks(clear_all=False, session_id="session-b") is True

        # Only session-b's process (22222) should be terminated
        terminate_verified.assert_called_once_with(22222, 200.0)
        cleanup_locks.assert_called_once_with("device-b")
        update_status.assert_called_once()
        assert update_status.call_args[0][0] == "session-b"
        assert update_status.call_args[0][1] == "cancelled"
        update_trace.assert_called_once_with(
            "session-b",
            "cancelled",
            error="Task stopped from the Artemis frontend.",
        )


def test_stop_tasks_by_device_id_targets_specific_device():
    """Stopping by device_id targets the active task on that specific device."""
    owner_a = DeviceLockOwner(
        pid=33333,
        process_created_at=300.0,
        token="token-a",
        device_id="pixel-8",
        description="Task on Pixel 8",
        acquired_at="2026-08-24T00:00:00+00:00",
        session_id="session-pixel8",
        ingress="sdk",
    )

    with (
        patch(
            "apps.admin_console.services.task_queue_service.DeviceExecutionLock.get_active_owners",
            return_value={"pixel_8": owner_a},
        ),
        patch(
            "apps.admin_console.services.task_queue_service.DeviceExecutionLock.is_active_owner",
            return_value=True,
        ),
        patch("apps.admin_console.services.task_queue_service.DeviceExecutionLock.cleanup_stale_locks"),
        patch(
            "apps.admin_console.services.task_queue_service.process_supervisor.terminate_tree_verified",
            return_value=True,
        ) as terminate_verified,
        patch("apps.admin_console.services.task_queue_service.session_repo.update_session_status"),
    ):
        assert task_queue_service.stop_tasks(clear_all=False, device_id="pixel-8") is True
        terminate_verified.assert_called_once_with(33333, 300.0)


def test_stop_tasks_queued_item_by_session_id():
    """Stopping a queued task by session_id cancels its reservation and removes it from queue."""
    state.queue_items = [
        {"session_id": "queue-1", "goal": "Goal 1", "status": "pending", "queue_ticket": "ticket-1"},
        {"session_id": "queue-2", "goal": "Goal 2", "status": "pending", "queue_ticket": "ticket-2"},
    ]

    with (
        patch(
            "apps.admin_console.services.task_queue_service.DeviceExecutionLock.get_active_owners",
            return_value={},
        ),
        patch(
            "apps.admin_console.services.task_queue_service.DeviceExecutionLock.get_active_owner",
            return_value=None,
        ),
        patch(
            "apps.admin_console.services.task_queue_service.DeviceExecutionLock.cancel_reservation",
            return_value=True,
        ) as cancel_res,
        patch("apps.admin_console.services.task_queue_service.session_repo.update_session_status") as update_status,
    ):
        assert task_queue_service.stop_tasks(clear_all=False, session_id="queue-1") is True
        cancel_res.assert_called_once_with("ticket-1")
        # queue-2 should remain in queue
        assert len(state.queue_items) == 1
        update_status.assert_called_once()
        assert update_status.call_args[0][0] == "queue-1"
        assert update_status.call_args[0][1] == "cancelled"


@pytest.mark.asyncio
async def test_enqueue_tasks_deduplicates_by_session_id():
    state.queue_items = []
    state.active_session_id = None

    with (
        patch("apps.admin_console.services.task_queue_service.session_repo"),
        patch("apps.admin_console.services.task_queue_service.DeviceExecutionLock.reserve", return_value="ticket-123"),
    ):
        res1 = await task_queue_service.enqueue_tasks(["Task goal"], session_id="sid-dedup-1")
        assert len(state.queue_items) == 1
        assert res1["enqueued_count"] == 1

        # Second submission with same session_id must not enqueue a duplicate
        res2 = await task_queue_service.enqueue_tasks(["Task goal again"], session_id="sid-dedup-1")
        assert len(state.queue_items) == 1
        assert res2["enqueued_count"] == 0
        assert res2["tasks"][0]["session_id"] == "sid-dedup-1"


@pytest.mark.asyncio
async def test_enqueue_tasks_debounces_rapid_identical_submissions():
    state.queue_items = []
    state.active_session_id = None

    with (
        patch("apps.admin_console.services.task_queue_service.session_repo"),
        patch("apps.admin_console.services.task_queue_service.DeviceExecutionLock.reserve", return_value="ticket-456"),
        patch(
            "artemis.runtime.device_pool.device_pool.try_list_devices_async",
            new=AsyncMock(return_value=[]),
        ),
        patch.object(TaskQueueService, "ensure_worker_running"),
    ):
        res1 = await task_queue_service.enqueue_tasks(["Rapid duplicate goal"], device_serial="dev-1")
        assert len(state.queue_items) == 1
        assert res1["enqueued_count"] == 1

        # Immediate second submission (within 1s) must debounce
        res2 = await task_queue_service.enqueue_tasks(["Rapid duplicate goal"], device_serial="dev-1")
        assert len(state.queue_items) == 1
        assert res2["enqueued_count"] == 0




