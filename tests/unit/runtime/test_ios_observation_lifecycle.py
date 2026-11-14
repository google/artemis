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

"""Lifecycle tests for the shared one-shot iOS observation helper.

``observe_ios_controller`` must acquire the real iOS-scoped lease before any
native setup, and must always disconnect + release — including on failure and
cancellation. All native calls are stubbed; only the lock registry is real
(isolated per test via ``get_temp_dir``).
"""

import asyncio
import base64
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from artemis.runtime.device_target import IOS_LOCK_SCOPE
from artemis.runtime.device_lock import DeviceBusyError, DeviceExecutionLock
from artemis.runtime.ios_observation import observe_ios_controller

UDID = "AAAA-1111-0000"


@pytest.fixture(autouse=True)
def isolated_lock_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("artemis.runtime.device_lock.get_temp_dir", lambda *a, **k: tmp_path)


def _screen_data():
    return SimpleNamespace(
        base64=base64.b64encode(b"\x89PNG" + bytes(600)).decode("ascii"),
        elements='<hierarchy><node class="XCUIElementTypeButton"/></hierarchy>',
        width=1170,
        height=2532,
    )


def _controller(events, *, connect_error=None, capture_error=None):
    """A lazy iOS controller whose driver records its call order."""

    async def resolve_device():
        events.append("resolve")
        return UDID

    async def connect():
        events.append("connect")
        if connect_error is not None:
            raise connect_error

    async def disconnect():
        events.append("disconnect")

    async def capture():
        events.append("capture")
        if capture_error is not None:
            raise capture_error
        return _screen_data()

    driver = SimpleNamespace(resolve_device=resolve_device, connect=connect, disconnect=disconnect)
    device = SimpleNamespace(device_id="booted", device_width=0, device_height=0)
    return SimpleNamespace(
        _driver=driver, ctx=SimpleNamespace(device=device), get_screen_data=capture
    )


def _ios_owner(device_id: str = UDID):
    owners = DeviceExecutionLock.get_active_owners()
    return next(
        (o for o in owners.values() if o.device_id == device_id and o.lock_scope == IOS_LOCK_SCOPE),
        None,
    )


@pytest.mark.asyncio
async def test_observation_orders_resolve_lease_connect_capture_cleanup():
    events: list[str] = []
    controller = _controller(events)

    data = await observe_ios_controller(controller)

    assert events == ["resolve", "connect", "capture", "disconnect"]
    assert data.width == 1170
    # Real metrics and the resolved UDID land on the context.
    assert controller.ctx.device.device_id == UDID
    assert controller.ctx.device.device_width == 1170
    assert controller.ctx.device.device_height == 2532
    # The lease is released after disconnect.
    assert _ios_owner() is None


@pytest.mark.asyncio
async def test_observation_holds_ios_lease_during_capture():
    events: list[str] = []
    controller = _controller(events)
    held: list[bool] = []

    original_capture = controller.get_screen_data

    async def capture():
        held.append(_ios_owner() is not None)
        return await original_capture()

    controller.get_screen_data = capture
    await observe_ios_controller(controller)
    assert held == [True]


@pytest.mark.asyncio
async def test_held_ios_lease_blocks_observation_before_connect():
    events: list[str] = []
    controller = _controller(events)
    held = DeviceExecutionLock(
        UDID,
        "running iOS task",
        ingress="mcp",
        concurrency_mode="per_device",
        lock_scope=IOS_LOCK_SCOPE,
    )
    held.acquire(blocking=False)
    try:
        with pytest.raises(DeviceBusyError):
            await observe_ios_controller(controller)
    finally:
        held.release()
    assert "connect" not in events
    assert _ios_owner() is None


@pytest.mark.asyncio
async def test_android_lock_on_same_text_does_not_block_ios():
    """Identical serial text in the Android scope is a different device."""
    events: list[str] = []
    controller = _controller(events)
    android_lock = DeviceExecutionLock(UDID, "android task")
    android_lock.acquire(blocking=False)
    try:
        await observe_ios_controller(controller)
    finally:
        android_lock.release()
    assert events == ["resolve", "connect", "capture", "disconnect"]


@pytest.mark.asyncio
async def test_capture_failure_disconnects_and_releases():
    events: list[str] = []
    controller = _controller(events, capture_error=RuntimeError("capture blew up"))

    with pytest.raises(RuntimeError, match="capture blew up"):
        await observe_ios_controller(controller)
    assert events == ["resolve", "connect", "capture", "disconnect"]
    assert _ios_owner() is None


@pytest.mark.asyncio
async def test_connect_failure_still_disconnects_and_releases():
    """A half-open native session is closed when connect was attempted."""
    events: list[str] = []
    controller = _controller(events, connect_error=RuntimeError("bridge down"))

    with pytest.raises(RuntimeError, match="bridge down"):
        await observe_ios_controller(controller)
    assert events == ["resolve", "connect", "disconnect"]
    assert _ios_owner() is None


@pytest.mark.asyncio
async def test_disconnect_error_does_not_mask_capture_failure():
    events: list[str] = []
    controller = _controller(events, capture_error=RuntimeError("native lost"))

    async def disconnect():
        events.append("disconnect")
        raise RuntimeError("disconnect failed")

    controller._driver.disconnect = disconnect
    with pytest.raises(RuntimeError, match="native lost"):
        await observe_ios_controller(controller)
    assert _ios_owner() is None


@pytest.mark.asyncio
async def test_cancellation_still_disconnects_and_releases():
    events: list[str] = []
    controller = _controller(events)
    started = asyncio.Event()

    async def capture():
        events.append("capture")
        started.set()
        await asyncio.Event().wait()  # hang until cancelled

    controller.get_screen_data = capture
    task = asyncio.create_task(observe_ios_controller(controller))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == ["resolve", "connect", "capture", "disconnect"]
    assert _ios_owner() is None


@pytest.mark.asyncio
async def test_busy_acquire_failure_never_connects():
    """A failed non-blocking acquire must not touch the native session."""
    events: list[str] = []
    controller = _controller(events)
    held = DeviceExecutionLock(
        UDID, "owner", concurrency_mode="per_device", lock_scope=IOS_LOCK_SCOPE
    )
    held.acquire(blocking=False)
    try:
        with pytest.raises(DeviceBusyError):
            await observe_ios_controller(controller)
    finally:
        held.release()
    assert events == ["resolve"]
    assert controller.ctx.device.device_id == UDID


@pytest.mark.asyncio
async def test_cancelled_acquire_is_drained_and_never_connects(monkeypatch):
    """Cancelling while lease.acquire runs must wait for the gated thread.

    The acquisition eventually lands the real isolated lock; the observer
    must not propagate until the thread drains, never connect, and leave no
    owned lease behind.
    """
    events: list[str] = []
    controller = _controller(events)
    gate = threading.Event()
    entered = threading.Event()
    real_acquire = DeviceExecutionLock.acquire

    def gated_acquire(self, blocking=True):
        entered.set()
        gate.wait(timeout=10)
        return real_acquire(self, blocking=blocking)

    monkeypatch.setattr(DeviceExecutionLock, "acquire", gated_acquire)
    task = asyncio.create_task(observe_ios_controller(controller))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)  # let the cancellation land
        assert not task.done()  # still draining the gated acquire thread
    finally:
        gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Cleanup released the landed lease; connect was never attempted.
    assert events == ["resolve"]
    assert _ios_owner() is None
