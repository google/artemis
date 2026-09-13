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

"""Safe wrappers around ``adb`` and ``adb shell`` invocations.

Issue #55: the Android driver and MCP actuator previously concatenated
``package_name`` and ``url`` arguments directly into a string passed to
``device.shell(...)`` and ``am start -d '...'``. That let a hostile or
malformed caller inject shell metacharacters (``;``, ``|``, ``$()``,
backticks, redirection, ...) and execute arbitrary commands inside the
device's ADB session.

This module centralises the fix:

* ``validate_package_name`` enforces ``^[A-Za-z0-9_.]+$`` (the legal
  Android package-name character set) so ``package_name`` can never carry
  a shell metacharacter or a leading hyphen that would smuggle a flag.
* ``validate_url`` rejects ``url`` values containing whitespace, control
  bytes or any of the POSIX shell command-chaining metacharacters
  (``;|<>$()``` and quotes). Legitimate URL syntax (``?``, ``&``, ``=``,
  ``%XX``, ``#``, ``:``, ``/``, ``.``, alphanumerics, ``+``, ``~``,
  ``@``, ...) is preserved.
* ``run_adb_shell`` and ``run_adb_shell_async`` build an argv list and
  hand it to ``subprocess.run`` / ``asyncio.create_subprocess_exec``
  with ``shell=False`` so the OS exec's the ``adb`` binary directly
  without invoking a local shell. ``adb`` itself then concatenates the
  args into a single remote-shell command line, which is exactly what
  we want — the metacharacters were only ever dangerous because the
  *local* Python process was previously passing the string to
  ``/bin/sh -c``.

The wrapper preserves the historical return type (a ``str`` of remote
stdout/stderr) so callers like ``AndroidAdbDriver.execute_shell`` continue
to work without a behavioural change for safe inputs.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import subprocess
from typing import Sequence

# Android package names follow the Java identifier rules plus dots; no
# uppercase requirement, no colons, slashes, spaces, or anything the local
# shell would interpret. Anchored so ``-p android.intent.category.LAUNCHER``
# cannot smuggle an ``-e`` flag through a leading hyphen either.
PACKAGE_NAME_RE = re.compile(r"^[A-Za-z0-9_.]+$")

# True POSIX shell command-chaining metacharacters plus the URL forms
# that would let a caller smuggle a second command to ``adb shell``
# (which itself pipes the concatenated arg list through the device's
# ``/system/bin/sh``). Legitimate URL syntax (``?``, ``&``, ``=``,
# ``%XX``, ``#``, ``:``, ``/``, ``.``, ``+``, ``~``, ``@``, ``!``,
# ``*``, ``,``, alphanumerics, hyphens, underscores) is preserved; ``&``
# is the ``?a=1&b=2`` query separator and is therefore *not* in this
# set. Whitespace (space, tab, newline, etc.) and all C0/C1 control
# bytes are forbidden.
URL_FORBIDDEN_RE = re.compile(r"""[;|<>`$()\\\s'"\x00-\x1f\x7f]""")


class UnsafeShellArgumentError(ValueError):
    """Raised when a caller hands the safe wrapper an unsafe ``package_name`` or ``url``."""


def validate_package_name(package_name: str) -> str:
    """Return ``package_name`` unchanged when it is a safe Android package id.

    Raises :class:`UnsafeShellArgumentError` if it contains any character
    outside ``[A-Za-z0-9_.]`` or is empty. The check is anchored so a value
    like ``"; rm -rf /"`` or ``"-e FLAG"`` is rejected as well as obvious
    injection payloads.
    """
    if not isinstance(package_name, str) or not package_name:
        raise UnsafeShellArgumentError("package_name must be a non-empty string")
    if not PACKAGE_NAME_RE.fullmatch(package_name):
        raise UnsafeShellArgumentError(
            f"package_name {package_name!r} contains characters outside [A-Za-z0-9_.]"
        )
    return package_name


def validate_url(url: str) -> str:
    """Return ``url`` unchanged when it is safe to pass to ``am start -d``.

    Rejects whitespace and shell metacharacters. The local-side check is
    what matters: ``adb`` itself only forwards the bytes to the device.
    """
    if not isinstance(url, str) or not url:
        raise UnsafeShellArgumentError("url must be a non-empty string")
    if URL_FORBIDDEN_RE.search(url):
        raise UnsafeShellArgumentError(
            f"url {url!r} contains a shell metacharacter or control byte"
        )
    return url


def _adb_argv(device_id: str | None, shell_args: Sequence[str]) -> list[str]:
    """Build the ``adb`` argv list for a device-targeted shell invocation.

    ``shell_args`` is the *remote* shell command, passed as discrete tokens
    so the local Python process never goes through ``/bin/sh``. ``device_id``
    is either a serial or ``None`` (use the single attached device).
    """
    if device_id is not None and not isinstance(device_id, str):
        raise UnsafeShellArgumentError("device_id must be a string or None")
    argv: list[str] = ["adb"]
    if device_id:
        argv += ["-s", device_id]
    argv += ["shell", *shell_args]
    return argv


def run_adb_shell(
    shell_args: Sequence[str],
    *,
    device_id: str | None = None,
    timeout_seconds: float | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run ``adb shell <shell_args>`` without ``shell=True``.

    ``shell_args`` is forwarded as discrete argv tokens to ``adb``; ``adb``
    concatenates them into a single remote command line. The result mirrors
    ``subprocess.run`` so callers can inspect stdout/stderr/returncode.
    """
    argv = _adb_argv(device_id, shell_args)
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=check,
        # Belt-and-braces: keep ``shell=False`` even if a future refactor
        # forgets it. The default already is False; this makes the intent
        # explicit and survives a careless refactor that injects
        # ``shell=some_flag``.
        shell=False,
        env=os.environ.copy(),
    )


async def run_adb_shell_async(
    shell_args: Sequence[str],
    *,
    device_id: str | None = None,
) -> str:
    """Async analogue of :func:`run_adb_shell` returning the remote stdout.

    The string return type matches the historical contract of
    ``AndroidAdbDriver.execute_shell`` so the call sites keep working.
    """
    argv = _adb_argv(device_id, shell_args)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        shell=False,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        # Mirror the historical "Error: ..." return type so callers don't
        # have to branch on a non-zero exit code.
        message = (stderr or b"").decode(errors="replace").strip() or (stdout or b"").decode(
            errors="replace"
        ).strip()
        return f"Error: {message or f'adb shell exited with code {proc.returncode}'}"
    return (stdout or b"").decode(errors="replace")


def quote_for_remote_shell(token: str) -> str:
    """Quote a single token for inclusion in a remote ``adb shell`` arg list.

    This is a thin convenience over :func:`shlex.quote` for callers that
    still want to build a *single* string argument (e.g. ``input text``
    payloads) while keeping the *list* form on the local side. The output is
    always wrapped in single quotes; callers must build the local argv as a
    list — never interpolate this into a shell command line.
    """
    return shlex.quote(token)
