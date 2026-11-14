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

"""Shared iOS observation lifecycle for one-shot native captures.

``mobile_get_device_state`` and the device smoke check both need a screen
capture from an iOS driver that was constructed lazily — no lease, no native
session.  This helper performs the complete, cancellation-safe lifecycle:
resolve the device identity, take the real iOS-scoped lease, connect, capture,
then disconnect and release in that order, even when the caller is cancelled.
"""

import asyncio
from typing import Any

from artemis.controllers.device_controller import ScreenDataResponse
from artemis.runtime.device_target import IOS_LOCK_SCOPE
from artemis.runtime.device_lock import DeviceExecutionLock
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

_DISCONNECT_ERRORS = (OSError, ValueError, RuntimeError, TimeoutError)


async def observe_ios_controller(controller: Any) -> ScreenDataResponse:
    """Capture screen data from an unconnected iOS controller.

    Acquires the ``ios``-scoped device lease before any native setup, connects
    the driver, captures, then always disconnects (when connect was attempted)
    and releases the lease — including on cancellation, where cleanup is
    shielded from the caller's CancelledError before it propagates.
    """
    driver = controller._driver
    resolved = await driver.resolve_device()
    controller.ctx.device.device_id = resolved
    lease = DeviceExecutionLock(
        resolved,
        description="Artemis iOS observation",
        ingress="mcp",
        concurrency_mode="per_device",
        lock_scope=IOS_LOCK_SCOPE,
    )
    connect_attempted = False

    async def _cleanup() -> None:
        try:
            if connect_attempted:
                try:
                    await driver.disconnect()
                except _DISCONNECT_ERRORS as exc:
                    logger.warning(f"iOS observation native disconnect failed: {exc}")
        finally:
            lease.release()

    acquire_task = asyncio.create_task(asyncio.to_thread(lease.acquire, blocking=False))
    try:
        try:
            # A cancelled waiter must still drain the acquisition thread: if the
            # lease landed concurrently, skipping the drain would leak it.
            await asyncio.shield(acquire_task)
        except asyncio.CancelledError:
            try:
                await acquire_task
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError, RuntimeError, TimeoutError) as drain_error:
                logger.debug(f"iOS observation lease drain failed while cancelling: {drain_error}")
            raise
        connect_attempted = True
        await driver.connect()
        data = await controller.get_screen_data()
        device = controller.ctx.device
        device.device_width = data.width
        device.device_height = data.height
        return data
    finally:
        cleanup = asyncio.ensure_future(_cleanup())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise
