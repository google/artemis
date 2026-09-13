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

"""
Unit and Integration Tests for GCP Cloud Brain Outer Server Gateway.

Tests multi-tenant authentication, device registration, device mutex locking,
workspace path resolution, RESTful /api/v1/* endpoints, and SSE event streaming.
"""

import pytest
from fastapi.testclient import TestClient

pytestmark = [pytest.mark.integration, pytest.mark.cloud]
pytest.importorskip(
    "cloud_service",
    reason="optional cloud_service package is not installed",
)

from cloud_service.gateway.auth import TokenAuthProvider
from cloud_service.gateway.manager import ConnectionManager

try:
    from cloud_service.server import app
except ImportError:
    from apps.cloud_service.server import app


client = TestClient(app)


def test_token_auth_provider():
    auth = TokenAuthProvider()
    # Test valid tenant name as Bearer token
    assert auth.authenticate("Bearer alice") == "alice"
    assert auth.authenticate("Bearer bob") == "bob"
    assert auth.authenticate("alice-token") == "alice"
    # Test invalid token
    assert auth.authenticate("Bearer invalid-token-1234") is None
    assert auth.authenticate("invalid-token-1234") is None


def test_connection_manager_device_lifecycle():
    mgr = ConnectionManager()
    device = mgr.register_device("alice", "pixel8-test-001", model="Pixel 8")
    assert device.tenant == "alice"
    assert device.device_serial == "pixel8-test-001"
    assert device.status == "ONLINE"

    devices = mgr.list_tenant_devices("alice")
    assert len(devices) == 1
    assert devices[0].device_serial == "pixel8-test-001"

    # Test unregister
    assert mgr.unregister_device("alice", "pixel8-test-001") is True
    assert len(mgr.list_tenant_devices("alice")) == 0
    assert mgr.unregister_device("alice", "non-existent") is False


def test_workspace_dir_resolution():
    mgr = ConnectionManager()
    ws_path = mgr.get_workspace_dir("alice", "session_test_001")
    assert "alice" in str(ws_path)
    assert "session_test_001" in str(ws_path)
    assert ws_path.exists()


def test_api_v1_tenant_profile():
    headers = {"Authorization": "Bearer alice"}
    resp = client.get("/api/v1/tenants/me", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["tenant_id"] == "alice"
    assert "max_concurrent_sessions" in data


def test_api_v1_unauthorized():
    resp = client.get("/api/v1/tenants/me")
    assert resp.status_code == 401
    resp2 = client.get("/api/v1/tenants/me", headers={"Authorization": "Bearer bad-token"})
    assert resp2.status_code in (401, 403)


def test_api_v1_device_register_and_list():
    headers = {"Authorization": "Bearer alice"}
    reg_payload = {
        "device_serial": "pixel8-api-001",
        "model": "Pixel 8 Pro",
        "android_version": "14",
    }
    resp = client.post("/api/v1/devices/register", json=reg_payload, headers=headers)
    assert resp.status_code == 200
    data = resp.json().get("device", resp.json())
    assert data["device_serial"] == "pixel8-api-001"
    assert data["model"] == "Pixel 8 Pro"

    # List devices
    list_resp = client.get("/api/v1/devices", headers=headers)
    assert list_resp.status_code == 200
    devices = list_resp.json()["devices"]
    assert any(d["device_serial"] == "pixel8-api-001" for d in devices)

    # Unregister device
    del_resp = client.delete("/api/v1/devices/pixel8-api-001", headers=headers)
    assert del_resp.status_code == 200
    assert del_resp.json()["status"] == "unregistered"


@pytest.mark.asyncio
async def test_device_mutex_lock():
    mgr = ConnectionManager()
    mgr.register_device("alice", "pixel8-mutex-001")
    lock = mgr.get_device_lock("pixel8-mutex-001")

    # Simulate device busy by acquiring lock
    await lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="currently BUSY"):
            await mgr.spawn_user_session("alice", "pixel8-mutex-001", "test goal")
    finally:
        lock.release()
