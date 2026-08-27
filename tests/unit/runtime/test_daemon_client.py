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

import json
from unittest.mock import MagicMock, patch
import pytest

from artemis.runtime.daemon_client import (
    ensure_daemon_running,
    is_daemon_running,
    is_standalone_forced,
    spawn_daemon,
    submit_task_to_daemon,
)


def test_is_standalone_forced(monkeypatch):
    monkeypatch.delenv("ARTEMIS_STANDALONE", raising=False)
    assert is_standalone_forced() is False

    monkeypatch.setenv("ARTEMIS_STANDALONE", "1")
    assert is_standalone_forced() is True

    monkeypatch.setenv("ARTEMIS_STANDALONE", "true")
    assert is_standalone_forced() is True

    monkeypatch.setenv("ARTEMIS_STANDALONE", "0")
    assert is_standalone_forced() is False


def test_is_daemon_running_success():
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        assert is_daemon_running() is True


def test_is_daemon_running_failure():
    with patch("urllib.request.urlopen", side_effect=OSError("Connection refused")):
        assert is_daemon_running() is False


def test_spawn_daemon():
    mock_proc = MagicMock()
    mock_proc.pid = 54321
    with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
        proc = spawn_daemon(host="127.0.0.1", port=8000)
        assert proc is mock_proc
        assert mock_popen.called


def test_ensure_daemon_running_when_already_active():
    with patch("artemis.runtime.daemon_client.is_daemon_running", return_value=True):
        ok, base_url = ensure_daemon_running(host="127.0.0.1", port=8000)
        assert ok is True
        assert base_url == "http://127.0.0.1:8000"


def test_ensure_daemon_running_standalone_override(monkeypatch):
    monkeypatch.setenv("ARTEMIS_STANDALONE", "1")
    ok, base_url = ensure_daemon_running()
    assert ok is False
    assert base_url is None


def test_ensure_daemon_running_auto_spawns():
    with (
        patch("artemis.runtime.daemon_client.is_daemon_running", side_effect=[False, True]),
        patch("artemis.runtime.daemon_client.spawn_daemon") as mock_spawn,
    ):
        ok, base_url = ensure_daemon_running(timeout=1.0, wait_ready=True)
        assert ok is True
        assert base_url == "http://127.0.0.1:8000"
        mock_spawn.assert_called_once()


def test_submit_task_to_daemon():
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = b'{"status": "queued", "tasks": [{"session_id": "test-sess"}]}'
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        res = submit_task_to_daemon(
            "test goal",
            profile="flash",
            device_serial="pixel-8",
            session_id="custom-sess-789",
            ingress="mcp",
            conversation_id="conv-123",
        )
        assert res is not None
        assert res["status"] == "queued"
        assert res["tasks"][0]["session_id"] == "test-sess"

        # Verify request payload
        req = mock_urlopen.call_args[0][0]
        data = json.loads(req.data.decode("utf-8"))
        assert data["goal"] == "test goal"
        assert data["profile"] == "flash"
        assert data["device_serial"] == "pixel-8"
        assert data["session_id"] == "custom-sess-789"
        assert data["ingress"] == "mcp"
        assert data["conversation_id"] == "conv-123"

