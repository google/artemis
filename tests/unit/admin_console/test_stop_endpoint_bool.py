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

"""POST /api/stop must not treat the string 'false' as stop-all."""

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from apps.admin_console.routers.bool_params import coerce_bool
from apps.admin_console.server import app


@pytest.mark.parametrize(
    ("value", "default", "expected"),
    [
        (False, False, False),
        (True, False, True),
        ("false", False, False),
        ("FALSE", False, False),
        ("0", False, False),
        ("no", False, False),
        ("off", False, False),
        ("", False, False),
        ("true", False, True),
        ("TRUE", False, True),
        ("1", False, True),
        ("yes", False, True),
        ("on", False, True),
        (0, False, False),
        (1, False, True),
        (None, False, False),
        (None, True, True),
        ("maybe", False, False),
        ("maybe", True, True),
    ],
)
def test_coerce_bool(value, default, expected):
    assert coerce_bool(value, default=default) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "query", "expected_clear_all"),
    [
        ({"all": False, "session_id": "sess-1"}, "", False),
        ({"all": "false", "session_id": "sess-1"}, "", False),
        ({"all": "true", "session_id": "sess-1"}, "", True),
        ({"all": True}, "", True),
        ({"session_id": "sess-1"}, "", False),
        ({"session_id": "sess-1"}, "?all=false", False),
        ({}, "?all=true", True),
    ],
)
async def test_stop_endpoint_parses_all_flag(payload, query, expected_clear_all):
    with patch(
        "apps.admin_console.routers.tasks.task_queue_service.stop_tasks",
        return_value=True,
    ) as stop_tasks:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://localhost") as client:
            response = await client.post(f"/api/stop{query}", json=payload)

    assert response.status_code == 200
    stop_tasks.assert_called_once()
    kwargs = stop_tasks.call_args.kwargs
    assert kwargs["clear_all"] is expected_clear_all
