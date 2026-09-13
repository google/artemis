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
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for ``artemis.utils.safe_adb`` (Issue #55 mitigation)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from artemis.utils.safe_adb import (
    UnsafeShellArgumentError,
    run_adb_shell,
    validate_package_name,
    validate_url,
)


# ``validate_package_name`` ---------------------------------------------------


@pytest.mark.parametrize(
    "package_name",
    [
        "com.android.settings",
        "com.example.app_v2",
        "a",
        "A.B.C_0",
    ],
)
def test_validate_package_name_accepts_legal_ids(package_name: str) -> None:
    assert validate_package_name(package_name) == package_name


@pytest.mark.parametrize(
    "package_name",
    [
        "",  # empty
        "; rm -rf /",
        "$(whoami)",
        "`id`",
        "com.android.settings; rm",
        "com.android.settings && rm",
        "com.android.settings|rm",
        "com.android.settings\nrm",
        '"com.android.settings"',
        "'com.android.settings'",
        "-e FLAG",  # leading hyphen would smuggle a flag
        "a b",  # whitespace
        "a/b",  # not a legal Android id char
        "a:b",
        "a\\b",
    ],
)
def test_validate_package_name_rejects_unsafe(package_name: str) -> None:
    with pytest.raises(UnsafeShellArgumentError):
        validate_package_name(package_name)


def test_validate_package_name_rejects_non_string() -> None:
    with pytest.raises(UnsafeShellArgumentError):
        validate_package_name(None)  # type: ignore[arg-type]


# ``validate_url`` -----------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://example.com:8080/path",
        "https://example.com/path?d=1&e=2",  # & is legitimate query separator
        "https://example.com/path#fragment",
        "https://user:pass@example.com/",
        "file:///tmp/recording.json",
    ],
)
def test_validate_url_accepts_legal_urls(url: str) -> None:
    assert validate_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://example.com;rm",
        "https://example.com|rm",
        "https://example.com`id`",
        "https://example.com$(id)",
        "https://example.com > /etc/passwd",
        "https://example.com < /etc/passwd",
        "https://example.com\nrm",
        "https://example.com\trm",
        "https://example.com\\rm",
        "https://example.com'OR''",
        'https://example.com"OR""',
        "a b",
    ],
)
def test_validate_url_rejects_shell_metacharacters(url: str) -> None:
    with pytest.raises(UnsafeShellArgumentError):
        validate_url(url)


def test_validate_url_rejects_non_string() -> None:
    with pytest.raises(UnsafeShellArgumentError):
        validate_url(None)  # type: ignore[arg-type]


# ``run_adb_shell`` -----------------------------------------------------------


def test_run_adb_shell_forwards_argv_list_without_shell_true() -> None:
    """The wrapper must hand a list to ``subprocess.run`` with ``shell=False``.

    This is the core of the Issue #55 fix: a hostile ``shell_args`` value
    like ``["a", ";", "rm", "-rf", "/"]`` is delivered to ``adb`` as five
    discrete argv tokens. ``adb`` joins them into a single remote command
    line, but the *local* Python process never sees ``/bin/sh -c`` so
    there is no shell to inject into.
    """
    sentinel = object()
    with patch("artemis.utils.safe_adb.subprocess.run", return_value=sentinel) as run:
        result = run_adb_shell(
            ["monkey", "-p", "com.android.settings", "-c", "android.intent.category.LAUNCHER", "1"],
            device_id="emulator-5554",
            timeout_seconds=10.0,
        )
    assert result is sentinel
    # First positional arg to ``subprocess.run`` is the argv list.
    argv = run.call_args.args[0]
    # argv is a list (not a string) and never goes through a local shell
    assert argv == [
        "adb",
        "-s",
        "emulator-5554",
        "shell",
        "monkey",
        "-p",
        "com.android.settings",
        "-c",
        "android.intent.category.LAUNCHER",
        "1",
    ]
    assert run.call_args.kwargs["shell"] is False
    assert run.call_args.kwargs["timeout"] == 10.0


def test_run_adb_shell_without_device_id_omits_serial_flag() -> None:
    with patch("artemis.utils.safe_adb.subprocess.run") as run:
        run_adb_shell(["echo", "hello"])
    argv = run.call_args.args[0]
    assert argv[0] == "adb"
    assert "-s" not in argv
    assert argv[argv.index("shell") + 1 :] == ["echo", "hello"]
