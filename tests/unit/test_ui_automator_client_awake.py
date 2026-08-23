from unittest.mock import MagicMock, patch

import pytest

from artemis.clients.ui_automator_client import (
    AWAKE_STRATEGY_HEARTBEAT,
    AWAKE_STRATEGY_STAY_ON,
    AWAKE_STRATEGY_WAKE_LOCK,
    UIAutomatorClient,
    _keep_device_awake,
)
from artemis.drivers.android.adb_driver import AndroidAdbDriver


def completed(stdout="", returncode=0, stderr=""):
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


@patch("artemis.clients.ui_automator_client.subprocess.run")
def test_keep_device_awake_uses_verified_primary_strategy(mock_run):
    mock_run.side_effect = [
        completed(),
        completed(),
        completed(),
        completed("  mStayOn=true\n"),
    ]

    strategy = _keep_device_awake("device-123")

    assert strategy == AWAKE_STRATEGY_STAY_ON
    assert mock_run.call_count == 4


@patch("artemis.clients.ui_automator_client.subprocess.run")
def test_keep_device_awake_falls_back_to_verified_screen_wake_lock(mock_run):
    mock_run.side_effect = [
        completed(),
        completed(),
        completed(),
        completed("  mStayOn=false\n"),
        completed("WakeLock{abc held=true, refCount=1}\n"),
        completed(
            "Display 0, wakelock type: SCREEN_BRIGHT_WAKE_LOCK: "
            "WakeLock{abc held=true, refCount=1}\n"
        ),
    ]

    strategy = _keep_device_awake("device-123")

    assert strategy == AWAKE_STRATEGY_WAKE_LOCK
    assert mock_run.call_args_list[-2].args[0] == [
        "adb",
        "-s",
        "device-123",
        "shell",
        "cmd",
        "power",
        "set-wakelock",
        "acquire",
        "-d",
        "0",
        "SCREEN_BRIGHT_WAKE_LOCK",
    ]


@patch("artemis.clients.ui_automator_client.subprocess.run")
def test_keep_device_awake_uses_heartbeat_when_wake_lock_is_unavailable(mock_run):
    mock_run.side_effect = [
        completed(),
        completed(),
        completed(),
        completed("  mStayOn=false\n"),
        completed(returncode=1, stderr="unknown command"),
        completed("Wakelocks:\n"),
    ]

    strategy = _keep_device_awake("device-123")

    assert strategy == AWAKE_STRATEGY_HEARTBEAT


@patch.dict("os.environ", {"ARTEMIS_KEEP_DEVICE_AWAKE": "false"})
@patch("artemis.clients.ui_automator_client.subprocess.run")
def test_keep_device_awake_can_be_disabled(mock_run):
    assert _keep_device_awake("device-123") is None
    mock_run.assert_not_called()


@patch("artemis.clients.ui_automator_client.u2.connect")
@patch(
    "artemis.clients.ui_automator_client._keep_device_awake",
    return_value=AWAKE_STRATEGY_HEARTBEAT,
)
@patch("artemis.clients.ui_automator_client._ensure_maestro_not_installed")
def test_new_ui_connection_starts_heartbeat_fallback(
    mock_remove_maestro, mock_keep_awake, mock_connect
):
    client = UIAutomatorClient("device-123")
    client._start_awake_heartbeat = MagicMock()

    client._ensure_connected()

    mock_remove_maestro.assert_called_once_with("device-123")
    mock_keep_awake.assert_called_once_with("device-123")
    mock_connect.assert_called_once_with("device-123")
    client._start_awake_heartbeat.assert_called_once_with()
    assert client._awake_strategy == AWAKE_STRATEGY_HEARTBEAT


@patch("artemis.clients.ui_automator_client._release_screen_wake_lock")
def test_disconnect_releases_screen_wake_lock(mock_release):
    client = UIAutomatorClient("device-123")
    client._awake_strategy = AWAKE_STRATEGY_WAKE_LOCK
    client._stop_awake_heartbeat = MagicMock()

    client.disconnect()

    client._stop_awake_heartbeat.assert_called_once_with()
    mock_release.assert_called_once_with("device-123")
    assert client._awake_strategy is None


@patch("artemis.clients.ui_automator_client.threading.Thread")
@patch("artemis.clients.ui_automator_client.threading.Event")
def test_heartbeat_thread_is_daemonized_and_stoppable(mock_event, mock_thread):
    stop_event = MagicMock()
    thread = MagicMock()
    mock_event.return_value = stop_event
    mock_thread.return_value = thread
    client = UIAutomatorClient("device-123")

    client._start_awake_heartbeat()
    client._stop_awake_heartbeat()

    assert mock_thread.call_args.kwargs["daemon"] is True
    thread.start.assert_called_once_with()
    stop_event.set.assert_called_once_with()
    thread.join.assert_called_once_with(timeout=2.0)


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


@patch("artemis.clients.ui_automator_client.subprocess.run")
def test_keep_device_awake_continues_after_an_adb_failure(mock_run):
    mock_run.side_effect = [
        completed(returncode=1, stderr="not allowed"),
        OSError("adb temporarily unavailable"),
        completed(),
        completed("mStayOn=true\n"),
    ]

    assert _keep_device_awake("device-123") == AWAKE_STRATEGY_STAY_ON
    assert mock_run.call_count == 4
