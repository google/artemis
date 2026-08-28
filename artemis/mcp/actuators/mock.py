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

"""Mock actuator for tests: real actuator code paths over an in-memory device.

Subclasses :class:`AdbActuator` deliberately, so tests exercise the same coordinate
conversion and controller dispatch as production -- only the driver is in-memory.
``capabilities``/``extensions`` are overridable to model partial and extended
backends.
"""

from artemis.context import ArtemisContext, DeviceContext, DevicePlatform
from artemis.drivers.mock.mock_driver import MockDeviceDriver
from artemis.mcp.action_manifest import DEVICE_ACTIONS, ExtensionTool
from artemis.mcp.actuators.adb import AdbActuator
from artemis.platform import platform

__all__ = ["MockActuator"]


class MockActuator(AdbActuator):
    """An :class:`AdbActuator` bound to a :class:`MockDeviceDriver`."""

    def __init__(
        self,
        width: int = 1080,
        height: int = 2400,
        capabilities: frozenset[str] | None = None,
        extensions: list[ExtensionTool] | None = None,
    ):
        driver = MockDeviceDriver(width=width, height=height)
        ctx = ArtemisContext(
            trace_id="mock-actuator",
            device=DeviceContext(
                host_platform=platform.os_type.name,
                mobile_platform=DevicePlatform.ANDROID,
                device_id=driver.device_id,
                device_width=width,
                device_height=height,
            ),
        )
        # get_driver(ctx) returns a cached `_active_driver` when present
        # (artemis/drivers/factory.py), the established test seam.
        ctx._active_driver = driver
        super().__init__(ctx)
        self.driver = driver
        self._capabilities = capabilities if capabilities is not None else DEVICE_ACTIONS
        self._extensions = list(extensions or [])

    def capabilities(self) -> frozenset[str]:
        return self._capabilities

    def extensions(self) -> list[ExtensionTool]:
        return self._extensions

    @property
    def action_history(self) -> list[dict]:
        """The mock driver's recorded actions, for assertions."""
        return self.driver.action_history
