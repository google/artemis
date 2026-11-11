from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from artemis.drivers.android.adb_driver import AndroidAdbDriver


def _shell_calls_from_a_swipe() -> list[str]:
    adb_client = MagicMock()
    driver = AndroidAdbDriver(device_id="103.123.5.27:5555", adb_client=adb_client)
    asyncio.run(driver.swipe(553, 362, 941, 362, 800))
    return [call.args[0] for call in adb_client.device.return_value.shell.call_args_list]


def test_a_swipe_reaches_the_widget_as_move_events() -> None:
    commands = _shell_calls_from_a_swipe()

    assert len(commands) == 1
    drag = commands[0]
    assert "input motionevent DOWN 553 362" in drag
    assert drag.count("MOVE") >= 3
    assert "input motionevent MOVE 941 362" in drag
    assert "input motionevent UP 941 362" in drag


def test_a_swipe_never_sends_the_input_swipe_command() -> None:
    """The 中控 brightness slider ignored `input swipe` four times on 2026-11-11 while the same drag sent as move events moved it."""

    commands = _shell_calls_from_a_swipe()

    assert all("input swipe" not in command for command in commands)
