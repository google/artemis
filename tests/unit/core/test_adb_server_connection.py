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

"""Tests for process-wide local and forwarded ADB server selection."""

import os
import subprocess

import pytest

from artemis.config import settings
from artemis.core.diagnostics.adb_server_connection import (
    AdbServerConnectionManager,
    AdbServerEndpoint,
    InvalidAdbServerEndpoint,
    adb_server_connection,
)
from artemis.core.diagnostics.engine import ReadinessEngine
from artemis.runtime.adb_endpoint import ADB_ENDPOINT_ID_ENV, AdbSession


@pytest.mark.parametrize(
    ("host", "port"),
    [
        ("", 5037),
        ("http://127.0.0.1", 5037),
        ("127.0.0.1/path", 5037),
        ("127.0.0.1", 0),
        ("127.0.0.1", 65536),
    ],
)
def test_validate_endpoint_rejects_invalid_addresses(host, port):
    with pytest.raises(InvalidAdbServerEndpoint):
        AdbServerConnectionManager.validate_endpoint(host, port)


def test_endpoint_formats_ipv6_socket():
    endpoint = AdbServerConnectionManager.validate_endpoint("[::1]", 5038)

    assert endpoint.socket == "tcp:[::1]:5038"
    assert endpoint.mode == "remote"


def test_adb_session_builds_explicit_endpoint_command_and_environment():
    endpoint = AdbServerEndpoint("server.example", 5040)
    session = AdbSession(endpoint, adb_path="adb")

    assert session.command(["devices"]) == [
        "adb",
        "-H",
        "server.example",
        "-P",
        "5040",
        "devices",
    ]
    environment = session.environment({"EXISTING": "value"})
    assert environment["EXISTING"] == "value"
    assert environment["ADB_SERVER_SOCKET"] == "tcp:server.example:5040"
    assert environment[ADB_ENDPOINT_ID_ENV] == endpoint.identity


@pytest.mark.asyncio
async def test_connect_activates_forwarded_server_after_success(monkeypatch):
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["environment"] = kwargs["env"]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "List of devices attached\n"
                "R58M1234 device product:husky model:Pixel_8_Pro transport_id:1\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(settings, "ADB_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "ADB_PORT", 5037)
    monkeypatch.setenv("ADB_SERVER_SOCKET", "tcp:127.0.0.1:5999")
    manager = AdbServerConnectionManager(adb_resolver=lambda: "adb", env_files=[])

    result = await manager.connect("127.0.0.1", 5038, persist=False)

    assert result["success"] is True
    assert result["devices"] == [
        {
            "serial": "R58M1234",
            "state": "device",
            "model": "Pixel 8 Pro",
            "product": "husky",
        }
    ]
    assert captured["command"] == [
        "adb",
        "-H",
        "127.0.0.1",
        "-P",
        "5038",
        "devices",
        "-l",
    ]
    assert "ADB_SERVER_SOCKET" not in captured["environment"]
    assert settings.ADB_HOST == "127.0.0.1"
    assert settings.ADB_PORT == 5038
    assert os.environ["ADB_SERVER_SOCKET"] == "tcp:127.0.0.1:5038"


@pytest.mark.asyncio
async def test_connect_does_not_activate_unreachable_server(monkeypatch):
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="cannot connect to daemon at tcp:127.0.0.1:5038",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(settings, "ADB_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "ADB_PORT", 5037)
    manager = AdbServerConnectionManager(adb_resolver=lambda: "adb", env_files=[])

    result = await manager.connect("127.0.0.1", 5038, persist=False)

    assert result["success"] is False
    assert result["error_code"] == "server_unreachable"
    assert settings.ADB_PORT == 5037


@pytest.mark.asyncio
async def test_probe_never_activates_reachable_endpoint(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout="List of devices attached\n",
            stderr="",
        ),
    )
    monkeypatch.setattr(settings, "ADB_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "ADB_PORT", 5037)
    manager = AdbServerConnectionManager(adb_resolver=lambda: "adb", env_files=[])

    result = await manager.probe("127.0.0.1", 5038)

    assert result["success"] is True
    assert result["endpoint"]["port"] == 5038
    assert settings.ADB_PORT == 5037


@pytest.mark.asyncio
async def test_default_resolver_uses_the_shared_toolchain(monkeypatch):
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "artemis.core.diagnostics.adb_server_connection.toolchain.resolve",
        lambda name: "sdk-platform-tools-adb" if name == "adb" else None,
    )

    def fake_run(command, **kwargs):
        captured["command"] = command
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="List of devices attached\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    manager = AdbServerConnectionManager(env_files=[])

    result = await manager.probe("127.0.0.1", 5038)

    assert result["success"] is True
    assert captured["command"][0] == "sdk-platform-tools-adb"


@pytest.mark.asyncio
async def test_connect_persists_endpoint_without_replacing_other_settings(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("EXISTING_VALUE=keep-me\n", encoding="utf-8")

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="List of devices attached\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    manager = AdbServerConnectionManager(adb_resolver=lambda: "adb", env_files=[env_file])

    result = await manager.connect("tunnel.local", 5038, persist=True)

    content = env_file.read_text(encoding="utf-8")
    assert result["persisted"] is True
    assert "EXISTING_VALUE=keep-me" in content
    assert "ADB_HOST=tunnel.local" in content
    assert "ADB_PORT=5038" in content


@pytest.mark.asyncio
async def test_connect_remains_active_when_persistence_fails(monkeypatch):
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="List of devices attached\n",
            stderr="",
        )

    def fail_persist(endpoint):
        raise OSError("read-only")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(settings, "ADB_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "ADB_PORT", 5037)
    manager = AdbServerConnectionManager(adb_resolver=lambda: "adb", env_files=[])
    monkeypatch.setattr(manager, "_persist", fail_persist)

    result = await manager.connect("127.0.0.1", 5038, persist=True)

    assert result["success"] is True
    assert result["persisted"] is False
    assert result["persistence_error"] == "read-only"
    assert settings.ADB_PORT == 5038


@pytest.mark.asyncio
async def test_use_local_server_restores_standard_endpoint(monkeypatch):
    monkeypatch.setattr(settings, "ADB_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "ADB_PORT", 5038)
    manager = AdbServerConnectionManager(env_files=[])

    result = await manager.use_local_server(persist=False)

    assert result["success"] is True
    assert result["endpoint"] == AdbServerEndpoint("127.0.0.1", 5037).to_dict()
    assert settings.ADB_PORT == 5037
    assert os.environ["ADB_SERVER_SOCKET"] == "tcp:127.0.0.1:5037"


@pytest.mark.asyncio
async def test_restart_skips_kill_server_for_forwarded_endpoint(monkeypatch):
    endpoint = AdbServerEndpoint("127.0.0.1", 5038)
    monkeypatch.setattr(adb_server_connection, "current_endpoint", lambda: endpoint)

    result = await ReadinessEngine().restart_adb_server()

    assert result["success"] is True
    assert result["skipped"] is True
    assert result["endpoint"]["port"] == 5038


@pytest.mark.asyncio
async def test_remote_endpoint_blocks_local_wireless_connection(monkeypatch):
    monkeypatch.setattr(
        adb_server_connection,
        "current_endpoint",
        lambda: AdbServerEndpoint("127.0.0.1", 5038),
    )

    result = await ReadinessEngine().connect_wireless_adb("192.168.1.100", 5555)

    assert result["success"] is False
    assert "Switch to local ADB" in result["message"]


@pytest.mark.asyncio
async def test_remote_endpoint_blocks_local_emulator_launch(monkeypatch):
    monkeypatch.setattr(
        adb_server_connection,
        "current_endpoint",
        lambda: AdbServerEndpoint("127.0.0.1", 5038),
    )

    result = await ReadinessEngine().launch_emulator("Pixel_8")

    assert result["status"] == "failed"
    assert "Switch to local ADB" in result["error"]
