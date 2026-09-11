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

from unittest.mock import AsyncMock, MagicMock

from fastapi import HTTPException
import pytest

from apps.admin_console.routers import tasks


@pytest.mark.asyncio
async def test_stop_task_string_false_does_not_clear_all(monkeypatch):
    request = AsyncMock()
    request.json.return_value = {"all": "false", "session_id": "session-1"}
    stop_tasks = MagicMock(return_value=True)
    monkeypatch.setattr(tasks.task_queue_service, "stop_tasks", stop_tasks)

    result = await tasks.stop_task(request)

    assert result == {"status": "stopped", "session_id": "session-1"}
    stop_tasks.assert_called_once_with(
        clear_all=False,
        session_id="session-1",
        device_id=None,
    )


@pytest.mark.asyncio
async def test_stop_task_string_true_clears_all(monkeypatch):
    request = AsyncMock()
    request.json.return_value = {"all": "true"}
    stop_tasks = MagicMock(return_value=True)
    monkeypatch.setattr(tasks.task_queue_service, "stop_tasks", stop_tasks)

    await tasks.stop_task(request)

    stop_tasks.assert_called_once_with(clear_all=True, session_id=None, device_id=None)


@pytest.mark.asyncio
async def test_stop_task_rejects_unrecognized_boolean(monkeypatch):
    request = AsyncMock()
    request.json.return_value = {"all": "not-a-boolean"}
    stop_tasks = MagicMock(return_value=True)
    monkeypatch.setattr(tasks.task_queue_service, "stop_tasks", stop_tasks)

    with pytest.raises(HTTPException) as exc_info:
        await tasks.stop_task(request)

    assert exc_info.value.status_code == 422
    stop_tasks.assert_not_called()
