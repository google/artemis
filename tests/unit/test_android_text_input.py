"""Regression coverage for rejected clipboard writes and Unicode input fallback."""

import base64
from unittest.mock import MagicMock

import pytest

from artemis.clients.ui_automator_client import UIAutomatorClient
from artemis.drivers.android.adb_driver import AndroidAdbDriver


@pytest.fixture
def input_path():
    device = MagicMock()
    device.shell.return_value = "com.example/.Keyboard"
    client = MagicMock()
    client.send_text.return_value = True
    # The real helper can claim clipboard success despite a denied write.
    client.set_clipboard.return_value = True
    adb = MagicMock()
    adb.device.return_value = device
    action = AndroidAdbDriver("test", adb, client).input_text
    return action, device, client


@pytest.mark.asyncio
async def test_direct_unicode_input_does_not_paste_or_duplicate(input_path):
    action, device, client = input_path
    assert await action("héllo\n你好", clear_existing=False) is True
    client.send_text.assert_called_once_with("héllo\n你好")
    client.set_clipboard.assert_not_called()
    assert [c.args[0] for c in device.shell.call_args_list] == ["input keyevent 123"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, RuntimeError("unsupported field")])
async def test_failed_direct_input_falls_back_to_adbkeyboard(input_path, failure):
    action, device, client = input_path
    if isinstance(failure, Exception):
        client.send_text.side_effect = failure
    else:
        client.send_text.return_value = failure
    device.shell.return_value = "com.android.adbkeyboard/.AdbIME"
    assert await action("你好", clear_existing=False) is True
    payload = base64.b64encode("你好".encode()).decode()
    device.shell.assert_any_call(f"am broadcast -a ADB_INPUT_B64 --es msg '{payload}'")
    client.set_clipboard.assert_not_called()


@pytest.mark.asyncio
async def test_unicode_without_supported_channel_reports_failure(input_path):
    action, device, client = input_path
    client.send_text.return_value = False
    assert await action("你好", clear_existing=False) is False
    assert not any(c.args[0].startswith("input text ") for c in device.shell.call_args_list)
    client.set_clipboard.assert_not_called()


def test_uiautomator_send_text_returns_success_and_restores_ime(monkeypatch):
    device = MagicMock()
    monkeypatch.setattr(UIAutomatorClient, "_ensure_connected", lambda self: device)
    monkeypatch.setattr("artemis.clients.ui_automator_client.time.sleep", lambda _: None)
    client = object.__new__(UIAutomatorClient)
    assert client.send_text("你好") is True
    device.send_keys.assert_called_once_with("你好")
    assert [c.args for c in device.set_fastinput_ime.call_args_list] == [(True,), (False,)]


@pytest.mark.asyncio
async def test_ascii_fallback_still_inputs_text(input_path):
    action, device, client = input_path
    client.send_text.return_value = False
    assert await action("hello world", clear_existing=False) is True
    device.shell.assert_any_call("input text hello%sworld")


@pytest.mark.asyncio
async def test_driver_normalizes_escaped_newlines_before_direct_input():
    client = MagicMock()
    client.send_text.return_value = True
    driver = AndroidAdbDriver("test", MagicMock(), client)
    assert await driver.input_text(r"你好\n世界") is True
    client.send_text.assert_called_once_with("你好\n世界")


def test_uiautomator_send_text_restores_ime_on_error(monkeypatch):
    device = MagicMock()
    device.send_keys.side_effect = RuntimeError("input failed")
    monkeypatch.setattr(UIAutomatorClient, "_ensure_connected", lambda self: device)
    monkeypatch.setattr("artemis.clients.ui_automator_client.time.sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="input failed"):
        object.__new__(UIAutomatorClient).send_text("你好")
    device.set_fastinput_ime.assert_called_with(False)


@pytest.mark.asyncio
async def test_unicode_without_client_or_adbkeyboard_fails():
    device = MagicMock()
    device.shell.return_value = "com.example/.Keyboard"
    adb = MagicMock()
    adb.device.return_value = device
    action = AndroidAdbDriver("test", adb).input_text
    assert await action("你好", clear_existing=False) is False
    assert not any(c.args[0].startswith("input text ") for c in device.shell.call_args_list)
