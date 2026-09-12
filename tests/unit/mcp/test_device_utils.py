"""Unit tests for platform-specific emulator process startup."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from mcp_server.utils import device_utils


def test_ensure_emulator_uses_windows_creation_flags(monkeypatch) -> None:
    popen = MagicMock()
    monkeypatch.setattr(device_utils.sys, "platform", "win32")
    monkeypatch.setattr(device_utils, "is_emulator_running", lambda _adb: False)
    monkeypatch.setattr(device_utils.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(device_utils.subprocess, "Popen", popen)
    monkeypatch.setattr(
        device_utils.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="1"),
    )
    monkeypatch.setattr(device_utils.time, "sleep", lambda _seconds: None)

    assert device_utils.ensure_emulator(
        adb_path="adb.exe", emulator_path="emulator.exe", timeout_seconds=1
    )

    kwargs = popen.call_args.kwargs
    assert kwargs["creationflags"] == (
        device_utils.subprocess.CREATE_NEW_PROCESS_GROUP | device_utils.subprocess.DETACHED_PROCESS
    )
    assert "start_new_session" not in kwargs


def test_ensure_emulator_starts_new_session_on_posix(monkeypatch) -> None:
    popen = MagicMock()
    monkeypatch.setattr(device_utils.sys, "platform", "linux")
    monkeypatch.setattr(device_utils, "is_emulator_running", lambda _adb: False)
    monkeypatch.setattr(device_utils.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(device_utils.subprocess, "Popen", popen)
    monkeypatch.setattr(
        device_utils.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="1"),
    )
    monkeypatch.setattr(device_utils.time, "sleep", lambda _seconds: None)

    assert device_utils.ensure_emulator(adb_path="adb", emulator_path="emulator", timeout_seconds=1)

    kwargs = popen.call_args.kwargs
    assert kwargs["start_new_session"] is True
    assert "creationflags" not in kwargs
