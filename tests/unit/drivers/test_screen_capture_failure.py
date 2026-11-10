from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from artemis.drivers.android.adb_driver import AndroidAdbDriver, ScreenCaptureFailed


def _driver_whose_device_cannot_capture() -> AndroidAdbDriver:
    adb_client = MagicMock()
    adb_client.device.return_value.screenshot.side_effect = ConnectionError("device offline")
    return AndroidAdbDriver(device_id="103.123.5.27:5555", adb_client=adb_client)


def test_an_offline_device_raises_instead_of_returning_a_placeholder_screen() -> None:
    driver = _driver_whose_device_cannot_capture()

    with pytest.raises(ScreenCaptureFailed) as raised:
        import asyncio

        asyncio.run(driver.get_screen_data(skip_settling=True))

    assert "103.123.5.27:5555" in str(raised.value)


def test_the_failure_names_the_device_so_the_caller_can_reconnect_it() -> None:
    error = ScreenCaptureFailed("192.168.1.9:5555")

    assert error.device_id == "192.168.1.9:5555"
    assert "unreachable" in str(error)
