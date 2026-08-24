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

from artemis.runtime.awake_lease import ScreenAwakeLease


@pytest.fixture(autouse=True)
def isolated_awake_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "artemis.runtime.awake_lease.get_temp_dir",
        lambda _name: tmp_path,
    )


def test_first_lease_drains_legacy_references_and_acquires_exactly_one(monkeypatch):
    lease = ScreenAwakeLease("device-123")
    count = 3

    def state():
        return (count > 0, count)

    def set_lock(action):
        nonlocal count
        count += 1 if action == "acquire" else -1
        return True

    monkeypatch.setattr(lease, "_wake_lock_state", state)
    monkeypatch.setattr(lease, "_set_wake_lock", set_lock)

    assert lease.acquire()
    assert count == 1
    assert lease.lease_path.exists()

    lease.release()
    assert count == 0


def test_overlapping_clients_share_one_android_reference(monkeypatch):
    first = ScreenAwakeLease("device-123")
    second = ScreenAwakeLease("device-123")
    count = 0

    def state():
        return (count > 0, count)

    def set_lock(action):
        nonlocal count
        count += 1 if action == "acquire" else -1
        return True

    for lease in (first, second):
        monkeypatch.setattr(lease, "_wake_lock_state", state)
        monkeypatch.setattr(lease, "_set_wake_lock", set_lock)

    assert first.acquire()
    assert second.acquire()
    assert count == 1

    first.release()
    assert count == 1
    second.release()
    assert count == 0


def test_crashed_client_is_pruned_and_legacy_reference_is_recovered(monkeypatch):
    crashed = ScreenAwakeLease("device-123")
    crashed.lease_dir.mkdir(parents=True, exist_ok=True)
    crashed.lease_path.write_text(
        json.dumps(
            {
                "pid": 999999,
                "process_created_at": 1.0,
                "token": crashed.token,
                "device_id": "device-123",
            }
        ),
        encoding="utf-8",
    )
    replacement = ScreenAwakeLease("device-123")
    count = 1

    def state():
        return (count > 0, count)

    def set_lock(action):
        nonlocal count
        count += 1 if action == "acquire" else -1
        return True

    monkeypatch.setattr(replacement, "_wake_lock_state", state)
    monkeypatch.setattr(replacement, "_set_wake_lock", set_lock)
    monkeypatch.setattr(
        ScreenAwakeLease,
        "_owner_is_alive",
        staticmethod(lambda payload: int(payload["pid"]) != 999999),
    )

    assert replacement.acquire()
    assert not crashed.lease_path.exists()
    assert count == 1

    replacement.release()
    assert count == 0


def test_migration_cleanup_never_drains_a_live_legacy_owner(monkeypatch):
    owner = ScreenAwakeLease("device-123")
    owner.lease_dir.mkdir(parents=True, exist_ok=True)
    owner.lease_path.write_text(json.dumps(owner._payload()), encoding="utf-8")
    cleanup = ScreenAwakeLease("device-123")
    cleanup._drain_references = lambda: pytest.fail("must not drain a live owner")

    assert cleanup.cleanup_unowned_references() is False
