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

"""Validation helpers for values interpolated into on-device shell commands.

`adb shell "<cmd>"` is parsed by the device's own shell, so any shell
metacharacters (``;``, ``&&``, ``$()``, backticks, ...) in interpolated
values execute on the connected device. These helpers are the structural
guard for the MCP -> controller -> driver path (issue #55).
"""

from __future__ import annotations

import re
import shlex

# Android package-name grammar: at least two dot-separated components, each
# starting with a letter followed by letters/digits/underscores.
# See https://developer.android.com/build/configure-app-module#set-application-id
PACKAGE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$")

# Android keycodes are small non-negative integers (KEYCODE_* < ~300 today).
# 1000 is a generous upper bound that still blocks arbitrary string injection.
MAX_KEYCODE = 1000


def is_valid_package_name(package_name: object) -> bool:
    """Return True iff `package_name` matches Android's package-name grammar."""
    if not isinstance(package_name, str):
        return False
    if len(package_name) > 255:
        return False
    return PACKAGE_NAME_RE.match(package_name) is not None


def require_valid_package_name(package_name: str) -> str:
    """Return stripped package name or raise ValueError if it is unsafe."""
    if not is_valid_package_name(
        package_name.strip() if isinstance(package_name, str) else package_name
    ):
        raise ValueError(f"Invalid Android package name: {package_name!r}")
    return package_name.strip()


def quote_url_for_adb(url: str) -> str:
    """Quote `url` for safe interpolation into a single `adb shell` argument.

    Uses `shlex.quote` (POSIX quoting understood by Android's shell) instead
    of manual single-quote wrapping, which breaks on embedded quotes.
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError(f"Invalid URL: {url!r}")
    cleaned = url.strip()
    if len(cleaned) > 4096:
        raise ValueError(f"Invalid URL (too long): {cleaned[:64]!r}...")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        raise ValueError(f"Invalid URL (control characters): {cleaned[:64]!r}")
    return shlex.quote(cleaned)


def coerce_keycode(key: object, keymap: dict[str, int]) -> int | None:
    """Resolve user-supplied key to an integer keycode, or None if unsafe.

    Accepts KeyCode enum members, known names (with or without KEYCODE_
    prefix, case-insensitive), and plain integer strings/ints in range.
    Anything else (e.g. "4; reboot") returns None instead of passing
    through raw into `input keyevent`.
    """
    if hasattr(key, "value"):
        try:
            key = key.value  # type: ignore[assignment]
        except Exception:
            return None
    if isinstance(key, bool):
        return None
    if isinstance(key, int):
        return key if 0 <= key <= MAX_KEYCODE else None
    if not isinstance(key, str):
        return None
    normalized = key.strip().lower().replace("keycode.", "").replace("keycode_", "")
    if normalized in keymap:
        return keymap[normalized]
    # Bare integer strings such as "123" (KEYCODE_MOVE_END) are legitimate.
    if re.fullmatch(r"\d{1,4}", normalized):
        try:
            value = int(normalized)
        except ValueError:
            return None
        return value if 0 <= value <= MAX_KEYCODE else None
    return None


def coerce_coord(value: object) -> int | None:
    """Coerce tap/swipe coordinate to int, or None if not a plain integer."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        if re.fullmatch(r"-?\d+", value.strip()):
            try:
                return int(value.strip())
            except ValueError:
                return None
        return None
    return None
