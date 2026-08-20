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

"""Unit tests for Unified Base Device Driver and implementations."""

import base64
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from artemis.drivers.base import KeyCode, ScreenData, SwipeDirection
from artemis.drivers.mock.mock_driver import MockDeviceDriver
from artemis.drivers.android.adb_driver import AndroidAdbDriver


_ONE_PIXEL_PNG = base64.b64encode(
    base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
).decode("ascii")


@pytest.mark.asyncio
async def test_mock_driver_actions():
    """Verify MockDeviceDriver records and performs all core actions."""
    driver = MockDeviceDriver(device_id="test-mock", width=1080, height=2400)
    assert driver.device_id == "test-mock"
    assert driver.screen_size == (1080, 2400)

    # 1. Screen data
    screen_data = await driver.get_screen_data()
    assert isinstance(screen_data, ScreenData)
    assert screen_data.width == 1080
    assert screen_data.height == 2400
    assert screen_data.screenshot_base64 is not None

    # 2. Tap
    assert await driver.tap(500, 1000) is True
    assert driver.action_history[-1]["action"] == "tap"
    assert driver.action_history[-1]["x"] == 500

    # 3. Swipe
    assert await driver.swipe(100, 200, 300, 400) is True
    assert driver.action_history[-1]["action"] == "swipe"

    # 4. Swipe direction
    assert await driver.swipe_direction(SwipeDirection.UP) is True
    assert driver.action_history[-1]["direction"] == "up"

    # 5. Input text
    assert await driver.input_text("Hello Artemis") is True
    assert driver.action_history[-1]["text"] == "Hello Artemis"

    # 6. Press key
    assert await driver.press_key(KeyCode.HOME) is True
    assert driver.action_history[-1]["key"] == "home"

    # 7. App lifecycle
    assert await driver.launch_app("com.android.settings") is True
    assert await driver.get_current_package() == "com.android.settings"
    assert await driver.stop_app("com.android.settings") is True


@pytest.mark.asyncio
async def test_android_driver_with_mock_adb():
    """Verify AndroidAdbDriver formats shell commands properly."""
    mock_adb_client = MagicMock()
    mock_adb_device = MagicMock()
    mock_adb_client.device.return_value = mock_adb_device

    driver = AndroidAdbDriver(
        device_id="emulator-5554",
        adb_client=mock_adb_client,
        width=1080,
        height=2400,
    )

    # Test tap
    await driver.tap(540, 1200)
    mock_adb_device.shell.assert_called_with("input tap 540 1200")

    # Test multi-tap
    await driver.tap(540, 1200, times=3, delay_ms=100)
    expected_multi = "input tap 540 1200 && sleep 0.100 && input tap 540 1200 && sleep 0.100 && input tap 540 1200"
    mock_adb_device.shell.assert_called_with(expected_multi)

    # Test press key
    await driver.press_key(KeyCode.BACK)
    mock_adb_device.shell.assert_called_with("input keyevent 4")


@pytest.mark.asyncio
async def test_android_driver_preserves_elements_from_combined_screen_data():
    """The combined uiautomator2 response is the authoritative live snapshot."""
    mock_adb_client = MagicMock()
    mock_ui_client = MagicMock()
    expected_elements = [
        {
            "text": "",
            "class": "android.widget.FrameLayout",
            "bounds": "[0,0][1080,2400]",
            "clickable": "false",
            "focusable": "false",
        },
        {
            "text": "Start",
            "class": "android.widget.Button",
            "bounds": "[10,20][100,80]",
        }
    ]
    mock_ui_client.get_screen_data.return_value = SimpleNamespace(
        base64=_ONE_PIXEL_PNG,
        hierarchy_xml="<hierarchy />",
        elements=expected_elements,
        width=1080,
        height=2400,
    )

    driver = AndroidAdbDriver(
        device_id="emulator-5554",
        adb_client=mock_adb_client,
        ui_adb_client=mock_ui_client,
    )

    screen_data = await driver.get_screen_data(skip_settling=True)

    assert screen_data.ui_elements == [expected_elements[1]]
    assert screen_data.ui_hierarchy_xml == "<hierarchy />"
    mock_ui_client.get_hierarchy.assert_not_called()


@pytest.mark.asyncio
async def test_android_driver_parses_xml_hierarchy_fallback():
    """XML-only clients remain supported without discarding their hierarchy."""
    mock_adb_client = MagicMock()
    mock_ui_client = MagicMock()
    mock_ui_client.get_screen_data.return_value = SimpleNamespace(
        base64=_ONE_PIXEL_PNG,
        width=1080,
        height=2400,
    )
    mock_ui_client.get_hierarchy.return_value = (
        '<hierarchy><node text="Settings" class="android.widget.TextView" '
        'bounds="[0,0][100,100]" /></hierarchy>'
    )

    driver = AndroidAdbDriver(
        device_id="emulator-5554",
        adb_client=mock_adb_client,
        ui_adb_client=mock_ui_client,
    )

    screen_data = await driver.get_screen_data(skip_settling=True)

    assert screen_data.ui_hierarchy_xml is not None
    assert any(element.get("text") == "Settings" for element in screen_data.ui_elements)
