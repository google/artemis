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
Unit tests for RemoteAdbDevice, RemoteAdbClient, and platform specific commands parity.
"""

from unittest.mock import MagicMock

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.cloud]
pytest.importorskip(
    "cloud_service",
    reason="optional cloud_service package is not installed",
)

from cloud_service.virtualization.remote_adb import (
    RemoteAdbDevice,
    RemoteAdbClient,
)

from artemis.context import ArtemisContext, DeviceContext, DevicePlatform
from artemis.controllers.platform_specific_commands_controller import (
    get_adb_device,
    list_packages,
    get_current_foreground_package,
    get_device_date,
)


def test_remote_adb_device_shell():
    mock_bridge = MagicMock()
    mock_bridge.execute_sync.return_value = {
        "status": "SUCCESS",
        "output": "package:/data/app/com.android.chrome.apk=com.android.chrome\npackage:/data/app/com.android.settings.apk=com.android.settings",
    }

    device = RemoteAdbDevice(serial="emulator-5554", bridge=mock_bridge)
    output = device.shell("pm list packages -f")

    assert "com.android.chrome" in output
    mock_bridge.execute_sync.assert_called_once_with("shell", {"command": "pm list packages -f"})


def test_remote_adb_device_current_app():
    mock_bridge = MagicMock()
    mock_bridge.execute_sync.return_value = {
        "status": "SUCCESS",
        "package": "com.android.chrome",
        "activity": "com.google.android.apps.chrome.Main",
    }

    device = RemoteAdbDevice(serial="emulator-5554", bridge=mock_bridge)
    app = device.current_app()

    assert app is not None
    assert app.package == "com.android.chrome"
    assert app.activity == "com.google.android.apps.chrome.Main"


def test_remote_adb_device_window_size():
    mock_bridge = MagicMock()
    mock_bridge.execute_sync.return_value = {"status": "SUCCESS", "width": 1440, "height": 3120}

    device = RemoteAdbDevice(serial="emulator-5554", bridge=mock_bridge)
    size = device.window_size()

    assert size == (1440, 3120)


def test_platform_commands_parity_with_remote_device(monkeypatch):
    monkeypatch.setenv("ARTEMIS_CLOUD_MODE", "1")
    monkeypatch.setenv("ARTEMIS_CLOUD_SESSION_ID", "session_parity_001")
    monkeypatch.setenv("ADB_DEVICE_SERIAL", "emulator-5554")

    mock_bridge = MagicMock()
    mock_bridge.execute_sync.side_effect = [
        # 1. date
        {"status": "SUCCESS", "output": "Thu Aug 6 12:00:00 UTC 2026\n"},
        # 2. pm list packages
        {
            "status": "SUCCESS",
            "output": "package:/data/app/com.example.app1.apk=com.example.app1\npackage:/data/app/com.example.app2.apk=com.example.app2",
        },
        # 3. current_app
        {"status": "SUCCESS", "package": "com.example.app1", "activity": ".MainActivity"},
    ]

    client = RemoteAdbClient(bridge=mock_bridge)
    device = client.device("emulator-5554")

    ctx = ArtemisContext(
        device=DeviceContext(
            host_platform="LINUX",
            mobile_platform=DevicePlatform.ANDROID,
            device_id="emulator-5554",
            device_width=1080,
            device_height=2400,
        ),
        adb_client=client,
    )

    dev = get_adb_device(ctx)
    assert dev.serial == "emulator-5554"

    # Verify real dynamic date
    date_str = get_device_date(ctx)
    assert "2026" in date_str

    # Verify real dynamic package listing (Zero hardcoding)
    pkgs = list_packages(ctx)
    assert pkgs == "com.example.app1\ncom.example.app2"

    # Verify real dynamic foreground package detection (Zero retry loop)
    fg_pkg = get_current_foreground_package(ctx)
    assert fg_pkg == "com.example.app1"


def test_remote_uiautomator_client_parity():
    """Verify RemoteUIAutomatorClient correctly returns structured UIAutomatorScreenData."""
    from cloud_service.virtualization.remote_adb import RemoteUIAutomatorClient

    mock_bridge = MagicMock()
    mock_bridge.execute_sync.return_value = {
        "status": "SUCCESS",
        "base64": "fake_b64",
        "hierarchy_xml": "<hierarchy><node text='Search' bounds='[100,200][300,400]'/></hierarchy>",
        "elements": [{"text": "Search", "bounds": "[100,200][300,400]"}],
        "width": 1080,
        "height": 2400,
    }

    client = RemoteUIAutomatorClient(bridge=mock_bridge)
    b64, xml, elements = client.get_screen_data()

    assert b64 == "fake_b64"
    assert "<node text='Search'" in xml
    assert len(elements) == 1
    assert elements[0]["text"] == "Search"


def test_controller_factory_cloud_mode_uses_unified_controller(monkeypatch):
    """Verify controller_factory creates UnifiedMobileController in Cloud Mode."""
    monkeypatch.setenv("ARTEMIS_CLOUD_MODE", "1")
    monkeypatch.setenv("ARTEMIS_CLOUD_SESSION_ID", "test_cloud_session")
    monkeypatch.setenv("ADB_DEVICE_SERIAL", "emulator-5554")

    from artemis.controllers.controller_factory import create_device_controller
    from artemis.controllers.unified_controller import UnifiedMobileController

    ctx = ArtemisContext(
        device=DeviceContext(
            host_platform="LINUX",
            mobile_platform=DevicePlatform.ANDROID,
            device_id="emulator-5554",
            device_width=1080,
            device_height=2400,
        )
    )

    controller = create_device_controller(ctx)
    assert isinstance(controller, UnifiedMobileController)
    assert controller.driver.device_id == "emulator-5554"
