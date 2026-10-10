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

"""iOS Simulator platform-parity tests: discovery, pool, locks, queue, routing.

Every test is hermetic — ``simctl``/``xcrun``/adb are never invoked; device
enumeration is stubbed at the discovery boundary.
"""

import os
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from artemis.drivers.ios.discovery import (
    clear_ios_simulator_cache,
    list_ios_simulators,
    list_ios_simulators_sync,
    parse_simctl_devices,
)
from artemis.runtime.adb_endpoint import AdbEndpoint
from artemis.runtime.device_target import IOS_LOCK_SCOPE, AdbTarget, IosTarget
from artemis.runtime.device_lock import DeviceExecutionLock, DeviceLockOwner
from artemis.runtime.ios_device_pool import IosDevicePool


@pytest.fixture(autouse=True)
def isolated_lock_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "artemis.runtime.device_lock.get_temp_dir",
        lambda _name: tmp_path,
    )


@pytest.fixture(autouse=True)
def fresh_simulator_enumeration():
    clear_ios_simulator_cache()
    yield
    clear_ios_simulator_cache()


SIMCTL_PAYLOAD = {
    "devices": {
        "com.apple.CoreSimulator.SimRuntime.iOS-26-0": [
            {
                "udid": "AAAA-1111",
                "name": "iPhone 17 Pro",
                "state": "Booted",
                "isAvailable": True,
            },
            {
                "udid": "BBBB-2222",
                "name": "iPhone 17",
                "state": "Shutdown",
                "isAvailable": True,
            },
            {
                "udid": "CCCC-3333",
                "name": "iPad Unavailable",
                "state": "Shutdown",
                "isAvailable": False,
            },
        ],
        "com.apple.CoreSimulator.SimRuntime.watchOS-26-0": [
            {
                "udid": "DDDD-4444",
                "name": "Watch",
                "state": "Shutdown",
                "isAvailable": True,
            }
        ],
        "com.apple.CoreSimulator.SimRuntime.iOS-26-1": [
            {
                "udid": "",
                "name": "No UDID",
                "state": "Shutdown",
                "isAvailable": True,
            }
        ],
    }
}

SIM_LIST = parse_simctl_devices(SIMCTL_PAYLOAD)


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #


def test_parse_simctl_devices_filters_ios_available_only():
    devices = parse_simctl_devices(SIMCTL_PAYLOAD)
    udids = {d["udid"] for d in devices}
    assert udids == {"AAAA-1111", "BBBB-2222"}
    by_udid = {d["udid"]: d for d in devices}
    assert by_udid["AAAA-1111"]["state"] == "Booted"
    assert by_udid["AAAA-1111"]["name"] == "iPhone 17 Pro"
    assert "iOS-26-0" in by_udid["BBBB-2222"]["runtime"]


def test_parse_simctl_devices_empty_payload():
    assert parse_simctl_devices({}) == []
    assert parse_simctl_devices({"devices": {}}) == []


@pytest.mark.asyncio
async def test_list_ios_simulators_returns_none_on_failure(monkeypatch):
    monkeypatch.setattr(
        "artemis.drivers.ios.discovery.run_xcrun",
        AsyncMock(side_effect=RuntimeError("no xcode")),
    )
    assert await list_ios_simulators() is None


@pytest.mark.asyncio
async def test_list_ios_simulators_parses_json(monkeypatch):
    import json

    monkeypatch.setattr(
        "artemis.drivers.ios.discovery.run_xcrun",
        AsyncMock(return_value=json.dumps(SIMCTL_PAYLOAD).encode()),
    )
    monkeypatch.setattr("artemis.drivers.ios.discovery.simctl_available", lambda: True)
    devices = await list_ios_simulators()
    assert {d["udid"] for d in devices} == {"AAAA-1111", "BBBB-2222"}


def test_list_ios_simulators_sync_skips_without_simctl(monkeypatch):
    monkeypatch.setattr("artemis.drivers.ios.discovery.simctl_available", lambda: False)
    assert list_ios_simulators_sync() is None


def test_list_ios_simulators_sync_parses_json(monkeypatch):
    import json
    import subprocess

    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=json.dumps(SIMCTL_PAYLOAD).encode()
    )
    monkeypatch.setattr("artemis.drivers.ios.discovery.simctl_available", lambda: True)
    monkeypatch.setattr("artemis.drivers.ios.discovery.subprocess.run", lambda *a, **k: completed)
    devices = list_ios_simulators_sync()
    assert {d["udid"] for d in devices} == {"AAAA-1111", "BBBB-2222"}


@pytest.mark.asyncio
async def test_list_ios_simulators_reuses_cached_enumeration(monkeypatch):
    import json

    run_xcrun = AsyncMock(return_value=json.dumps(SIMCTL_PAYLOAD).encode())
    monkeypatch.setattr("artemis.drivers.ios.discovery.run_xcrun", run_xcrun)
    monkeypatch.setattr("artemis.drivers.ios.discovery.simctl_available", lambda: True)
    assert await list_ios_simulators() is not None
    assert await list_ios_simulators() is not None
    # Second call inside the TTL serves the cache — no second simctl spawn.
    assert run_xcrun.await_count == 1


@pytest.mark.asyncio
async def test_list_ios_simulators_does_not_cache_failures(monkeypatch):
    import json

    run_xcrun = AsyncMock(
        side_effect=[RuntimeError("cold simctl"), json.dumps(SIMCTL_PAYLOAD).encode()]
    )
    monkeypatch.setattr("artemis.drivers.ios.discovery.run_xcrun", run_xcrun)
    monkeypatch.setattr("artemis.drivers.ios.discovery.simctl_available", lambda: True)
    assert await list_ios_simulators() is None
    assert await list_ios_simulators() is not None
    assert run_xcrun.await_count == 2


@pytest.mark.asyncio
async def test_list_ios_simulators_force_refresh_bypasses_cache(monkeypatch):
    import json

    run_xcrun = AsyncMock(return_value=json.dumps(SIMCTL_PAYLOAD).encode())
    monkeypatch.setattr("artemis.drivers.ios.discovery.run_xcrun", run_xcrun)
    monkeypatch.setattr("artemis.drivers.ios.discovery.simctl_available", lambda: True)
    await list_ios_simulators()
    await list_ios_simulators(force_refresh=True)
    assert run_xcrun.await_count == 2


def test_list_ios_simulators_sync_shares_cache(monkeypatch):
    import json
    import subprocess

    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=json.dumps(SIMCTL_PAYLOAD).encode()
    )
    calls = []
    monkeypatch.setattr("artemis.drivers.ios.discovery.simctl_available", lambda: True)
    monkeypatch.setattr(
        "artemis.drivers.ios.discovery.subprocess.run",
        lambda *a, **k: calls.append(1) or completed,
    )
    assert list_ios_simulators_sync() is not None
    assert list_ios_simulators_sync() is not None
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# IosTarget / lock scope
# --------------------------------------------------------------------------- #


def test_ios_target_lock_scope_and_key():
    target = IosTarget(serial="AAAA-1111")
    assert target.platform == "ios"
    assert target.lock_scope == IOS_LOCK_SCOPE
    assert target.lock_key == f"{IOS_LOCK_SCOPE}/AAAA-1111"
    assert target.to_dict() == {"platform": "ios", "serial": "AAAA-1111"}


def test_ios_target_apply_to_environment_sets_scope_and_scrubs_adb(monkeypatch):
    env = {"ADB_DEVICE_SERIAL": "emulator-5554", "ADB_HOST": "10.0.0.1"}
    IosTarget(serial="AAAA-1111").apply_to_environment(env)
    assert env[DeviceExecutionLock.LOCK_SCOPE_ENV] == IOS_LOCK_SCOPE
    # iOS workers never touch ADB: a stale serial must not leak in.
    assert "ADB_DEVICE_SERIAL" not in env


def test_ios_and_android_locks_do_not_collide():
    """A UDID and an ADB serial with identical text lock independently."""
    shared_id = "emulator-5554"
    ios_lock = DeviceExecutionLock(shared_id, "ios task", lock_scope=IOS_LOCK_SCOPE)
    android_lock = DeviceExecutionLock(shared_id, "android task")
    assert ios_lock.clean_device_id != android_lock.clean_device_id

    ios_lock.acquire()
    try:
        owners = list(DeviceExecutionLock.get_active_owners().values())
        ios_owner = next(o for o in owners if o.device_id == shared_id)
        assert ios_owner.lock_scope == IOS_LOCK_SCOPE
        # Android lock for the same text is unaffected and acquirable.
        android_lock.acquire(blocking=False)
        try:
            owners = list(DeviceExecutionLock.get_active_owners().values())
            by_scope = {o.lock_scope: o for o in owners if o.device_id == shared_id}
            assert IOS_LOCK_SCOPE in by_scope
            assert len(by_scope) == 2
        finally:
            android_lock.release()
    finally:
        ios_lock.release()


def test_get_active_owner_scoped_lookup_isolates_platform(monkeypatch):
    monkeypatch.delenv(DeviceExecutionLock.LOCK_SCOPE_ENV, raising=False)
    shared_id = "emulator-5554"
    ios_lock = DeviceExecutionLock(shared_id, "ios task", lock_scope=IOS_LOCK_SCOPE)
    ios_lock.acquire()
    try:
        owner = DeviceExecutionLock.get_active_owner(shared_id, lock_scope=IOS_LOCK_SCOPE)
        assert owner is not None and owner.description == "ios task"
        # An unscoped (Android) lookup must not see the iOS owner.
        assert DeviceExecutionLock.get_active_owner(shared_id, lock_scope=None) is None
    finally:
        ios_lock.release()


# --------------------------------------------------------------------------- #
# IosDevicePool
# --------------------------------------------------------------------------- #


def _pool_with_devices(monkeypatch, devices=None):
    import importlib

    pool = IosDevicePool()
    devices = SIM_LIST if devices is None else devices
    # artemis.runtime.ios_device_pool resolves to the singleton instance in the
    # package namespace; patch the module object itself.
    module = importlib.import_module("artemis.runtime.ios_device_pool")
    monkeypatch.setattr(module, "list_ios_simulators", AsyncMock(return_value=list(devices)))
    monkeypatch.setattr(module, "list_ios_simulators_sync", lambda: list(devices))
    monkeypatch.setattr(module, "list_core_devices", AsyncMock(return_value=[]))
    monkeypatch.setattr(module, "list_core_devices_sync", lambda: [])
    return pool


@pytest.mark.asyncio
async def test_ios_pool_statuses_carry_platform_and_runtime(monkeypatch):
    pool = _pool_with_devices(monkeypatch)
    devices = await pool.list_devices_async()
    by_serial = {d.serial: d for d in devices}
    booted = by_serial["AAAA-1111"]
    assert booted.platform == "ios"
    assert booted.state == "device"  # Booted maps to the generic ready state
    assert booted.is_emulator is True
    assert "iOS 26 0" in booted.product
    shutdown = by_serial["BBBB-2222"]
    assert shutdown.state == "Shutdown"


@pytest.mark.asyncio
async def test_ios_pool_busy_status_uses_ios_lock_scope(monkeypatch):
    pool = _pool_with_devices(monkeypatch)
    lock = DeviceExecutionLock("AAAA-1111", "ios task", lock_scope=IOS_LOCK_SCOPE)
    lock.acquire()
    try:
        devices = await pool.list_devices_async()
        by_serial = {d.serial: d for d in devices}
        assert by_serial["AAAA-1111"].is_busy is True
        assert by_serial["AAAA-1111"].active_task_desc == "ios task"
        assert by_serial["BBBB-2222"].is_busy is False
    finally:
        lock.release()


@pytest.mark.asyncio
async def test_ios_pool_android_lock_does_not_mark_ios_busy(monkeypatch):
    """An Android lock on the same text must not mark the simulator busy."""
    pool = _pool_with_devices(monkeypatch)
    lock = DeviceExecutionLock("AAAA-1111", "android task")
    lock.acquire()
    try:
        devices = await pool.list_devices_async()
        by_serial = {d.serial: d for d in devices}
        assert by_serial["AAAA-1111"].is_busy is False
    finally:
        lock.release()


@pytest.mark.asyncio
async def test_ios_pool_validate_explicit_serial(monkeypatch):
    pool = _pool_with_devices(monkeypatch)
    assert await pool.validate_explicit_serial_async("AAAA-1111") is None
    assert await pool.validate_explicit_serial_async("bbbb-2222") is None
    rejection = await pool.validate_explicit_serial_async("NOPE-9999")
    assert rejection is not None and "not available" in rejection
    # Shutdown sims are valid; unavailable devices were filtered at parse time.
    assert await pool.validate_explicit_serial_async("CCCC-3333") is not None


@pytest.mark.asyncio
async def test_ios_pool_validate_fails_open_on_enumeration_error(monkeypatch):
    pool = IosDevicePool()
    import importlib

    module = importlib.import_module("artemis.runtime.ios_device_pool")
    monkeypatch.setattr(module, "list_ios_simulators", AsyncMock(return_value=None))
    monkeypatch.setattr(module, "list_core_devices", AsyncMock(return_value=None))
    assert await pool.validate_explicit_serial_async("ANY") is None


def test_ios_pool_validate_explicit_serial_sync(monkeypatch):
    pool = _pool_with_devices(monkeypatch)
    assert pool.validate_explicit_serial("AAAA-1111") is None
    rejection = pool.validate_explicit_serial("NOPE-9999")
    assert rejection is not None and "not available" in rejection


@pytest.mark.asyncio
async def test_ios_pool_select_device_prefers_booted_and_idle(monkeypatch):
    pool = _pool_with_devices(monkeypatch)
    assert await pool.select_device_async() == "AAAA-1111"
    # Explicit preference always wins.
    assert await pool.select_device_async(preferred_serial="BBBB-2222") == "BBBB-2222"


@pytest.mark.asyncio
async def test_ios_pool_select_device_never_picks_physical(monkeypatch):
    """Auto-selection must never target paired hardware without an explicit serial."""
    import importlib

    physical = {
        "udid": "00008130-0000ABCD1234FFFF",
        "name": "Test iPhone",
        "reality": "physical",
        "platform": "iOS",
        "pairing_state": "paired",
        "connection_state": "connected",
        "os_version": "26.0",
    }
    module = importlib.import_module("artemis.runtime.ios_device_pool")
    pool = IosDevicePool()

    # Physical alone: nothing to auto-pick.
    monkeypatch.setattr(module, "list_ios_simulators", AsyncMock(return_value=[]))
    monkeypatch.setattr(module, "list_core_devices", AsyncMock(return_value=[physical]))
    assert await pool.select_device_async() is None

    # Physical alongside one booted sim: only the sim is eligible.
    monkeypatch.setattr(module, "list_ios_simulators", AsyncMock(return_value=list(SIM_LIST)))
    assert await pool.select_device_async() == "AAAA-1111"

    # Explicit serial still passes through untouched.
    assert await pool.select_device_async(preferred_serial=physical["udid"]) == physical["udid"]


@pytest.mark.asyncio
async def test_ios_pool_validate_fails_open_on_partial_enumeration(monkeypatch):
    """A dead devicectl enumeration cannot disprove a physical serial."""
    import importlib

    module = importlib.import_module("artemis.runtime.ios_device_pool")
    pool = IosDevicePool()
    monkeypatch.setattr(module, "list_ios_simulators", AsyncMock(return_value=list(SIM_LIST)))
    monkeypatch.setattr(module, "list_core_devices", AsyncMock(return_value=None))
    assert await pool.validate_explicit_serial_async("00008130-0000ABCD1234FFFF") is None


@pytest.mark.asyncio
async def test_ios_pool_select_device_boots_single_shutdown_sim(monkeypatch):
    pool = _pool_with_devices(monkeypatch, [d for d in SIM_LIST if d["udid"] == "BBBB-2222"])
    assert await pool.select_device_async() == "BBBB-2222"


@pytest.mark.asyncio
async def test_ios_pool_select_device_skips_busy_booted(monkeypatch):
    two_booted = [
        {**SIM_LIST[0]},
        {**SIM_LIST[1], "state": "Booted"},
    ]
    pool = _pool_with_devices(monkeypatch, two_booted)
    lock = DeviceExecutionLock("AAAA-1111", "ios task", lock_scope=IOS_LOCK_SCOPE)
    lock.acquire()
    try:
        assert await pool.select_device_async() == "BBBB-2222"
    finally:
        lock.release()


# --------------------------------------------------------------------------- #
# Task queue routing
# --------------------------------------------------------------------------- #


def test_task_target_routes_ios_to_ios_target():
    from apps.admin_console.services.task_queue_service import TaskQueueService

    target = TaskQueueService._task_target({"platform": "ios", "device_serial": "AAAA-1111"})
    assert isinstance(target, IosTarget)
    assert target.serial == "AAAA-1111"


def test_task_target_defaults_to_android_adb_target():
    from apps.admin_console.services.task_queue_service import TaskQueueService

    target = TaskQueueService._task_target({"device_serial": "emulator-5554"})
    assert isinstance(target, AdbTarget)
    assert target.serial == "emulator-5554"


def test_build_worker_invocation_ios(monkeypatch):
    from apps.admin_console.services.task_queue_service import TaskQueueService

    monkeypatch.delenv("ADB_DEVICE_SERIAL", raising=False)
    cmd, env = TaskQueueService._build_worker_invocation(
        {
            "platform": "ios",
            "device_serial": "AAAA-1111",
            "ios_workspace": "/proj/App.xcodeproj",
        },
        run_key="rk",
        sess_id="sess-1",
        goal="open settings",
        profile="flash",
        target=IosTarget(serial="AAAA-1111"),
    )
    assert "--platform" in cmd
    assert cmd[cmd.index("--platform") + 1] == "ios"
    assert cmd[cmd.index("--device-serial") + 1] == "AAAA-1111"
    assert cmd[cmd.index("--ios-workspace") + 1] == "/proj/App.xcodeproj"
    assert env[DeviceExecutionLock.LOCK_SCOPE_ENV] == IOS_LOCK_SCOPE
    assert "ADB_DEVICE_SERIAL" not in env


def test_build_worker_invocation_android_unchanged():
    from apps.admin_console.services.task_queue_service import TaskQueueService

    endpoint = AdbEndpoint.local()
    target = AdbTarget(endpoint=endpoint, serial="emulator-5554")
    cmd, env = TaskQueueService._build_worker_invocation(
        {"device_serial": "emulator-5554"},
        run_key="rk",
        sess_id="sess-1",
        goal="open settings",
        profile="flash",
        target=target,
    )
    assert "--platform" not in cmd
    assert cmd[cmd.index("--device-serial") + 1] == "emulator-5554"
    assert env["ADB_DEVICE_SERIAL"] == "emulator-5554"
    assert env[DeviceExecutionLock.LOCK_SCOPE_ENV] == endpoint.identity


@pytest.mark.asyncio
async def test_enqueue_tasks_ios_carries_platform_and_lock_scope(monkeypatch):
    from apps.admin_console.services.task_queue_service import TaskQueueService
    from apps.admin_console.core.state import state

    monkeypatch.setattr(TaskQueueService, "ensure_worker_running", staticmethod(lambda: None))
    monkeypatch.setattr(
        TaskQueueService,
        "_find_duplicate_submission",
        classmethod(lambda cls, *a, **k: None),
    )
    monkeypatch.setattr(
        IosDevicePool,
        "validate_explicit_serial_async",
        AsyncMock(return_value=None),
    )
    state.queue_items.clear()
    try:
        resp = await TaskQueueService.enqueue_tasks(
            ["open settings"],
            "flash",
            None,
            None,
            None,
            None,
            "AAAA-1111",
            "frontend",
            session_id="sess-1",
            conversation_id=None,
            platform="ios",
            ios_workspace="/proj/App.xcodeproj",
        )
        item = state.queue_items[-1]
        assert item["platform"] == "ios"
        assert item["ios_workspace"] == "/proj/App.xcodeproj"
        assert item["device_serial"] == "AAAA-1111"
        # The queue reservation was scoped ios — check via the target builder.
        target = TaskQueueService._task_target(item)
        assert target.lock_scope == IOS_LOCK_SCOPE
        assert not resp.get("rejected")
    finally:
        state.queue_items.clear()


# --------------------------------------------------------------------------- #
# Stream service target resolution
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_stream_target_prefers_active_ios_owner(monkeypatch):
    from apps.admin_console.services.device_stream_service import DeviceStreamService

    owner = DeviceLockOwner(
        pid=os.getpid(),
        process_created_at=0.0,
        token="t",
        device_id="AAAA-1111",
        description="ios task",
        acquired_at="now",
        lock_scope=IOS_LOCK_SCOPE,
    )
    monkeypatch.setattr(
        DeviceExecutionLock,
        "get_active_owners",
        classmethod(lambda cls: {"ios__AAAA-1111": owner}),
    )
    service = DeviceStreamService()
    target = await service.get_stream_target()
    assert target == {"platform": "ios", "serial": "AAAA-1111"}


@pytest.mark.asyncio
async def test_stream_target_falls_back_to_android(monkeypatch):
    from apps.admin_console.services.device_stream_service import DeviceStreamService

    monkeypatch.setattr(DeviceExecutionLock, "get_active_owners", classmethod(lambda cls: {}))
    service = DeviceStreamService()
    monkeypatch.setattr(service, "_android_serial", AsyncMock(return_value="emulator-5554"))
    monkeypatch.setattr(IosDevicePool, "select_device_async", AsyncMock(return_value=None))
    target = await service.get_stream_target()
    assert target == {"platform": "android", "serial": "emulator-5554"}


@pytest.mark.asyncio
async def test_stream_target_falls_back_to_booted_sim(monkeypatch):
    from apps.admin_console.services.device_stream_service import DeviceStreamService

    monkeypatch.setattr(DeviceExecutionLock, "get_active_owners", classmethod(lambda cls: {}))
    service = DeviceStreamService()
    monkeypatch.setattr(service, "_android_serial", AsyncMock(return_value=None))
    monkeypatch.setattr(IosDevicePool, "select_device_async", AsyncMock(return_value="AAAA-1111"))
    target = await service.get_stream_target()
    assert target == {"platform": "ios", "serial": "AAAA-1111"}


@pytest.mark.asyncio
async def test_stream_target_none_without_devices(monkeypatch):
    from apps.admin_console.services.device_stream_service import DeviceStreamService

    monkeypatch.setattr(DeviceExecutionLock, "get_active_owners", classmethod(lambda cls: {}))
    service = DeviceStreamService()
    monkeypatch.setattr(service, "_android_serial", AsyncMock(return_value=None))
    monkeypatch.setattr(IosDevicePool, "select_device_async", AsyncMock(return_value=None))
    assert await service.get_stream_target() is None


# --------------------------------------------------------------------------- #
# MCP task runner platform routing
# --------------------------------------------------------------------------- #


def test_mcp_validate_device_serial_ios_uses_ios_pool(monkeypatch):
    from artemis.runtime import device_pool as adb_pool
    from artemis.runtime.ios_device_pool import ios_device_pool
    from mcp_server.tools import task_runner

    ios_validate = MagicMock(return_value=None)
    adb_validate = MagicMock(return_value="should not be called")
    monkeypatch.setattr(ios_device_pool, "validate_explicit_serial", ios_validate)
    monkeypatch.setattr(adb_pool, "validate_explicit_serial", adb_validate)
    assert task_runner._validate_device_serial("AAAA-1111", "ios") is None
    ios_validate.assert_called_once_with("AAAA-1111")
    adb_validate.assert_not_called()


def test_mcp_validate_device_serial_ios_rejection(monkeypatch):
    from artemis.runtime.ios_device_pool import ios_device_pool
    from mcp_server.tools import task_runner

    monkeypatch.setattr(
        ios_device_pool,
        "validate_explicit_serial",
        lambda serial: f"iOS simulator '{serial}' is not available.",
    )
    result = task_runner._validate_device_serial("NOPE", "ios")
    assert result is not None and result["status"] == "failed"
    assert "simctl" in result["message"]


# --------------------------------------------------------------------------- #
# Daemon client payloads
# --------------------------------------------------------------------------- #


def test_submit_task_to_daemon_forwards_platform(monkeypatch):
    from artemis.runtime import daemon_client

    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"status": "enqueued"}'

    def fake_urlopen(req, timeout=0):
        import json

        captured.update(json.loads(req.data.decode()))
        return FakeResponse()

    monkeypatch.setattr("artemis.runtime.daemon_client.urllib.request.urlopen", fake_urlopen)
    resp = daemon_client.submit_task_to_daemon(
        "open settings",
        device_serial="AAAA-1111",
        platform="ios",
        ios_workspace="/proj/App.xcodeproj",
    )
    assert resp == {"status": "enqueued"}
    assert captured["platform"] == "ios"
    assert captured["ios_workspace"] == "/proj/App.xcodeproj"
    assert captured["device_serial"] == "AAAA-1111"


def test_submit_batch_to_daemon_forwards_platform(monkeypatch):
    from artemis.runtime import daemon_client

    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"status": "enqueued"}'

    def fake_urlopen(req, timeout=0):
        import json

        captured.update(json.loads(req.data.decode()))
        return FakeResponse()

    monkeypatch.setattr("artemis.runtime.daemon_client.urllib.request.urlopen", fake_urlopen)
    daemon_client.submit_batch_to_daemon(
        ["a", "b"],
        device_serial="AAAA-1111",
        platform="ios",
        ios_workspace="/proj/App.xcodeproj",
    )
    assert captured["platform"] == "ios"
    assert captured["ios_workspace"] == "/proj/App.xcodeproj"


# --------------------------------------------------------------------------- #
# Replay context platform preservation
# --------------------------------------------------------------------------- #


def test_replay_device_context_preserves_ios_platform():
    from apps.admin_console.replay_manager import ReplayManager
    from artemis.context import DevicePlatform

    ctx = ReplayManager._replay_device_context(
        {
            "mobile_platform": "ios",
            "device_id": "AAAA-1111",
            "device_width": 1206,
            "device_height": 2622,
        },
        override_device_id="BBBB-2222",
    )
    assert ctx.mobile_platform == DevicePlatform.IOS
    assert ctx.device_id == "BBBB-2222"
    assert ctx.device_width == 1206


def test_replay_device_context_android_ignores_override():
    from apps.admin_console.replay_manager import ReplayManager
    from artemis.context import DevicePlatform

    ctx = ReplayManager._replay_device_context(
        {"mobile_platform": "android", "device_id": "emulator-5554"},
        override_device_id="AAAA-1111",
    )
    assert ctx.mobile_platform == DevicePlatform.ANDROID
    # Android replay keeps the recorded device; the picker stays decorative.
    assert ctx.device_id == "emulator-5554"


def test_replay_device_context_missing_platform_defaults_android():
    from apps.admin_console.replay_manager import ReplayManager
    from artemis.context import DevicePlatform

    ctx = ReplayManager._replay_device_context({"device_id": "emulator-5554"}, None)
    assert ctx.mobile_platform == DevicePlatform.ANDROID
    assert ctx.device_id == "emulator-5554"


# --------------------------------------------------------------------------- #
# iOS readiness probe
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_ios_probe_skipped_without_simctl(monkeypatch):
    from artemis.core.diagnostics.probes.ios_probe import IosSimulatorProbe
    from artemis.core.diagnostics.schema import ProbeStatus

    monkeypatch.setattr("artemis.core.diagnostics.probes.ios_probe.simctl_available", lambda: False)
    result = await IosSimulatorProbe().probe()
    assert result.status is ProbeStatus.SKIPPED
    assert result.is_blocker is False


@pytest.mark.asyncio
async def test_ios_probe_warns_on_old_xcode(monkeypatch):
    from artemis.core.diagnostics.probes.ios_probe import IosSimulatorProbe
    from artemis.core.diagnostics.schema import ProbeStatus

    monkeypatch.setattr("artemis.core.diagnostics.probes.ios_probe.simctl_available", lambda: True)
    monkeypatch.setattr(
        "artemis.core.diagnostics.probes.ios_probe.run_xcrun",
        AsyncMock(return_value=b"Xcode 16.4\nBuild version 16F6"),
    )
    monkeypatch.setattr(
        "artemis.core.diagnostics.probes.ios_probe.list_ios_simulators",
        AsyncMock(return_value=SIM_LIST),
    )
    monkeypatch.setattr(
        "artemis.core.diagnostics.probes.ios_probe.list_core_devices",
        AsyncMock(return_value=[]),
    )
    result = await IosSimulatorProbe().probe()
    assert result.status is ProbeStatus.WARN
    assert result.metadata["xcode_27_or_newer"] is False


@pytest.mark.asyncio
async def test_ios_probe_passes_with_xcode27_and_sims(monkeypatch):
    from artemis.core.diagnostics.probes.ios_probe import IosSimulatorProbe
    from artemis.core.diagnostics.schema import ProbeStatus

    monkeypatch.setattr("artemis.core.diagnostics.probes.ios_probe.simctl_available", lambda: True)
    monkeypatch.setattr(
        "artemis.core.diagnostics.probes.ios_probe.run_xcrun",
        AsyncMock(return_value=b"Xcode 27.0\nBuild version 27A266a"),
    )
    monkeypatch.setattr(
        "artemis.core.diagnostics.probes.ios_probe.list_ios_simulators",
        AsyncMock(return_value=SIM_LIST),
    )
    monkeypatch.setattr(
        "artemis.core.diagnostics.probes.ios_probe.list_core_devices",
        AsyncMock(return_value=[]),
    )
    result = await IosSimulatorProbe().probe()
    assert result.status is ProbeStatus.PASS
    assert result.metadata["simulator_count"] == 2
    assert result.metadata["booted_udids"] == ["AAAA-1111"]
    assert result.is_blocker is False


@pytest.mark.asyncio
async def test_ios_probe_warns_without_simulators(monkeypatch):
    from artemis.core.diagnostics.probes.ios_probe import IosSimulatorProbe
    from artemis.core.diagnostics.schema import ProbeStatus

    monkeypatch.setattr("artemis.core.diagnostics.probes.ios_probe.simctl_available", lambda: True)
    monkeypatch.setattr(
        "artemis.core.diagnostics.probes.ios_probe.run_xcrun",
        AsyncMock(return_value=b"Xcode 27.0"),
    )
    monkeypatch.setattr(
        "artemis.core.diagnostics.probes.ios_probe.list_ios_simulators",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "artemis.core.diagnostics.probes.ios_probe.list_core_devices",
        AsyncMock(return_value=[]),
    )
    result = await IosSimulatorProbe().probe()
    assert result.status is ProbeStatus.WARN
    assert "No Devices" in result.summary


def test_ios_probe_registered_in_engine():
    from artemis.core.diagnostics.engine import readiness_engine

    assert "ios_simulators" in readiness_engine._probes
    assert readiness_engine._probes["ios_simulators"].is_blocker is False


# --------------------------------------------------------------------------- #
# web: platform-aware device selection
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_select_device_ios_validates_udid_and_skips_adb_target(monkeypatch):
    from unittest.mock import AsyncMock

    from apps.admin_console.routers import system as system_router
    from apps.admin_console.routers.system import SelectDeviceRequest

    validate = AsyncMock(return_value=None)
    monkeypatch.setattr(IosDevicePool, "validate_explicit_serial_async", validate)
    set_target = Mock()
    monkeypatch.setattr(system_router.readiness_engine, "set_probe_target_serial", set_target)

    result = await system_router.select_active_device(
        SelectDeviceRequest(serial="AAAA-1111", platform="ios")
    )

    assert result["status"] == "success"
    assert result["selected_serial"] == "AAAA-1111"
    assert result["platform"] == "ios"
    validate.assert_awaited_once_with("AAAA-1111")
    set_target.assert_not_called()


@pytest.mark.asyncio
async def test_select_device_ios_rejects_unknown_udid(monkeypatch):
    from unittest.mock import AsyncMock

    from fastapi import HTTPException

    from apps.admin_console.routers.system import SelectDeviceRequest, select_active_device

    monkeypatch.setattr(
        IosDevicePool,
        "validate_explicit_serial_async",
        AsyncMock(return_value="iOS Simulator UDID 'NOPE' was not found."),
    )

    with pytest.raises(HTTPException) as exc_info:
        await select_active_device(SelectDeviceRequest(serial="NOPE", platform="ios"))
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_select_device_rejects_unknown_platform():
    from fastapi import HTTPException

    from apps.admin_console.routers.system import SelectDeviceRequest, select_active_device

    with pytest.raises(HTTPException) as exc_info:
        await select_active_device(SelectDeviceRequest(serial="dev-1", platform="tvos"))
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_select_device_android_unchanged(monkeypatch):
    from unittest.mock import AsyncMock

    from apps.admin_console.routers import system as system_router
    from apps.admin_console.routers.system import SelectDeviceRequest

    set_target = Mock()
    report = object()
    monkeypatch.setattr(system_router.readiness_engine, "set_probe_target_serial", set_target)
    monkeypatch.setattr(system_router.readiness_engine, "run_all", AsyncMock(return_value=report))

    result = await system_router.select_active_device(SelectDeviceRequest(serial="emulator-5554"))

    assert result["status"] == "success"
    assert result["selected_serial"] == "emulator-5554"
    set_target.assert_called_once_with("emulator-5554")


def test_ios_press_key_vocabulary_passes_the_operator_gate():
    """Every key the iOS guidance advertises must survive the Operator key gate."""
    from artemis.agents.operator.operator import SUPPORTED_PRESS_KEYS

    ios_supported = {"enter", "home", "power", "volume_up", "volume_down", "app_switch"}
    assert ios_supported <= {key.lower() for key in SUPPORTED_PRESS_KEYS}


# --------------------------------------------------------------------------- #
# lazy MCP controller selection
# --------------------------------------------------------------------------- #


def test_lazy_mcp_ios_controller_uses_ios_context_and_isolated_cache(monkeypatch):
    """The lazy MCP path builds an iOS DeviceContext without a native session."""
    from types import SimpleNamespace

    from artemis.context import DevicePlatform
    from artemis.mcp import adb_server

    captured = {}

    def fake_controller(ctx):
        captured["ctx"] = ctx
        return SimpleNamespace(ctx=ctx)

    monkeypatch.setattr(adb_server, "_CONTROLLERS", {})
    monkeypatch.setattr(adb_server, "_GLOBAL_CONTROLLER", None)
    monkeypatch.setattr(adb_server, "UnifiedMobileController", fake_controller)
    monkeypatch.delenv("ARTEMIS_DEVICE_ID", raising=False)
    monkeypatch.delenv("ADB_DEVICE_SERIAL", raising=False)

    controller = adb_server._get_controller(target_platform="ios")
    assert captured["ctx"].device.mobile_platform == DevicePlatform.IOS
    assert captured["ctx"].device.device_id == "booted"
    assert adb_server._CONTROLLERS == {"ios:booted": controller}

    again = adb_server._get_controller(target_platform="ios")
    assert again is controller

    # iOS lookups never populate the Android global slot or un-namespaced keys.
    assert adb_server._GLOBAL_CONTROLLER is None
    assert all(key.startswith("ios:") for key in adb_server._CONTROLLERS)


def test_lazy_mcp_ios_controller_never_reads_adb_device_serial(monkeypatch):
    """ADB_DEVICE_SERIAL is Android-only: it must not seed an iOS target."""
    from types import SimpleNamespace

    from artemis.context import DevicePlatform
    from artemis.mcp import adb_server

    captured = {}

    def fake_controller(ctx):
        captured["ctx"] = ctx
        return SimpleNamespace(ctx=ctx)

    monkeypatch.setattr(adb_server, "_CONTROLLERS", {})
    monkeypatch.setattr(adb_server, "_GLOBAL_CONTROLLER", None)
    monkeypatch.setattr(adb_server, "UnifiedMobileController", fake_controller)
    monkeypatch.delenv("ARTEMIS_DEVICE_ID", raising=False)
    monkeypatch.setenv("ADB_DEVICE_SERIAL", "android-only")

    controller = adb_server._get_controller(target_platform="ios")
    assert captured["ctx"].device.mobile_platform == DevicePlatform.IOS
    assert captured["ctx"].device.device_id == "booted"
    assert set(adb_server._CONTROLLERS) == {"ios:booted"}
    assert adb_server._GLOBAL_CONTROLLER is None


# --------------------------------------------------------------------------- #
# iOS pool: "booted" selector and name ambiguity
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_ios_pool_booted_selector_pins_the_unique_booted_sim(monkeypatch):
    pool = _pool_with_devices(monkeypatch)
    assert await pool.select_device_async(preferred_serial="booted") == "AAAA-1111"
    assert await pool.validate_explicit_serial_async("booted") is None
    assert pool.validate_explicit_serial("booted") is None


@pytest.mark.asyncio
async def test_ios_pool_booted_selector_tolerates_case_and_whitespace(monkeypatch):
    """' Booted ' / 'BOOTED' normalize to the reserved selector, not a name."""
    pool = _pool_with_devices(monkeypatch)
    assert await pool.select_device_async(preferred_serial=" Booted ") == "AAAA-1111"
    assert await pool.validate_explicit_serial_async(" BOOTED ") is None
    assert pool.validate_explicit_serial("Booted") is None


@pytest.mark.asyncio
async def test_ios_pool_booted_selector_fails_open_on_ambiguity(monkeypatch):
    """Zero or multiple booted sims keep the literal so the driver's guidance fires."""
    two_booted = [dict(SIM_LIST[0]), {**SIM_LIST[1], "state": "Booted"}]
    pool = _pool_with_devices(monkeypatch, two_booted)
    assert await pool.select_device_async(preferred_serial="booted") == "booted"
    rejection = await pool.validate_explicit_serial_async("booted")
    assert rejection is not None and "exactly one booted" in rejection
    assert "UDID" in rejection

    no_booted = [{**SIM_LIST[0], "state": "Shutdown"}, dict(SIM_LIST[1])]
    pool = _pool_with_devices(monkeypatch, no_booted)
    assert await pool.select_device_async(preferred_serial="booted") == "booted"
    rejection = await pool.validate_explicit_serial_async("booted")
    assert rejection is not None and "0 found" in rejection


@pytest.mark.asyncio
async def test_ios_pool_booted_selector_fails_open_on_enumeration_error(monkeypatch):
    pool = IosDevicePool()
    import importlib

    module = importlib.import_module("artemis.runtime.ios_device_pool")
    monkeypatch.setattr(module, "list_ios_simulators", AsyncMock(return_value=None))
    monkeypatch.setattr(module, "list_core_devices", AsyncMock(return_value=None))
    assert await pool.select_device_async(preferred_serial="booted") == "booted"
    assert await pool.validate_explicit_serial_async("booted") is None


def test_ios_pool_rejects_duplicate_device_names(monkeypatch):
    """Two usable devices sharing a name must be rejected, not first-matched."""
    twins = [
        {**SIM_LIST[0], "name": "Office iPhone"},
        {**SIM_LIST[1], "name": "Office iPhone", "state": "Booted"},
    ]
    pool = _pool_with_devices(monkeypatch, twins)
    rejection = pool.validate_explicit_serial("Office iPhone")
    assert rejection is not None and "UDID" in rejection
    # UDID pins resolve fine.
    assert pool.validate_explicit_serial("AAAA-1111") is None


# --------------------------------------------------------------------------- #
# Admin capabilities endpoint (thin-SDK iOS wire contract)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_admin_capabilities_endpoint_is_static_and_advertises_ios(monkeypatch):
    """GET /api/v1/capabilities advertises 'platform.ios' without probing."""
    import importlib

    tasks_router = importlib.import_module("apps.admin_console.routers.tasks")
    pool_module = importlib.import_module("artemis.runtime.ios_device_pool")
    monkeypatch.setattr(
        pool_module,
        "list_core_devices",
        AsyncMock(side_effect=AssertionError("device discovery must not run")),
    )
    monkeypatch.setattr(
        pool_module,
        "list_ios_simulators",
        AsyncMock(side_effect=AssertionError("device discovery must not run")),
    )

    response = await tasks_router.get_capabilities()

    assert response["api_version"] == "1"
    assert "platform.ios" in response["features"]
    assert {
        "tasks.submit",
        "tasks.get",
        "tasks.stop",
        "devices.list",
        "system.readiness",
    }.issubset(response["features"])
