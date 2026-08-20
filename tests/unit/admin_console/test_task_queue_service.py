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
from apps.admin_console.services.task_queue_service import TaskQueueService, task_queue_service


@pytest.fixture(autouse=True)
def clean_state(tmp_path, monkeypatch):
    """Reset global state between tests."""
    isolated_pause_file = tmp_path / ".artemis_paused"
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
    state.was_stopped_manually = False
    if state.worker_task and not state.worker_task.done():
        state.worker_task.cancel()
    state.worker_task = None
    yield
    state.clear_queue()
    state.queue_items.clear()
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
    ):
        stopped = task_queue_service.stop_tasks(clear_all=False)
        assert stopped is True
        mock_proc.kill.assert_called_once()
        mock_term.assert_called_once_with(12345)


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
    ):
        mock_repo.get_running_session_id.return_value = None
        # Enqueue two tasks
        res = await task_queue_service.enqueue_tasks(["First Task", "Second Task"])
        assert res["enqueued_count"] == 2

        # Give event loop time for queue_worker to execute both tasks
        for _ in range(30):
            if len(executed_goals) == 2:
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
    ):
        mock_repo.get_running_session_id.return_value = None

        await task_queue_service.enqueue_tasks(
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
        assert "--output-description" in cmd
        assert "Final summary" in cmd
        assert "--locked-app" in cmd
        assert "com.google.android.apps.maps" in cmd
        assert "--app-path" in cmd
        assert "/path/to/app.apk" in cmd
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
    encoded = "worker 输出正常\n".encode()
    stream.feed_data(encoded[:9])
    stream.feed_data(encoded[9:])
    stream.feed_eof()

    await TaskQueueService._forward_worker_output(stream)

    assert capsys.readouterr().out == "worker 输出正常\n"


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
    ):
        mock_repo.get_running_session_id.return_value = None
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
    ):
        mock_repo.get_running_session_id.return_value = None

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
            if "Task 2" in executed_goals:
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
