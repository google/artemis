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

"""Tests for POST /api/system/reload-env."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from apps.admin_console.server import app
from artemis.config.settings import settings


@pytest.mark.asyncio
async def test_reload_env_updates_settings_and_masks_secrets(tmp_path, monkeypatch):
    secret = "sk-reload-secret-do-not-echo"
    env_file = tmp_path / ".env"
    env_file.write_text(f"GEMINI_API_KEY={secret}\n")
    monkeypatch.setattr("artemis.config.paths.get_env_file", lambda: env_file)
    settings.set_api_key("google", "stale-before-reload", persist_to_env=False)

    fake_report = {"overall_ready": True, "probes": [], "timestamp": 1}
    with patch(
        "apps.admin_console.routers.system.readiness_engine.run_all",
        new=AsyncMock(return_value=fake_report),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://localhost") as ac:
            res = await ac.post("/api/system/reload-env")

    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert secret not in res.text
    assert "env_vars" not in data
    assert settings.get_api_key("google").get_secret_value() == secret
