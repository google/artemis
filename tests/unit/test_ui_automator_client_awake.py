from unittest.mock import MagicMock, patch

import pytest

from artemis.clients.ui_automator_client import UIAutomatorClient
from artemis.drivers.android.adb_driver import AndroidAdbDriver


@patch("artemis.clients.ui_automator_client.u2.connect")
@patch(
    "artemis.clients.ui_automator_client.ensure_device_awake",
    return_value="host_heartbeat",
)
@patch("artemis.clients.ui_automator_client._ensure_maestro_not_installed")
def test_new_ui_connection_enrolls_device_in_shared_awake_strategy(
    mock_remove_maestro, mock_ensure_awake, mock_connect
):
    client = UIAutomatorClient("device-123")

    client.connect()
    client.connect()

    mock_remove_maestro.assert_called_once_with("device-123")
    mock_ensure_awake.assert_called_once_with("device-123")
    mock_connect.assert_called_once_with("device-123")


@patch("artemis.clients.ui_automator_client.u2.connect", side_effect=RuntimeError("offline"))
@patch(
    "artemis.clients.ui_automator_client.ensure_device_awake",
    return_value="host_heartbeat",
)
@patch("artemis.clients.ui_automator_client._ensure_maestro_not_installed")
def test_failed_ui_connection_does_not_stop_process_awake_service(
    _mock_remove_maestro, mock_ensure_awake, _mock_connect
):
    client = UIAutomatorClient("device-123")

    with pytest.raises(RuntimeError, match="offline"):
        client.connect()

    mock_ensure_awake.assert_called_once_with("device-123")
    assert client._awake_strategy is None


@patch("artemis.clients.ui_automator_client.u2.connect")
@patch(
    "artemis.clients.ui_automator_client.ensure_device_awake",
    return_value="host_heartbeat",
)
@patch("artemis.clients.ui_automator_client._ensure_maestro_not_installed")
def test_client_disconnect_does_not_send_power_cleanup_commands(
    _mock_remove_maestro, mock_ensure_awake, _mock_connect
):
    client = UIAutomatorClient("device-123")
    client.connect()

    client.disconnect()

    mock_ensure_awake.assert_called_once_with("device-123")
    assert client._device is None
    assert client._awake_strategy is None


@pytest.mark.asyncio
async def test_android_driver_disconnect_cleans_up_ui_client():
    ui_client = MagicMock()
    driver = AndroidAdbDriver(
        device_id="device-123",
        adb_client=MagicMock(),
        ui_adb_client=ui_client,
    )

    await driver.disconnect()

    ui_client.disconnect.assert_called_once_with()
