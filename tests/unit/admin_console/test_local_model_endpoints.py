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

"""Unit tests for the local-model lifecycle endpoints.

These guard the click-to-run console path: only resolvable model refs may
spawn ``artemis model`` subprocesses, ``stop`` stays port-scoped, and task
state/logs surface for polling.
"""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from apps.admin_console.routers import system
from apps.admin_console.server import app


@pytest.fixture(autouse=True)
def _clear_model_tasks():
    system._MODEL_TASKS.clear()
    yield
    system._MODEL_TASKS.clear()


class _FakeStdout:
    def __init__(self, lines):
        self._lines = lines

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for line in self._lines:
            yield line


class _FakeProc:
    def __init__(self, rc=0, lines=(b"done\n",)):
        self.stdout = _FakeStdout(lines)
        self._rc = rc

    async def wait(self):
        return self._rc


def _patch_subprocess(monkeypatch, captured: list, rc=0, lines=(b"done\n",)):
    async def _fake_exec(*cmd, **kwargs):
        captured.append(list(cmd))
        return _FakeProc(rc=rc, lines=lines)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)


def _client(**kwargs):
    return AsyncClient(transport=ASGITransport(app=app, **kwargs), base_url="http://localhost")


@pytest.mark.asyncio
async def test_status_reports_cli_unavailable_without_model_module():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as ac:
        res = await ac.get("/api/system/local-models/status")
    assert res.status_code == 200
    data = res.json()
    # The `artemis model` CLI ships with the local-models feature; on builds
    # without it the console must fall back to copy-to-terminal guidance.
    assert data["commands_available"] == system._model_cli_available()
    assert data["tasks"] == {}


@pytest.mark.asyncio
async def test_pull_returns_501_when_cli_missing():
    async with _client() as ac:
        res = await ac.post("/api/system/local-models/pull", json={"alias": "gemma4-e4b"})
    if system._model_cli_available():
        pytest.skip("model CLI present in this build")
    assert res.status_code == 501
    assert "terminal" in res.json()["detail"]


@pytest.mark.asyncio
async def test_pull_rejects_unresolvable_alias(monkeypatch):
    monkeypatch.setattr(system, "_model_cli_available", lambda: True)
    async with _client() as ac:
        for bad in ("not-a-model", "--port", "gemma4-e4b; rm -rf /"):
            res = await ac.post("/api/system/local-models/pull", json={"alias": bad})
            assert res.status_code == 400, bad
    assert system._MODEL_TASKS == {}


@pytest.mark.asyncio
async def test_pull_rejects_remote_client(monkeypatch):
    monkeypatch.setattr(system, "_model_cli_available", lambda: True)
    async with _client(client=("192.168.1.20", 42000)) as ac:
        res = await ac.post("/api/system/local-models/pull", json={"alias": "gemma4-e4b"})
    assert res.status_code == 403
    assert "local-only" in res.json()["detail"]


@pytest.mark.asyncio
async def test_pull_starts_task_and_completes(monkeypatch):
    monkeypatch.setattr(system, "_model_cli_available", lambda: True)
    captured = []
    _patch_subprocess(monkeypatch, captured, rc=0, lines=(b"downloading\n", b"done\n"))

    async with _client() as ac:
        res = await ac.post("/api/system/local-models/pull", json={"alias": "gemma4-e4b"})
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "running"
        assert body["key"] == "pull:gemma4-e4b"

    assert captured[0][-3:] == ["model", "pull", "gemma4-e4b"]

    await system._MODEL_TASKS["pull:gemma4-e4b"]["watcher"]
    task = system._MODEL_TASKS["pull:gemma4-e4b"]
    assert task["status"] == "done"
    assert task["returncode"] == 0
    assert "downloading" in "\n".join(task["log"])


@pytest.mark.asyncio
async def test_pull_is_idempotent_while_running(monkeypatch):
    monkeypatch.setattr(system, "_model_cli_available", lambda: True)
    started = asyncio.Event()
    release = asyncio.Event()

    class _HangingProc(_FakeProc):
        async def wait(self):
            release.set()
            return 0

    async def _fake_exec(*cmd, **kwargs):
        proc = _HangingProc()
        started.set()
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    async with _client() as ac:
        res1 = await ac.post("/api/system/local-models/pull", json={"alias": "gemma4-e4b"})
        res2 = await ac.post("/api/system/local-models/pull", json={"alias": "gemma4-e4b"})
        assert res1.json()["status"] == res2.json()["status"] == "running"


@pytest.mark.asyncio
async def test_serve_passes_port_and_serve_cmd(monkeypatch):
    monkeypatch.setattr(system, "_model_cli_available", lambda: True)
    captured = []
    _patch_subprocess(monkeypatch, captured)

    async with _client() as ac:
        res = await ac.post(
            "/api/system/local-models/serve", json={"alias": "gemma4-e4b", "port": 8097}
        )
    assert res.status_code == 200
    assert captured[0][-5:] == ["model", "serve", "gemma4-e4b", "--port", "8097"]


@pytest.mark.asyncio
async def test_stop_is_port_scoped_without_positional_alias(monkeypatch):
    monkeypatch.setattr(system, "_model_cli_available", lambda: True)
    captured = []
    _patch_subprocess(monkeypatch, captured)

    async with _client() as ac:
        res = await ac.post(
            "/api/system/local-models/stop", json={"alias": "gemma4-e4b", "port": 8080}
        )
    assert res.status_code == 200
    cmd = captured[0]
    # CLI: `artemis model stop --port N` — the alias must not appear as argv.
    assert cmd[-3:] == ["model", "stop", "--port"] or cmd[-4:] == [
        "model",
        "stop",
        "--port",
        "8080",
    ]
    assert "gemma4-e4b" not in cmd


@pytest.mark.asyncio
async def test_failed_process_surfaces_log_and_returncode(monkeypatch):
    monkeypatch.setattr(system, "_model_cli_available", lambda: True)
    _patch_subprocess(monkeypatch, [], rc=2, lines=(b"error: no space left\n",))

    async with _client() as ac:
        res = await ac.post("/api/system/local-models/pull", json={"alias": "gemma4-e4b"})
        assert res.status_code == 200

    await system._MODEL_TASKS["pull:gemma4-e4b"]["watcher"]
    async with _client() as ac:
        status = (await ac.get("/api/system/local-models/status")).json()
    task = status["tasks"]["pull:gemma4-e4b"]
    assert task["status"] == "failed"
    assert task["returncode"] == 2
    assert any("no space left" in line for line in task["log"])


@pytest.mark.asyncio
async def test_org_repo_alias_is_accepted(monkeypatch):
    monkeypatch.setattr(system, "_model_cli_available", lambda: True)
    captured = []
    _patch_subprocess(monkeypatch, captured)

    async with _client() as ac:
        res = await ac.post(
            "/api/system/local-models/pull", json={"alias": "mlx-community/gemma-4-e4b-it-4bit"}
        )
    assert res.status_code == 200
    assert captured[0][-1] == "mlx-community/gemma-4-e4b-it-4bit"


@pytest.mark.asyncio
async def test_invalid_port_rejected():
    async with _client() as ac:
        res = await ac.post(
            "/api/system/local-models/serve", json={"alias": "gemma4-e4b", "port": 70000}
        )
    assert res.status_code == 422
