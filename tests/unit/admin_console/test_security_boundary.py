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

"""Security regression tests for the Artemis console boundary.

Covers the invariants the no-auth security model depends on:
- secrets never appear in HTTP responses,
- media endpoints cannot read files outside the media allowlist,
- cross-origin browser traffic and DNS-rebinding Hosts are rejected,
- no CORS grants exist,
- lifecycle controls stay loopback-only.
"""

import secrets as py_secrets
from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from apps.admin_console.server import app
from artemis.config import TRACES_PATH, WORKSPACE_ROOT


def _client(**transport_kwargs) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, **transport_kwargs), base_url="http://localhost"
    )


# ---------------------------------------------------------------------------
# Secret exposure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_credentials_endpoint_never_returns_key_material(monkeypatch):
    from artemis.config import settings

    honeytoken = f"sk-honeytoken-{py_secrets.token_hex(16)}"
    monkeypatch.setattr(type(settings), "get_api_key", lambda self, provider: SecretStr(honeytoken))

    async with _client() as ac:
        res = await ac.get("/api/system/credentials")

    assert res.status_code == 200
    assert honeytoken not in res.text
    providers = {entry["name"]: entry["configured"] for entry in res.json()["providers"]}
    assert providers.get("google") is True


@pytest.mark.asyncio
async def test_server_status_omits_lifecycle_token_and_metadata():
    async with _client() as ac:
        res = await ac.get("/api/system/server-status")

    assert res.status_code == 200
    data = res.json()
    assert "metadata" not in data
    assert "lifecycle_token" not in res.text
    assert "cmdline" not in res.text
    assert "current_pid" in data


# ---------------------------------------------------------------------------
# File access boundaries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_videos_endpoint_refuses_dotenv_and_non_video_files():
    async with _client() as ac:
        for target in (".env", "pyproject.toml", "artemis/__init__.py"):
            res = await ac.get(f"/videos/{target}")
            assert res.status_code in (403, 404), target
            assert "API_KEY" not in res.text


@pytest.mark.asyncio
async def test_videos_endpoint_refuses_encoded_traversal():
    async with _client() as ac:
        for target in (
            "%2e%2e/%2e%2e/etc/passwd",
            "..%5c..%5cwindows%5cwin.ini",
            "%2e%2e%2f.env",
        ):
            res = await ac.get(f"/videos/{target}")
            assert res.status_code in (403, 404), target


@pytest.mark.asyncio
async def test_videos_endpoint_still_serves_real_recordings():
    TRACES_PATH.mkdir(parents=True, exist_ok=True)
    probe = TRACES_PATH / f"security-probe-{py_secrets.token_hex(4)}.mp4"
    probe.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    try:
        async with _client() as ac:
            res = await ac.get(f"/videos/{probe.name}")
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("video/mp4")
    finally:
        probe.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_local_file_endpoint_refuses_non_media_workspace_files():
    async with _client() as ac:
        for target in (
            str(WORKSPACE_ROOT / ".env"),
            str(WORKSPACE_ROOT / "pyproject.toml"),
            "file://" + str(WORKSPACE_ROOT / ".env"),
        ):
            res = await ac.get("/local_file", params={"path": target})
            assert res.status_code in (403, 404), target
            assert "API_KEY" not in res.text


@pytest.mark.asyncio
async def test_spa_route_does_not_leak_files_outside_static_roots():
    async with _client() as ac:
        res = await ac.get("/%2e%2e/%2e%2e/pyproject.toml")

    # The catch-all SPA route must fall back to HTML, never the file content.
    assert "requires-python" not in res.text
    assert res.headers["content-type"].startswith("text/html")


# ---------------------------------------------------------------------------
# Browser boundary: Host, Origin, CORS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dns_rebinding_host_is_rejected():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://attacker.example"
    ) as ac:
        res = await ac.get("/api/system/emulator/status")

    assert res.status_code == 403


@pytest.mark.asyncio
async def test_cross_origin_browser_request_is_rejected_without_cors_grant():
    async with _client() as ac:
        res = await ac.post(
            "/api/system/emulator/dismiss",
            headers={"Origin": "https://attacker.example"},
        )

    assert res.status_code == 403
    assert "access-control-allow-origin" not in {k.lower() for k in res.headers}


@pytest.mark.asyncio
async def test_same_origin_browser_request_passes():
    async with _client() as ac:
        res = await ac.get("/api/system/emulator/status", headers={"Origin": "http://localhost"})

    assert res.status_code == 200


@pytest.mark.asyncio
async def test_null_origin_is_rejected():
    async with _client() as ac:
        res = await ac.get("/api/system/emulator/status", headers={"Origin": "null"})

    assert res.status_code == 403


@pytest.mark.asyncio
async def test_security_headers_present_and_api_responses_uncacheable():
    async with _client() as ac:
        res = await ac.get("/api/system/emulator/status")

    assert res.headers.get("x-content-type-options") == "nosniff"
    assert res.headers.get("referrer-policy") == "no-referrer"
    assert res.headers.get("cache-control") == "no-store"


# ---------------------------------------------------------------------------
# Lifecycle controls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_is_loopback_only():
    with patch("threading.Thread") as mock_thread:
        mock_thread.return_value = MagicMock()
        async with AsyncClient(
            transport=ASGITransport(app=app, client=("203.0.113.9", 51000)),
            base_url="http://localhost",
        ) as ac:
            res = await ac.post("/api/system/restart")

        assert res.status_code == 403
        mock_thread.assert_not_called()
