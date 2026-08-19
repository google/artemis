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

import json

import pytest

from artemis.runtime.device_lock import DeviceBusyError, DeviceExecutionLock


@pytest.fixture(autouse=True)
def isolated_lock_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "artemis.runtime.device_lock.get_temp_dir",
        lambda _name: tmp_path,
    )


def test_device_lock_blocks_a_second_process_owner():
    first = DeviceExecutionLock("emulator-5554", "first task")
    second = DeviceExecutionLock("emulator-5554", "second task")

    first.acquire()
    try:
        with pytest.raises(DeviceBusyError, match="first task"):
            second.acquire()
    finally:
        first.release()


def test_device_lock_recovers_stale_owner(monkeypatch):
    lock = DeviceExecutionLock("emulator-5554", "replacement task")
    lock.path.write_text(
        json.dumps(
            {
                "pid": 999999,
                "process_created_at": 1.0,
                "token": "stale-token",
                "device_id": "emulator-5554",
                "description": "stale task",
                "acquired_at": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(DeviceExecutionLock, "_owner_is_alive", staticmethod(lambda _owner: False))

    lock.acquire()
    try:
        assert lock.path.exists()
    finally:
        lock.release()

    assert not lock.path.exists()


def test_device_lock_release_does_not_remove_replacement_owner():
    lock = DeviceExecutionLock("emulator-5554", "original task")
    lock.acquire()
    replacement = json.loads(lock.path.read_text(encoding="utf-8"))
    replacement["token"] = "replacement-token"
    lock.path.write_text(json.dumps(replacement), encoding="utf-8")

    lock.release()

    assert lock.path.exists()


def test_cleanup_stale_locks_keeps_live_owner(monkeypatch):
    live = DeviceExecutionLock("emulator-5554", "live task")
    stale = DeviceExecutionLock("physical-device", "stale task")
    live.acquire()
    stale.path.write_text(
        json.dumps(
            {
                "pid": 999999,
                "process_created_at": 1.0,
                "token": "stale-token",
                "device_id": "physical-device",
                "description": "stale task",
                "acquired_at": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    original_owner_is_alive = DeviceExecutionLock._owner_is_alive

    def owner_is_alive(owner):
        if owner.pid == 999999:
            return False
        return original_owner_is_alive(owner)

    monkeypatch.setattr(DeviceExecutionLock, "_owner_is_alive", staticmethod(owner_is_alive))
    try:
        assert DeviceExecutionLock.cleanup_stale_locks() == 1
        assert live.path.exists()
        assert not stale.path.exists()
    finally:
        live.release()
