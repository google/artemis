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

"""Device Driver Factory & Registry."""

import os
from typing import TYPE_CHECKING

from adbutils import AdbClient

from artemis.config import settings
from artemis.drivers.android.adb_driver import AndroidAdbDriver
from artemis.drivers.base import BaseDeviceDriver
from artemis.drivers.mock.mock_driver import MockDeviceDriver
from third_party.mobile_use.utils.logger import get_logger

if TYPE_CHECKING:
    from artemis.context import ArtemisContext

logger = get_logger(__name__)


def ios_driver_class(device_id: str | None) -> type[BaseDeviceDriver]:
    """Pick the driver class for an iOS serial by device reality.

    A serial resolving to a CoreDevice physical entry maps to
    ``PhysicalIosDriver``; simulators, ``booted``, and unknown serials keep
    ``XcodeSimulatorDriver`` so its existing validation and error paths apply.
    """
    from artemis.drivers.ios.discovery import BOOTED_SIMULATOR_ID
    from artemis.drivers.ios.xcode_driver import XcodeSimulatorDriver

    if device_id and device_id.strip().lower() != BOOTED_SIMULATOR_ID:
        from artemis.drivers.ios.discovery import (
            device_matches_identifier,
            find_physical_ios_device_sync,
            list_ios_simulators_sync,
        )

        simulators = list_ios_simulators_sync()
        if simulators is None or not any(
            device_matches_identifier(device, device_id) for device in simulators
        ):
            if find_physical_ios_device_sync(device_id) is not None:
                from artemis.drivers.ios.physical_driver import PhysicalIosDriver

                return PhysicalIosDriver
    return XcodeSimulatorDriver


def _create_ios_driver(ctx: "ArtemisContext") -> BaseDeviceDriver:
    """Instantiate the simulator or physical-device driver for an iOS target."""
    if os.environ.get("ARTEMIS_CLOUD_MODE") == "1":
        raise ValueError("iOS support is local only; cloud mode targets Android.")
    config = getattr(ctx, "agent_config", None)
    workspace_path = getattr(config, "ios_workspace_path", None) if config is not None else None
    driver_class = ios_driver_class(ctx.device.device_id)
    return driver_class(
        device_id=ctx.device.device_id,
        width=ctx.device.device_width,
        height=ctx.device.device_height,
        workspace_path=workspace_path,
    )


def create_driver(ctx: "ArtemisContext") -> BaseDeviceDriver:
    """Instantiates the appropriate BaseDeviceDriver based on the runtime context."""
    mobile_platform = getattr(ctx.device, "mobile_platform", "android")
    if mobile_platform == "ios":
        return _create_ios_driver(ctx)
    if mobile_platform not in {"android", "mock"}:
        raise ValueError(f"Unsupported mobile platform: {mobile_platform}")

    # 1. Cloud mode check. Cloud devices are reached through the gateway's
    # RemoteUIAutomatorClient; ARTEMIS_HIERARCHY_BACKEND does not apply there
    # because the Accessibility Helper needs a local adb forward.
    if os.environ.get("ARTEMIS_CLOUD_MODE") == "1":
        if ctx.adb_client is None:
            from cloud_service.virtualization import RemoteAdbClient

            ctx.adb_client = RemoteAdbClient()
        if ctx.ui_adb_client is None:
            from cloud_service.virtualization import RemoteUIAutomatorClient

            ctx.ui_adb_client = RemoteUIAutomatorClient(adb_client=ctx.adb_client)

    # 2. Mock mode check
    if (
        getattr(ctx.device, "mobile_platform", None) == "mock"
        or os.environ.get("ARTEMIS_MOCK_DRIVER") == "1"
    ):
        return MockDeviceDriver(
            device_id=ctx.device.device_id if ctx.device else "mock-device",
            width=ctx.device.device_width if ctx.device else 1080,
            height=ctx.device.device_height if ctx.device else 2400,
        )

    # 3. Default Android ADB driver
    if ctx.adb_client is None:
        ctx.adb_client = AdbClient(
            host=settings.ADB_HOST or "localhost", port=settings.ADB_PORT or 5037
        )

    return AndroidAdbDriver(
        device_id=ctx.device.device_id,
        adb_client=ctx.adb_client,
        ui_adb_client=getattr(ctx, "ui_adb_client", None),
        width=ctx.device.device_width,
        height=ctx.device.device_height,
    )


def get_driver(ctx: "ArtemisContext") -> BaseDeviceDriver:
    """Cached accessor for device driver in the current context."""
    if not hasattr(ctx, "_active_driver") or getattr(ctx, "_active_driver") is None:
        driver = create_driver(ctx)
        setattr(ctx, "_active_driver", driver)
    return getattr(ctx, "_active_driver")
