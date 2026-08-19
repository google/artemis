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
from contextlib import suppress
import signal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import uvicorn

from apps.admin_console.core.state import state
from apps.admin_console.routers.tasks import stream_events
from apps.admin_console.server import ArtemisUvicornServer, app, on_shutdown

WINDOWS_FORCE_SIGNAL = getattr(signal, "SIGBREAK", signal.SIGTERM)


def _pending_queue_get_tasks():
    return {
        task
        for task in asyncio.all_tasks()
        if not task.done() and task.get_coro().__qualname__ == "Queue.get"
    }


@pytest.mark.asyncio
async def test_cancelled_event_stream_reaps_queue_waiter():
    state.is_shutting_down = False
    state.shutdown_event.clear()
    subscribers_before = list(state.ipc_subscribers)
    queue_tasks_before = _pending_queue_get_tasks()

    response = await stream_events("test-session")
    iterator = response.body_iterator
    first_event = await anext(iterator)
    assert "Subscribed to session test-session" in first_event

    next_event = asyncio.create_task(anext(iterator))
    await asyncio.sleep(0)
    next_event.cancel()
    with suppress(asyncio.CancelledError):
        await next_event
    await iterator.aclose()
    await asyncio.sleep(0)

    assert state.ipc_subscribers == subscribers_before
    assert _pending_queue_get_tasks() == queue_tasks_before


@pytest.mark.asyncio
async def test_windows_sigint_is_ignored_while_task_is_active():
    state.current_process = MagicMock(returncode=None)
    state.is_shutting_down = False
    state.shutdown_event.clear()
    server = ArtemisUvicornServer(uvicorn.Config(app))

    with (
        patch("apps.admin_console.server.sys.platform", "win32"),
        patch.object(server, "_schedule_signal_report") as report,
        patch.object(uvicorn.Server, "handle_exit") as base_handle_exit,
    ):
        server.handle_exit(signal.SIGINT, None)

    base_handle_exit.assert_not_called()
    assert report.call_count == 2
    assert state.is_shutting_down is False
    assert state.shutdown_event.is_set() is False
    state.current_process = None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("platform_name", "exit_signal"),
    [("linux", signal.SIGINT), ("win32", WINDOWS_FORCE_SIGNAL)],
)
async def test_non_windows_sigint_and_windows_sigbreak_keep_uvicorn_behavior(
    platform_name, exit_signal
):
    state.current_process = MagicMock(returncode=None)
    state.is_shutting_down = False
    state.shutdown_event.clear()
    server = ArtemisUvicornServer(uvicorn.Config(app))

    with (
        patch("apps.admin_console.server.sys.platform", platform_name),
        patch.object(server, "_schedule_signal_report"),
        patch.object(uvicorn.Server, "handle_exit") as base_handle_exit,
    ):
        server.handle_exit(exit_signal, None)

    base_handle_exit.assert_called_once_with(exit_signal, None)
    assert state.is_shutting_down is True
    assert state.shutdown_event.is_set() is True
    state.current_process = None
    state.is_shutting_down = False
    state.shutdown_event.clear()


@pytest.mark.asyncio
async def test_shutdown_marks_only_ui_owned_running_sessions():
    state.queue_items = [
        {"session_id": "ui-running", "status": "running"},
        {"session_id": "ui-pending", "status": "pending"},
    ]
    state.active_session_id = "external-mcp-session"
    state.current_process = None
    state.worker_task = None

    update_status = MagicMock()
    with (
        patch(
            "apps.admin_console.server.session_repo.update_session_status",
            update_status,
        ),
        patch(
            "apps.admin_console.server.ipc_service.stop_server",
            new=AsyncMock(),
        ),
    ):
        await on_shutdown()

    update_status.assert_called_once_with("ui-running", "cancelled")
    assert state.queue_items == []
    state.active_session_id = None
    state.is_shutting_down = False
    state.shutdown_event.clear()
