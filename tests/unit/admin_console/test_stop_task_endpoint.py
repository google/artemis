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

from unittest.mock import MagicMock
import pytest
from apps.admin_console.routers import tasks


class DummyRequest:
    def __init__(self, json_data=None):
        self._json_data = json_data

    async def json(self):
        if self._json_data is None:
            raise ValueError("No JSON body")
        return self._json_data


@pytest.mark.parametrize(
    "body,expected_clear_all",
    [
        ({"all": "false"}, False),
        ({"all": False}, False),
        ({"all": "0"}, False),
        ({"clear_all": "false"}, False),
        ({"clear_all": False}, False),
        ({"all": "true"}, True),
        ({"all": True}, True),
        ({"all": "1"}, True),
        ({"clear_all": "true"}, True),
        ({"clear_all": True}, True),
    ],
)
@pytest.mark.asyncio
async def test_stop_task_boolean_parsing_in_body(monkeypatch, body, expected_clear_all):
    mock_stop_tasks = MagicMock(return_value=True)
    monkeypatch.setattr(tasks.task_queue_service, "stop_tasks", mock_stop_tasks)

    req = DummyRequest(json_data=body)
    resp = await tasks.stop_task(request=req)

    assert resp["status"] == "stopped"
    mock_stop_tasks.assert_called_once_with(
        clear_all=expected_clear_all,
        session_id=None,
        device_id=None,
    )


@pytest.mark.asyncio
async def test_stop_task_fallback_to_query_params(monkeypatch):
    mock_stop_tasks = MagicMock(return_value=True)
    monkeypatch.setattr(tasks.task_queue_service, "stop_tasks", mock_stop_tasks)

    req = DummyRequest(json_data=None)
    resp = await tasks.stop_task(request=req, all=False, clear_all=None)

    assert resp["status"] == "stopped"
    mock_stop_tasks.assert_called_once_with(
        clear_all=False,
        session_id=None,
        device_id=None,
    )


@pytest.mark.asyncio
async def test_stop_task_clear_all_query_param_precedence(monkeypatch):
    mock_stop_tasks = MagicMock(return_value=True)
    monkeypatch.setattr(tasks.task_queue_service, "stop_tasks", mock_stop_tasks)

    req = DummyRequest(json_data=None)
    await tasks.stop_task(request=req, all=True, clear_all=False)

    mock_stop_tasks.assert_called_once_with(
        clear_all=False,
        session_id=None,
        device_id=None,
    )
