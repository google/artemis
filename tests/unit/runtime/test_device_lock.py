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
import threading
import time

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
            second.acquire(blocking=False)
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
    live.acquire()
    stale_path = live.path.parent / "stale-device.lock"
    stale_path.write_text(
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
        assert not stale_path.exists()
    finally:
        live.release()


def test_device_lock_waits_and_runs_next_owner_in_fifo_order():
    first = DeviceExecutionLock("emulator-5554", "first task")
    second = DeviceExecutionLock("emulator-5554", "second task")
    acquired = threading.Event()

    first.acquire()

    def acquire_second():
        second.acquire()
        acquired.set()

    thread = threading.Thread(target=acquire_second)
    thread.start()
    time.sleep(0.15)
    assert not acquired.is_set()

    first.release()
    assert acquired.wait(timeout=2.0)
    second.release()
    thread.join(timeout=2.0)


def test_submission_reservations_preserve_order_before_workers_start():
    first_ticket = DeviceExecutionLock.reserve("first submitted task")
    second_ticket = DeviceExecutionLock.reserve("second submitted task")
    second = DeviceExecutionLock("emulator-5554", "second task", second_ticket)
    second_acquired = threading.Event()

    def acquire_second():
        second.acquire()
        second_acquired.set()

    thread = threading.Thread(target=acquire_second)
    thread.start()
    time.sleep(0.15)
    assert not second_acquired.is_set()

    first = DeviceExecutionLock("emulator-5554", "first task", first_ticket)
    first.acquire()
    first.release()

    assert second_acquired.wait(timeout=2.0)
    second.release()
    thread.join(timeout=2.0)


def test_cancelled_reservation_cannot_execute():
    ticket = DeviceExecutionLock.reserve("cancelled task")
    assert DeviceExecutionLock.cancel_reservation(ticket)

    lock = DeviceExecutionLock("emulator-5554", "cancelled task", ticket)
    with pytest.raises(DeviceBusyError, match="cancelled or expired"):
        lock.acquire()


def test_reservation_can_be_transferred_to_worker_process():
    ticket = DeviceExecutionLock.reserve("submitted task")

    assert DeviceExecutionLock.transfer_reservation(ticket, 4242, description="worker task")

    path = next((DeviceExecutionLock("device").queue_dir).glob(f"*-{ticket}.wait"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["pid"] == 4242
    assert payload["description"] == "worker task"


def test_waiting_acquire_can_be_cancelled_without_leaving_a_ticket():
    owner = DeviceExecutionLock("emulator-5554", "owner")
    waiter = DeviceExecutionLock("emulator-5554", "waiter")
    cancel_event = threading.Event()
    errors = []
    owner.acquire()

    def wait_for_device():
        try:
            waiter.acquire(cancel_event=cancel_event)
        except DeviceBusyError as exc:
            errors.append(str(exc))

    thread = threading.Thread(target=wait_for_device)
    thread.start()
    time.sleep(0.15)
    cancel_event.set()
    thread.join(timeout=2.0)

    try:
        assert errors == ["Waiting for the Artemis device queue was cancelled."]
        assert list(waiter.queue_dir.glob("*.wait")) == []
    finally:
        owner.release()


def test_active_owner_is_discoverable_and_can_be_annotated(monkeypatch):
    monkeypatch.setenv("ARTEMIS_TASK_INGRESS", "cli")
    monkeypatch.delenv("ARTEMIS_SESSION_ID", raising=False)
    owner_lock = DeviceExecutionLock("emulator-5554", "CLI task")
    owner_lock.acquire()
    try:
        owner = DeviceExecutionLock.get_active_owner()
        assert owner is not None
        assert owner.pid == owner_lock._read_owner(owner_lock.path).pid
        assert owner.ingress == "cli"
        assert owner.session_id is None
        assert DeviceExecutionLock.is_active_owner(owner)

        assert DeviceExecutionLock.annotate_active_owner(
            session_id="cli-session",
            ingress="cli",
        )
        annotated = DeviceExecutionLock.get_active_owner()
        assert annotated is not None
        assert annotated.session_id == "cli-session"
        assert annotated.ingress == "cli"
    finally:
        owner_lock.release()
