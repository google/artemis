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

"""Tests for GET /api/system/server-status.

``is_artemis_daemon`` calls this route with a short timeout, so it must not
use ``server_lifecycle.get_server_status`` (which runs ``lsof``).
"""

import os

import pytest
from httpx import ASGITransport, AsyncClient

from apps.admin_console.server import app


def _boom(*args, **kwargs):
    raise AssertionError("server-status must not scan ports/processes")


@pytest.mark.asyncio
async def test_server_status_answers_in_process_without_port_scans(monkeypatch):
    from artemis.runtime import server_lifecycle

    monkeypatch.setattr(server_lifecycle, "find_server_pids", _boom)
    monkeypatch.setattr(server_lifecycle, "get_server_status", _boom)
    monkeypatch.setattr(server_lifecycle.subprocess, "run", _boom)
    monkeypatch.setattr(server_lifecycle, "read_server_info", lambda: None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as ac:
        res = await ac.get("/api/system/server-status")

    assert res.status_code == 200
    data = res.json()
    assert data["running"] is True
    assert data["current_pid"] == os.getpid()
    assert data["active_pid"] == os.getpid()
    assert os.getpid() in data["pids"]
    assert data["url"] == f"http://localhost:{data['port']}"
    assert data["uptime_seconds"] is None or data["uptime_seconds"] >= 0
    assert "metadata" not in data


@pytest.mark.asyncio
async def test_server_status_uses_metadata_start_time(monkeypatch):
    from apps.admin_console.core.state import state
    from artemis.runtime import server_lifecycle

    monkeypatch.setattr(
        server_lifecycle,
        "read_server_info",
        lambda: {"pid": os.getpid(), "port": state.port, "started_at": 1.0, "lifecycle_token": "x"},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as ac:
        res = await ac.get("/api/system/server-status")

    data = res.json()
    assert data["uptime_seconds"] > 1_000_000
    assert data["pids"] == [os.getpid()]
    assert "lifecycle_token" not in res.text
