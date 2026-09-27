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

"""Unit tests for the native ``adb shell input text`` fallback."""

from unittest.mock import MagicMock

import pytest

from artemis.drivers.android.adb_driver import AndroidAdbDriver, _adb_input_text_commands
from artemis.drivers.android.input_ime import AndroidInputIME


def _typed_by_input_text(commands: list[str]) -> str:
    """Reproduce what Android's ``input text`` types for each command, then join."""
    typed = []
    for cmd in commands:
        arg = cmd.removeprefix("input text ")
        out = []
        i = 0
        while i < len(arg):
            if arg[i] == "\\" and i + 1 < len(arg):
                out.append(arg[i + 1])
                i += 2
            elif arg[i] == "%" and i + 1 < len(arg) and arg[i + 1] == "s":
                out.append(" ")
                i += 2
            else:
                out.append(arg[i])
                i += 1
        typed.append("".join(out))
    return "".join(typed)


@pytest.mark.parametrize(
    "line",
    [
        "Hello world",
        "Hello %s",
        "50%sale",
        "%s",
        "a%s%sb",
        "100% sure",
        "%%s literal",
        "ends with %",
    ],
)
def test_input_text_commands_type_the_line_literally(line):
    assert _typed_by_input_text(_adb_input_text_commands(line)) == line


def test_plain_text_stays_a_single_command():
    assert _adb_input_text_commands("Hello world") == ["input text Hello%sworld"]


def test_literal_percent_s_is_split_across_commands():
    assert _adb_input_text_commands("Hello %s") == [
        "input text Hello%s%",
        "input text s",
    ]


@pytest.mark.asyncio
async def test_driver_native_fallback_keeps_literal_percent_s():
    mock_adb_client = MagicMock()
    mock_adb_device = MagicMock()
    mock_adb_client.device.return_value = mock_adb_device
    mock_adb_device.shell.return_value = "com.google.android.inputmethod.latin/.LatinIME"

    driver = AndroidAdbDriver(device_id="emulator-5554", adb_client=mock_adb_client)

    assert await driver.input_text("Price: 50%sale", clear_existing=False) is True

    commands = [
        c.args[0]
        for c in mock_adb_device.shell.call_args_list
        if c.args[0].startswith("input text")
    ]
    assert _typed_by_input_text(commands) == "Price: 50%sale"


@pytest.mark.asyncio
async def test_ime_ascii_fast_path_keeps_literal_percent_s():
    mock_adb_device = MagicMock()
    ime = AndroidInputIME(mock_adb_device)

    assert await ime.type_text("Hello %s", clear_existing=False) is True

    commands = [
        c.args[0]
        for c in mock_adb_device.shell.call_args_list
        if c.args[0].startswith("input text")
    ]
    assert _typed_by_input_text(commands) == "Hello %s"
