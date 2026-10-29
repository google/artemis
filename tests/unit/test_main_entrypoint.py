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

"""Tests for argv handling in ``python -m artemis.main``.

Only a free-text goal is rewritten into ``run <goal>``; registered commands
and root options are passed through unchanged.
"""

import pytest
import typer

from artemis import main as legacy_main
from artemis.interfaces.cli.main import app


@pytest.mark.parametrize(
    "token",
    [
        "status",
        "ui",
        "stop",
        "restart",
        "helper",
        "run",
        "init",
        "doctor",
        "batch",
        "mcp",
        "server",
        "trace",
        "--version",
        "-v",
        "--help",
        "-h",
    ],
)
def test_known_commands_and_flags_are_not_rewritten(token):
    argv = ["artemis", token, "extra"]
    assert legacy_main.normalize_argv(argv) == argv


@pytest.mark.parametrize(
    "argv",
    [
        ["artemis", "Open Settings and enable dark mode"],
        ["artemis", "status of my battery"],
        ["artemis", "--device", "emulator-5554", "open settings"],
    ],
)
def test_free_text_goal_is_rewritten_to_run(argv):
    assert legacy_main.normalize_argv(argv) == [argv[0], "run", *argv[1:]]


def test_no_args_is_left_alone():
    assert legacy_main.normalize_argv(["artemis"]) == ["artemis"]


def test_every_registered_command_is_known():
    """Every command registered on the Typer app is a known root token."""
    registered = set(typer.main.get_command(app).commands)
    assert registered
    assert registered <= legacy_main._root_tokens()


def test_cli_status_dispatches_status_not_run(monkeypatch):
    calls = []

    def fake_app():
        calls.append(list(legacy_main.sys.argv))

    monkeypatch.setattr(legacy_main, "app", fake_app)
    monkeypatch.setattr(legacy_main.sys, "argv", ["artemis", "status"])
    legacy_main.cli()
    assert calls == [["artemis", "status"]]

    monkeypatch.setattr(legacy_main.sys, "argv", ["artemis", "open settings"])
    legacy_main.cli()
    assert calls[-1] == ["artemis", "run", "open settings"]
