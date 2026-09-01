from unittest.mock import MagicMock, patch

from artemis.runtime.awake_service import (
    AWAKE_STRATEGY_HEARTBEAT,
    AWAKE_STRATEGY_USB,
    ScreenAwakeService,
    _configure_usb_stay_awake,
    _discover_connected_device_ids,
)


def completed(stdout="", returncode=0, stderr=""):
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


@patch("artemis.runtime.awake_service.ScreenAwakeLease")
@patch("artemis.runtime.awake_service.subprocess.run")
def test_usb_policy_is_primary_when_android_reports_it_active(mock_run, lease_type):
    mock_run.side_effect = [
        completed(),
        completed(),
        completed(),
        completed("2\n"),
        completed("  mIsPowered=true\n  mPlugType=2\n  mStayOn=true\n"),
    ]

    assert _configure_usb_stay_awake("device-123") == AWAKE_STRATEGY_USB

    lease_type.assert_called_once_with("device-123")
    lease_type.return_value.cleanup_unowned_references.assert_called_once_with()
    commands = [call.args[0] for call in mock_run.call_args_list]
    assert ["shell", "svc", "power", "stayon", "usb"] in [command[3:] for command in commands]
    assert not any("SCREEN_BRIGHT_WAKE_LOCK" in command for command in commands)
    assert not any("KEYCODE_UNKNOWN" in command for command in commands)


@patch("artemis.runtime.awake_service.ScreenAwakeLease")
@patch("artemis.runtime.awake_service.subprocess.run")
def test_inactive_usb_policy_falls_back_to_effective_host_heartbeat(mock_run, _lease_type):
    mock_run.side_effect = [
        completed(),
        completed(),
        completed(),
        completed("2\n"),
        completed("  mIsPowered=true\n  mPlugType=2\n  mStayOn=false\n"),
        completed(),
    ]

    assert _configure_usb_stay_awake("device-123") == AWAKE_STRATEGY_HEARTBEAT

    assert mock_run.call_args_list[-1].args[0][-1] == "KEYCODE_UNKNOWN"


@patch.dict("os.environ", {"ARTEMIS_KEEP_DEVICE_AWAKE": "false"})
@patch("artemis.runtime.awake_service.subprocess.run")
def test_awake_strategy_can_be_disabled(mock_run):
    assert _configure_usb_stay_awake("device-123") is None
    mock_run.assert_not_called()


@patch("artemis.runtime.awake_service.threading.Thread")
@patch("artemis.runtime.awake_service.threading.Event")
@patch(
    "artemis.runtime.awake_service._configure_usb_stay_awake",
    return_value=AWAKE_STRATEGY_HEARTBEAT,
)
def test_host_heartbeat_lives_until_host_shutdown(configure, event_type, thread_type):
    service = ScreenAwakeService()
    stop_event = event_type.return_value
    thread = thread_type.return_value

    service.ensure_device("device-123")
    service.ensure_device("device-123")

    configure.assert_called_once_with("device-123")
    thread.start.assert_called_once_with()
    stop_event.set.assert_not_called()

    service.shutdown()
    service.shutdown()

    stop_event.set.assert_called_once_with()
    thread.join.assert_called_once_with(timeout=2.0)


@patch("artemis.runtime.awake_service._run_awake_adb_command")
@patch("artemis.runtime.awake_service.threading.Thread")
@patch("artemis.runtime.awake_service.threading.Event")
@patch(
    "artemis.runtime.awake_service._configure_usb_stay_awake",
    return_value=AWAKE_STRATEGY_HEARTBEAT,
)
def test_heartbeat_loop_uses_verified_user_activity_key(
    _configure, event_type, thread_type, run_command
):
    stop_event = event_type.return_value
    stop_event.wait.side_effect = [False, True]
    service = ScreenAwakeService()

    service.ensure_device("device-123")
    heartbeat_target = thread_type.call_args.kwargs["target"]
    heartbeat_target()

    run_command.assert_called_once_with(
        "device-123",
        ["shell", "input", "keyevent", "KEYCODE_UNKNOWN"],
        "send the host stay-awake heartbeat",
    )


@patch("artemis.runtime.awake_service.threading.Thread")
@patch("artemis.runtime.awake_service.threading.Event")
@patch("artemis.runtime.awake_service._discover_connected_device_ids")
@patch(
    "artemis.runtime.awake_service._configure_usb_stay_awake",
    return_value=AWAKE_STRATEGY_USB,
)
def test_service_monitor_enrolls_a_device_attached_after_start(
    configure, discover, event_type, thread_type
):
    discover.side_effect = [[], ["device-late"]]
    stop_event = event_type.return_value
    stop_event.wait.side_effect = [False, True]
    stop_event.is_set.return_value = False
    service = ScreenAwakeService()

    service.start()
    monitor_target = thread_type.call_args.kwargs["target"]
    monitor_target()

    configure.assert_called_once_with("device-late")
    assert service.device_ids == ("device-late",)


@patch("artemis.runtime.awake_service.threading.Thread")
@patch("artemis.runtime.awake_service.threading.Event")
@patch(
    "artemis.runtime.awake_service._configure_usb_stay_awake",
    return_value=AWAKE_STRATEGY_HEARTBEAT,
)
def test_disconnect_reconciliation_stops_heartbeat_and_allows_reenrollment(
    configure, event_type, thread_type
):
    service = ScreenAwakeService()
    stop_event = event_type.return_value
    thread = thread_type.return_value
    service.ensure_device("device-123")

    service._reconcile_connected_devices(set())

    stop_event.set.assert_called_once_with()
    thread.join.assert_called_once_with(timeout=2.0)
    assert service.device_ids == ()

    service.ensure_device("device-123")
    assert configure.call_count == 2


@patch("artemis.runtime.device_pool.device_pool.get_claimed_serials")
@patch("artemis.runtime.awake_service.AdbClient")
def test_discovery_keeps_only_pool_claimed_devices(adb_client, claimed):
    """The pool manages two devices while the ADB server lists three: only the
    two claimed devices are kept awake; the unrelated one is left untouched."""
    adb_client.return_value.device_list.return_value = [
        MagicMock(serial="device-1"),
        MagicMock(serial="device-2"),
        MagicMock(serial="device-3"),
    ]
    claimed.return_value = {"device-1", "device-2"}

    assert _discover_connected_device_ids() == ["device-1", "device-2"]


@patch(
    "artemis.runtime.device_pool.device_pool.get_claimed_serials",
    return_value=set(),
)
@patch("artemis.runtime.awake_service.AdbClient")
def test_discovery_returns_nothing_when_pool_claims_no_device(adb_client, _claimed):
    adb_client.return_value.device_list.return_value = [
        MagicMock(serial="device-1"),
        MagicMock(serial="device-2"),
    ]

    assert _discover_connected_device_ids() == []
