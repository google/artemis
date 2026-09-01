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

"""Comprehensive End-to-End Test for Artemis Multi-Port System.

Verifies end-to-end functionality across all entry points and ports:
1. Configuration & Default Model (gemini-3.5-flash-lite)
2. Daemon Server API & Multi-Device Discovery (/api/devices, /api/status)
3. Multi-Device Detection (Physical phone + Android Emulator)
4. MCP Server Tools (mobile_get_device_state, mobile_run_task, mobile_manage_task)
5. Python SDK (ArtemisClient with device targeting & concurrency modes)
6. CLI Entry Points (artemis run, batch, status)
7. Device Execution Lock Isolation (Multi-device concurrent execution)
"""

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
import pytest

from artemis.config import (
    DEFAULT_MODEL,
    ROOT_DIR,
    Settings,
    settings,
    get_config_path,
)
from artemis.runtime.daemon_client import (
    ensure_daemon_running,
    get_daemon_session,
    get_daemon_status,
    is_daemon_running,
    submit_task_to_daemon,
    stop_task_on_daemon,
)
from artemis.runtime.device_lock import DeviceExecutionLock
from artemis.runtime.device_pool import device_pool
from artemis.interfaces.sdk.client import ArtemisClient, ConcurrencyMode
from mcp_server.tools.device_state import mobile_get_device_state
from mcp_server.tools.task_runner import mobile_run_task
from mcp_server.tools.task_manager import mobile_manage_task
from artemis.runtime import trace_store


@pytest.fixture(scope="module", autouse=True)
def ensure_daemon():
    """Ensure Artemis Daemon is running for E2E tests."""
    assert ensure_daemon_running(wait_ready=True), "Artemis Daemon failed to start"
    yield


def test_01_default_model_configured():
    """Verify default model configuration loads properly across config and router."""
    assert DEFAULT_MODEL is not None
    cfg_path = get_config_path("artemis.jsonc")
    assert cfg_path.exists()
    content = cfg_path.read_text(encoding="utf-8")
    assert "default" in content

    # Check Settings pydantic model
    s = Settings()
    assert s.ARTEMIS_DEFAULT_MODEL is not None


def test_02_daemon_device_discovery_sees_phone_and_emulator():
    """Verify Daemon API /api/devices detects both the physical phone and emulator."""
    status = get_daemon_status()
    assert status is not None
    assert status.get("status") in ("idle", "running", "ok")
    if "model_info" in status and status["model_info"]:
        assert status["model_info"]["id"] is not None

    req = urllib.request.Request("http://127.0.0.1:8000/api/devices")
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        devices = data.get("devices", [])
        serials = [d["serial"] for d in devices]

        # Must detect both devices
        assert any(s.startswith("emulator") for s in serials), f"Emulator not found in {serials}"
        assert any(not s.startswith("emulator") for s in serials), f"Physical phone not found in {serials}"

        # Check is_emulator flag classification
        for d in devices:
            if d["serial"].startswith("emulator"):
                assert d["is_emulator"] is True
            else:
                assert d["is_emulator"] is False


@pytest.mark.asyncio
async def test_03_mcp_device_state_inspects_both_devices():
    """Verify MCP mobile_get_device_state reads element hierarchies from both devices."""
    devices = device_pool.list_devices()
    emulator_serial = next((d.serial for d in devices if d.is_emulator), None)
    phone_serial = next((d.serial for d in devices if not d.is_emulator), None)

    assert emulator_serial, f"No emulator connected in {devices}"
    assert phone_serial, f"No physical phone connected in {devices}"

    # Inspect emulator state
    emu_state = await mobile_get_device_state(view_type="hierarchy", device_serial=emulator_serial)
    assert isinstance(emu_state, str)
    assert len(emu_state) > 0
    assert "Bounds:" in emu_state or "Text:" in emu_state or len(emu_state.strip()) > 0

    # Inspect physical phone state
    phone_state = await mobile_get_device_state(view_type="hierarchy", device_serial=phone_serial)
    assert isinstance(phone_state, str)
    assert len(phone_state) > 0
    assert "Bounds:" in phone_state or "Text:" in phone_state or len(phone_state.strip()) > 0


def test_04_mcp_run_and_manage_task_via_daemon():
    """Verify MCP mobile_run_task dispatches to Daemon and mobile_manage_task tracks it."""
    devices = device_pool.list_devices()
    emulator_serial = next((d.serial for d in devices if d.is_emulator), None)
    assert emulator_serial, "No emulator connected"

    res = mobile_run_task(
        task_desc="E2E Test task from MCP port",
        conversation_id="e2e-conv-mcp-1",
        model="Flash",
        device_serial=emulator_serial,
    )
    assert "trace_id" in res
    trace_id = res["trace_id"]
    assert res["status"] in ("started", "queued", "running")

    # Verify tracking state via mobile_manage_task
    status_res = mobile_manage_task(action="status", trace_id=trace_id)
    assert status_res["trace_id"] == trace_id
    assert "status" in status_res
    assert status_res.get("device_serial") == emulator_serial

    # Cleanup: stop the test task on Daemon and release locks
    stop_task_on_daemon(session_id=trace_id)
    time.sleep(1.5)
    DeviceExecutionLock.cleanup_stale_locks()


def test_05_sdk_client_targeting_and_concurrency():
    """Verify Python SDK ArtemisClient correctly targets specific devices."""
    devices = device_pool.list_devices()
    emulator_serial = next((d.serial for d in devices if d.is_emulator), None)
    phone_serial = next((d.serial for d in devices if not d.is_emulator), None)

    assert emulator_serial, "Emulator serial required"
    assert phone_serial, "Phone serial required"

    client_emu = ArtemisClient(device_id=emulator_serial, concurrency_mode="per_device")
    assert client_emu.device_serial == emulator_serial
    assert client_emu.concurrency_mode == "per_device"

    client_phone = ArtemisClient(device_id=phone_serial, concurrency_mode="per_device")
    assert client_phone.device_serial == phone_serial

    # Verify multi-device lock independence
    DeviceExecutionLock.cleanup_stale_locks()
    lock_emu = DeviceExecutionLock(device_id=emulator_serial)
    lock_phone = DeviceExecutionLock(device_id=phone_serial)

    # Both locks can be acquired simultaneously because device serials differ
    acq1 = lock_emu.acquire(timeout=5.0)
    assert acq1 is True, "Failed to acquire emulator lock"
    try:
        acq2 = lock_phone.acquire(timeout=5.0)
        assert acq2 is True, "Failed to acquire phone lock concurrently (multi-device parallel mode)"
        lock_phone.release()
    finally:
        lock_emu.release()


def test_06_cli_daemon_and_standalone_options():
    """Verify CLI commands properly expose and route through Daemon or standalone."""
    # artemis status
    res = subprocess.run([sys.executable, "-m", "artemis.main", "status"], capture_output=True, text=True)
    assert res.returncode == 0
    assert "online" in res.stdout.lower() or "running" in res.stdout.lower()

    # artemis run --help exposes --standalone and --device
    res_run = subprocess.run([sys.executable, "-m", "artemis.main", "run", "--help"], capture_output=True, text=True)
    assert res_run.returncode == 0
    assert "--standalone" in res_run.stdout
    assert "--device" in res_run.stdout

    # artemis batch --help exposes --standalone
    res_batch = subprocess.run([sys.executable, "-m", "artemis.main", "batch", "--help"], capture_output=True, text=True)
    assert res_batch.returncode == 0
    assert "--standalone" in res_batch.stdout


def test_07_daemon_session_details_api():
    """Verify Daemon GET /api/sessions/{session_id} API endpoint."""
    res = submit_task_to_daemon(
        goal="E2E session query test",
        profile="flash",
        ingress="e2e_test",
    )
    assert "session_id" in res
    session_id = res["session_id"]

    # Poll session details
    session_data = get_daemon_session(session_id)
    assert session_data is not None
    assert session_data["session_id"] == session_id
    assert session_data["initial_goal"] == "E2E session query test"

    # Clean up test task
    stop_task_on_daemon(session_id=session_id)
