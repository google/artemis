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

"""Unit tests for the end-to-end device observation smoke test (no real device)."""

import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from artemis.core.diagnostics import smoke_test_device
from artemis.core.diagnostics import device_smoke
from artemis.runtime.device_lock import DeviceExecutionLock, DeviceLockOwner

SERIAL = "59100DLCR0033X"
FAKE_JPEG = b"\xff\xd8\xff" + bytes(range(256)) * 8 + b"\xff\xd9"


@pytest.fixture(autouse=True)
def idle_devices(monkeypatch):
    """No live lock owners unless a test installs some."""
    monkeypatch.setattr(DeviceExecutionLock, "get_active_owners", classmethod(lambda cls: {}))
    monkeypatch.delenv("ARTEMIS_DEVICE_ID", raising=False)
    monkeypatch.delenv("ADB_DEVICE_SERIAL", raising=False)


@pytest.fixture
def patch_controller(monkeypatch):
    """Install a fake ``_get_controller`` and return the controller it hands out."""

    def _install(get_screen_data=None, factory=None):
        controller = MagicMock()
        controller.ctx = SimpleNamespace(device=SimpleNamespace(device_id=SERIAL))
        if get_screen_data is not None:
            controller.get_screen_data = get_screen_data
        calls: list[str | None] = []

        def _fake_get_controller(device_serial=None):
            calls.append(device_serial)
            if factory is not None:
                return factory(device_serial)
            return controller

        monkeypatch.setattr("artemis.mcp.adb_server._get_controller", _fake_get_controller)
        controller.calls = calls
        return controller

    return _install


def _screen_data(elements, jpeg=FAKE_JPEG):
    return SimpleNamespace(
        base64=base64.b64encode(jpeg).decode("ascii"),
        elements=elements,
        width=1080,
        height=2400,
    )


@pytest.mark.asyncio
async def test_success_reports_bytes_elements_serial_and_elapsed(patch_controller):
    elements = [{"class": "android.widget.FrameLayout"}, {"text": "Hello"}, {"text": "World"}]
    controller = patch_controller(AsyncMock(return_value=_screen_data(elements)))

    result = await smoke_test_device(device_serial=SERIAL, timeout_seconds=5)

    assert result["ok"] is True
    assert result["error"] is None
    assert result["fix"] == []
    assert result["serial"] == SERIAL
    assert result["screenshot_bytes"] == len(FAKE_JPEG)
    assert result["element_count"] == 3
    assert isinstance(result["elapsed_seconds"], float)
    assert result["elapsed_seconds"] >= 0.0
    assert controller.calls == [SERIAL]
    controller.get_screen_data.assert_awaited_once()
    assert set(result) == {
        "ok",
        "serial",
        "elapsed_seconds",
        "screenshot_bytes",
        "element_count",
        "hierarchy_backend",
        "error",
        "fix",
    }


@pytest.mark.asyncio
async def test_serial_comes_from_controller_when_not_requested(patch_controller):
    patch_controller(AsyncMock(return_value=_screen_data([{"text": "x"}])))

    result = await smoke_test_device()

    assert result["ok"] is True
    assert result["serial"] == SERIAL


@pytest.mark.asyncio
async def test_xml_string_elements_are_counted_by_node(patch_controller):
    xml = '<hierarchy><node class="a"><node class="b"/><node class="c"/></node></hierarchy>'
    patch_controller(AsyncMock(return_value=_screen_data(xml)))

    result = await smoke_test_device(device_serial=SERIAL)

    assert result["ok"] is True
    assert result["element_count"] == 3


@pytest.mark.asyncio
async def test_init_failure_no_devices_points_to_launch_avd(patch_controller):
    def _boom(device_serial):
        raise Exception("No Android devices found at localhost:5037")

    patch_controller(factory=_boom)

    result = await smoke_test_device(device_serial=SERIAL, timeout_seconds=5)

    assert result["ok"] is False
    assert result["serial"] == SERIAL
    assert result["screenshot_bytes"] is None
    assert result["element_count"] is None
    assert "No Android devices found" in result["error"]
    assert any("launch_avd" in step for step in result["fix"])
    assert any("mobile_diagnose" in step for step in result["fix"])


@pytest.mark.asyncio
async def test_init_failure_target_not_found_points_to_launch_avd(patch_controller):
    def _boom(device_serial):
        raise Exception(
            f"Target Android device '{device_serial}' not found at localhost:5037 among available devices."
        )

    patch_controller(factory=_boom)

    result = await smoke_test_device(device_serial="emulator-5554")

    assert result["ok"] is False
    assert result["serial"] == "emulator-5554"
    assert any("launch_avd" in step for step in result["fix"])


@pytest.mark.asyncio
async def test_screen_data_timeout_reports_timeout_and_force_stop(patch_controller):
    async def _hang():
        await asyncio.sleep(3)
        return _screen_data([{"text": "late"}])

    patch_controller(_hang)

    result = await smoke_test_device(device_serial=SERIAL, timeout_seconds=0.3)

    assert result["ok"] is False
    assert result["serial"] == SERIAL
    assert result["error"] == "UIAutomator/screen capture did not respond within 0.3s"
    assert result["elapsed_seconds"] < 2.5
    joined = "\n".join(result["fix"])
    assert f"adb -s {SERIAL} shell am force-stop com.github.uiautomator" in joined
    assert "uiautomator2 init" in joined
    assert "screen on" in joined


@pytest.mark.asyncio
async def test_init_timeout_is_reported_without_blocking(patch_controller):
    import time

    def _slow(device_serial):
        time.sleep(2)
        return MagicMock()

    patch_controller(factory=_slow)

    result = await smoke_test_device(device_serial=SERIAL, timeout_seconds=0.3)

    assert result["ok"] is False
    assert "did not respond within 0.3s" in result["error"]
    assert result["elapsed_seconds"] < 1.5
    assert any("force-stop" in step for step in result["fix"])


@pytest.mark.asyncio
async def test_generic_screen_data_exception_falls_back_to_manual_dump(patch_controller):
    patch_controller(AsyncMock(side_effect=RuntimeError("weird failure 42")))

    result = await smoke_test_device(device_serial=SERIAL)

    assert result["ok"] is False
    assert result["error"] == "Screen capture failed: weird failure 42"
    assert any("uiautomator dump" in step for step in result["fix"])


@pytest.mark.asyncio
async def test_base_exception_from_uiautomator2_is_captured_not_raised(patch_controller):
    class HTTPTimeoutError(BaseException):
        """uiautomator2 errors derive from BaseException."""

    patch_controller(
        AsyncMock(side_effect=HTTPTimeoutError("HTTP request timeout: read timed out"))
    )

    result = await smoke_test_device(device_serial=SERIAL)

    assert result["ok"] is False
    assert "HTTPTimeoutError" in result["error"]
    assert any("force-stop" in step for step in result["fix"])


@pytest.mark.asyncio
async def test_unauthorized_and_offline_guidance(patch_controller):
    def _unauth(device_serial):
        raise Exception("device unauthorized. This adb server's $ADB_VENDOR_KEYS is not set")

    patch_controller(factory=_unauth)
    result = await smoke_test_device(device_serial=SERIAL)
    assert result["ok"] is False
    assert any("USB debugging" in step for step in result["fix"])

    def _offline(device_serial):
        raise Exception("device offline")

    patch_controller(factory=_offline)
    result = await smoke_test_device(device_serial=SERIAL)
    assert result["ok"] is False
    assert any("adb reconnect" in step for step in result["fix"])


@pytest.mark.asyncio
async def test_empty_hierarchy_is_not_ok(patch_controller):
    patch_controller(AsyncMock(return_value=_screen_data([])))

    result = await smoke_test_device(device_serial=SERIAL)

    assert result["ok"] is False
    assert result["screenshot_bytes"] == len(FAKE_JPEG)
    assert result["element_count"] == 0
    assert "no UI elements" in result["error"]
    assert any("force-stop" in step for step in result["fix"])


@pytest.mark.asyncio
async def test_placeholder_screenshot_is_not_ok(patch_controller):
    one_pixel_png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    patch_controller(AsyncMock(return_value=_screen_data([{"text": "x"}], jpeg=one_pixel_png)))

    result = await smoke_test_device(device_serial=SERIAL)

    assert result["ok"] is False
    assert result["screenshot_bytes"] == len(one_pixel_png)
    assert "placeholder" in result["error"]


def _owner(device_id: str, description: str = "task-1") -> DeviceLockOwner:
    return DeviceLockOwner(
        pid=4242,
        process_created_at=1.0,
        token="tok",
        device_id=device_id,
        description=description,
        acquired_at="2026-09-09T00:00:00Z",
    )


@pytest.mark.asyncio
async def test_busy_device_is_reported_without_touching_the_controller(
    patch_controller, monkeypatch
):
    controller = patch_controller(AsyncMock(return_value=_screen_data([{"text": "x"}])))
    monkeypatch.setattr(
        DeviceExecutionLock,
        "get_active_owners",
        classmethod(lambda cls: {SERIAL: _owner(SERIAL, "youtube smoke")}),
    )

    result = await smoke_test_device(device_serial=SERIAL)

    assert result["ok"] is False
    assert result["serial"] == SERIAL
    assert "busy" in result["error"]
    assert "youtube smoke" in result["error"]
    assert "4242" in result["error"]
    assert any("mobile_manage_task" in step for step in result["fix"])
    assert controller.calls == []
    controller.get_screen_data.assert_not_awaited()


@pytest.mark.asyncio
async def test_busy_guard_matches_scoped_lock_keys(patch_controller, monkeypatch):
    controller = patch_controller(AsyncMock(return_value=_screen_data([{"text": "x"}])))
    monkeypatch.setattr(
        DeviceExecutionLock,
        "get_active_owners",
        classmethod(lambda cls: {f"endpoint1__{SERIAL}": _owner(SERIAL)}),
    )

    result = await smoke_test_device(device_serial=SERIAL)

    assert result["ok"] is False
    assert "busy" in result["error"]
    assert controller.calls == []


@pytest.mark.asyncio
async def test_busy_guard_ignores_other_devices(patch_controller, monkeypatch):
    controller = patch_controller(AsyncMock(return_value=_screen_data([{"text": "x"}])))
    monkeypatch.setattr(
        DeviceExecutionLock,
        "get_active_owners",
        classmethod(lambda cls: {"emulator-5554": _owner("emulator-5554")}),
    )

    result = await smoke_test_device(device_serial=SERIAL)

    assert result["ok"] is True
    assert controller.calls == [SERIAL]


@pytest.mark.asyncio
async def test_busy_guard_without_target_serial_treats_any_owner_as_busy(
    patch_controller, monkeypatch
):
    controller = patch_controller(AsyncMock(return_value=_screen_data([{"text": "x"}])))
    monkeypatch.setattr(
        DeviceExecutionLock,
        "get_active_owners",
        classmethod(lambda cls: {"emulator-5554": _owner("emulator-5554")}),
    )

    result = await smoke_test_device()

    assert result["ok"] is False
    assert result["serial"] == "emulator-5554"
    assert "busy" in result["error"]
    assert controller.calls == []


def test_fix_for_error_generic_fallback_uses_serial():
    fix = device_smoke.fix_for_error("something odd", SERIAL)
    assert fix
    assert all(SERIAL in step for step in fix)
    assert any("uiautomator dump" in step for step in fix)
    assert device_smoke.fix_for_error(None, SERIAL) == []
    assert "<serial>" in device_smoke.fix_for_error("timeout", None)[1]


def test_smoke_test_is_exported_from_diagnostics_package():
    import artemis.core.diagnostics as diagnostics

    assert "smoke_test_device" in diagnostics.__all__
    assert diagnostics.smoke_test_device is device_smoke.smoke_test_device
