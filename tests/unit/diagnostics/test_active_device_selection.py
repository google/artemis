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

"""The diagnostics report must describe the device the caller asked for."""

from dataclasses import dataclass

from artemis.core.diagnostics.probes.adb_probe import select_active_device


@dataclass
class _Dev:
    serial: str
    is_locked: bool | None


def test_requested_device_with_unknown_lock_state_still_wins() -> None:
    """An emulator whose lock state is undetermined must not lose to another phone."""
    phone = _Dev("4e42463151563398", False)
    emulator = _Dev("emulator-5554", None)
    assert select_active_device([phone, emulator], "emulator-5554") is emulator


def test_requested_unlocked_device_wins() -> None:
    phone = _Dev("4e42463151563398", False)
    emulator = _Dev("emulator-5554", False)
    assert select_active_device([phone, emulator], "emulator-5554") is emulator


def test_confirmed_locked_request_falls_back() -> None:
    phone = _Dev("4e42463151563398", False)
    emulator = _Dev("emulator-5554", True)
    assert select_active_device([phone, emulator], "emulator-5554") is phone


def test_no_request_prefers_confirmed_unlocked() -> None:
    locked = _Dev("a", True)
    unlocked = _Dev("b", False)
    assert select_active_device([locked, unlocked], None) is unlocked
