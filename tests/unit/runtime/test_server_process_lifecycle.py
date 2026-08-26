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

"""Unit tests for Artemis Server Lifecycle Management and API Endpoints."""

import json
import os
from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from apps.admin_console.server import app
from artemis.runtime.server_lifecycle import (
    clear_server_info,
    find_server_pids,
    get_server_status,
    is_port_in_use,
    read_server_info,
    stop_server,
    write_server_info,
)


def test_write_read_clear_server_info(tmp_path, monkeypatch):
    """Verify write, read, and clear round-trip for server metadata file."""
    fake_info_file = tmp_path / "test_server.json"
    monkeypatch.setattr(
        "artemis.runtime.server_lifecycle.get_server_info_file", lambda: fake_info_file
    )

    # Initial read should be None
    assert read_server_info() is None

    # Write info
    written = write_server_info(port=8888, host="127.0.0.1", pid=99999)
    assert written == fake_info_file
    assert fake_info_file.exists()

    # Read back
    info = read_server_info()
    assert info is not None
    assert info["pid"] == 99999
    assert info["port"] == 8888
    assert info["host"] == "127.0.0.1"
    assert "started_at" in info

    # Clear info
    clear_server_info()
    assert not fake_info_file.exists()
    assert read_server_info() is None


def test_find_server_pids_from_metadata(tmp_path, monkeypatch):
    """Verify find_server_pids discovers PID saved in metadata file."""
    fake_info_file = tmp_path / "test_server.json"
    fake_info_file.write_text(
        json.dumps({"pid": os.getpid(), "port": 8000, "started_at": 100.0}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "artemis.runtime.server_lifecycle.get_server_info_file", lambda: fake_info_file
    )
    monkeypatch.setattr("artemis.runtime.server_lifecycle.is_port_in_use", lambda port, **k: True)

    pids = find_server_pids(8000)
    assert os.getpid() in pids


def test_get_server_status_offline(monkeypatch):
    """Verify get_server_status returns correct structure when server is offline."""
    monkeypatch.setattr("artemis.runtime.server_lifecycle.is_port_in_use", lambda port, **k: False)
    monkeypatch.setattr("artemis.runtime.server_lifecycle.find_server_pids", lambda port: [])
    monkeypatch.setattr("artemis.runtime.server_lifecycle.read_server_info", lambda: None)

    status = get_server_status(8000)
    assert status["running"] is False
    assert status["port"] == 8000
    assert status["pids"] == []
    assert status["url"] is None


def test_get_server_status_online(monkeypatch):
    """Verify get_server_status returns correct structure when server is online."""
    monkeypatch.setattr("artemis.runtime.server_lifecycle.is_port_in_use", lambda port, **k: True)
    monkeypatch.setattr("artemis.runtime.server_lifecycle.find_server_pids", lambda port: [12345])
    monkeypatch.setattr(
        "artemis.runtime.server_lifecycle.read_server_info",
        lambda: {"pid": 12345, "port": 8000, "started_at": 1000.0},
    )

    status = get_server_status(8000)
    assert status["running"] is True
    assert status["port"] == 8000
    assert status["pids"] == [12345]
    assert status["active_pid"] == 12345
    assert status["url"] == "http://localhost:8000"
    assert status["admin_url"] == "http://localhost:8000/admin"
    assert status["uptime_seconds"] is not None


def test_stop_server_when_not_running(monkeypatch):
    """Verify stop_server returns early when no server is running."""
    monkeypatch.setattr("artemis.runtime.server_lifecycle.is_port_in_use", lambda port, **k: False)
    monkeypatch.setattr("artemis.runtime.server_lifecycle.find_server_pids", lambda port: [])

    success, msg, stopped = stop_server(8000)
    assert success is True
    assert stopped == []
    assert "No active server" in msg


def test_stop_server_terminates_tree(monkeypatch):
    """Verify stop_server calls ProcessSupervisor.terminate_tree and returns stopped pids."""
    terminated_pids = []

    monkeypatch.setattr("artemis.runtime.server_lifecycle.find_server_pids", lambda port: [12345])
    port_in_use = [True, False]

    def mock_in_use(port, **kwargs):
        if port_in_use:
            return port_in_use.pop(0)
        return False

    monkeypatch.setattr("artemis.runtime.server_lifecycle.is_port_in_use", mock_in_use)
    monkeypatch.setattr(
        "artemis.runtime.supervisor.ProcessSupervisor.terminate_tree",
        lambda pid, timeout_seconds=4.0: terminated_pids.append(pid),
    )
    monkeypatch.setattr(
        "artemis.runtime.device_lock.DeviceExecutionLock.cleanup_stale_locks",
        lambda: 1,
    )

    success, msg, stopped = stop_server(8000)
    assert success is True
    assert stopped == [12345]
    assert 12345 in terminated_pids
    assert "stopped" in msg.lower()


@pytest.mark.asyncio
async def test_api_server_status():
    """Verify GET /api/system/server-status returns server runtime metadata."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/api/system/server-status")
        assert response.status_code == 200
        data = response.json()
        assert "port" in data
        assert "running" in data
        assert "current_pid" in data


@pytest.mark.asyncio
async def test_api_restart_endpoint():
    """Verify POST /api/system/restart accepts request and schedules restart."""
    with patch("threading.Thread") as mock_thread:
        mock_instance = MagicMock()
        mock_thread.return_value = mock_instance

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.post("/api/system/restart")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "restarting"
            assert "reconnection" in data["message"].lower() or "restarting" in data["message"].lower()
            mock_thread.assert_called_once()
            mock_instance.start.assert_called_once()


@pytest.mark.asyncio
async def test_api_shutdown_endpoint():
    """Verify POST /api/system/shutdown accepts request and schedules shutdown."""
    with patch("threading.Thread") as mock_thread:
        mock_instance = MagicMock()
        mock_thread.return_value = mock_instance

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.post("/api/system/shutdown")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "shutting_down"
            mock_thread.assert_called_once()
            mock_instance.start.assert_called_once()
