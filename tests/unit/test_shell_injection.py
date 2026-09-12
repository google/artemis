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

"""Regression tests for issue #55: shell injection via package_name/url/key."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.drivers.android.adb_driver import AndroidAdbDriver
from artemis.drivers.base import KeyCode
from artemis.utils.android_validation import (
    coerce_coord,
    coerce_keycode,
    is_valid_package_name,
    quote_url_for_adb,
)
from artemis.utils.app_launch_utils import launch_app_with_retries


def _make_driver():
    mock_adb_client = MagicMock()
    mock_adb_device = MagicMock()
    mock_adb_client.device.return_value = mock_adb_device
    driver = AndroidAdbDriver(device_id="emulator-5554", adb_client=mock_adb_client)
    return driver, mock_adb_device


def _make_controller(mock_driver):
    controller = UnifiedMobileController.__new__(UnifiedMobileController)
    controller.ctx = MagicMock()
    controller._driver = mock_driver
    controller._segment_cache = {}
    return controller


def test_package_name_grammar():
    assert is_valid_package_name("com.android.settings")
    assert is_valid_package_name("com.example_app.Main")
    assert not is_valid_package_name("com.android.settings; reboot")
    assert not is_valid_package_name("com.x$(reboot)")
    assert not is_valid_package_name("com.android.settings && reboot")
    assert not is_valid_package_name("com.android.settings`reboot`")
    assert not is_valid_package_name("")
    assert not is_valid_package_name("singlecomponent")
    assert not is_valid_package_name(None)
    assert not is_valid_package_name(123)


@pytest.mark.asyncio
async def test_driver_launch_app_rejects_injection():
    driver, device = _make_driver()
    assert await driver.launch_app("com.android.settings; reboot") is False
    assert await driver.launch_app("com.android.settings && reboot") is False
    assert await driver.launch_app("com.x$(whoami)") is False
    device.shell.assert_not_called()


@pytest.mark.asyncio
async def test_driver_launch_app_accepts_valid():
    driver, device = _make_driver()
    assert await driver.launch_app("com.android.settings") is True
    device.shell.assert_called_with(
        "monkey -p com.android.settings -c android.intent.category.LAUNCHER 1"
    )


@pytest.mark.asyncio
async def test_driver_stop_app_rejects_injection():
    driver, device = _make_driver()
    assert await driver.stop_app("com.android.settings; reboot") is False
    device.shell.assert_not_called()


@pytest.mark.asyncio
async def test_driver_stop_app_accepts_valid():
    driver, device = _make_driver()
    assert await driver.stop_app("com.android.settings") is True
    device.shell.assert_called_with("am force-stop com.android.settings")


@pytest.mark.asyncio
async def test_driver_press_key_rejects_injection():
    driver, device = _make_driver()
    assert await driver.press_key("4; reboot") is False
    assert await driver.press_key("ENTER; reboot") is False
    assert await driver.press_key("$(reboot)") is False
    device.shell.assert_not_called()


@pytest.mark.asyncio
async def test_driver_press_key_accepts_known_and_numeric():
    driver, device = _make_driver()
    assert await driver.press_key(KeyCode.BACK) is True
    device.shell.assert_called_with("input keyevent 4")
    assert await driver.press_key("123") is True
    device.shell.assert_called_with("input keyevent 123")
    assert await driver.press_key("KEYCODE_ENTER") is True
    device.shell.assert_called_with("input keyevent 66")


def test_coerce_keycode_rejects_out_of_range():
    from artemis.drivers.android.adb_driver import ANDROID_KEYCODE_MAP

    assert coerce_keycode("4; reboot", ANDROID_KEYCODE_MAP) is None
    assert coerce_keycode(99999, ANDROID_KEYCODE_MAP) is None
    assert coerce_keycode("99999", ANDROID_KEYCODE_MAP) is None
    assert coerce_keycode(True, ANDROID_KEYCODE_MAP) is None


@pytest.mark.asyncio
async def test_driver_tap_rejects_non_integer():
    driver, device = _make_driver()
    assert await driver.tap("540; reboot", 1200) is False  # type: ignore[arg-type]
    device.shell.assert_not_called()
    assert coerce_coord("540; reboot") is None
    assert coerce_coord("540") == 540


@pytest.mark.asyncio
async def test_controller_open_url_quotes_single_quote():
    mock_driver = AsyncMock()
    mock_driver.execute_shell.return_value = "ok"
    controller = _make_controller(mock_driver)
    url = "http://x'; reboot; echo '"
    assert await controller.open_url(url) is True
    (cmd,), _ = mock_driver.execute_shell.call_args
    # shlex.quote keeps it a single shell word: no unquoted command separator.
    assert cmd.startswith("am start -a android.intent.action.VIEW -d ")
    assert "; reboot;" not in cmd.replace("'; reboot; echo '", "QUOTED")
    # The quoted payload must round-trip through POSIX shell parsing as one word.
    import shlex as _shlex

    words = _shlex.split(cmd)
    assert url in words


@pytest.mark.asyncio
async def test_controller_open_url_rejects_invalid():
    mock_driver = AsyncMock()
    controller = _make_controller(mock_driver)
    assert await controller.open_url("") is False
    assert await controller.open_url("   ") is False
    assert await controller.open_url("http://x\nreboot") is False
    mock_driver.execute_shell.assert_not_called()


def test_quote_url_rejects_control_chars():
    with pytest.raises(ValueError):
        quote_url_for_adb("")
    with pytest.raises(ValueError):
        quote_url_for_adb("http://x\nreboot")


@pytest.mark.asyncio
async def test_controller_launch_terminate_reject_invalid():
    mock_driver = AsyncMock()
    controller = _make_controller(mock_driver)
    assert await controller.launch_app("com.android.settings; reboot") is False
    assert await controller.terminate_app("com.android.settings; reboot") is False
    mock_driver.launch_app.assert_not_called()
    mock_driver.stop_app.assert_not_called()


@pytest.mark.asyncio
async def test_launch_with_retries_rejects_invalid_without_controller():
    with patch("artemis.utils.app_launch_utils.UnifiedMobileController") as mock_controller_cls:
        success, error = await launch_app_with_retries(
            ctx=MagicMock(), app_package="com.android.settings; reboot"
        )
        assert success is False
        assert "Invalid Android package name" in (error or "")
        mock_controller_cls.assert_not_called()
