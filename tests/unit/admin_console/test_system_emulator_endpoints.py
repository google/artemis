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

"""Unit tests for System Emulator Launch & Lifecycle Endpoints."""

import pytest
from httpx import ASGITransport, AsyncClient

from apps.admin_console.server import app


@pytest.mark.asyncio
async def test_system_emulator_status_endpoint():
    """Verify GET /api/system/emulator/status returns valid schema."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        res = await ac.get("/api/system/emulator/status")
        assert res.status_code == 200
        data = res.json()
        assert "status" in data
        assert "stage_message" in data
        assert "progress_percent" in data
        assert "logs" in data


@pytest.mark.asyncio
async def test_system_emulator_dismiss_endpoint():
    """Verify POST /api/system/emulator/dismiss resets tracker state."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        res = await ac.post("/api/system/emulator/dismiss")
        assert res.status_code == 200
        data = res.json()
        assert data.get("success") is True


@pytest.mark.asyncio
async def test_system_emulator_launch_empty_validation():
    """Verify POST /api/system/emulator/launch validates empty avd_name."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        res = await ac.post("/api/system/emulator/launch", json={"avd_name": "   "})
        assert res.status_code == 400
