# Copyright 2025-2026 Minitap, Inc.
# Modifications Copyright 2026 Google LLC
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
#
# Originally from mobile-use (https://github.com/minitap-ai/mobile-use).
# See third_party/mobile_use/METADATA for the upstream source and local modifications.

"""Device-control facade base: the upstream ``UnifiedMobileController`` API.

Artemis extends it as ``artemis.controllers.unified_controller.UnifiedMobileController``
with scrcpy video recording and timeline segment extraction.
"""

from typing import Any

from artemis.context import ArtemisContext
from artemis.controllers.device_controller import ScreenDataResponse
from artemis.drivers.base import BaseDeviceDriver
from third_party.mobile_use.controllers.types import ElementQuery, TapOutput


class UnifiedMobileControllerBase:
    def __init__(self, ctx: ArtemisContext, driver: BaseDeviceDriver):
        self.ctx = ctx
        self._driver: BaseDeviceDriver = driver

    @property
    def driver(self) -> BaseDeviceDriver:
        return self._driver

    @property
    def controller(self) -> Any:
        return self._driver

    async def tap_at(
        self,
        x: int,
        y: int,
        long_press: bool = False,
        long_press_duration: int = 1000,
        times: int = 1,
        delay_ms: int = 100,
    ) -> TapOutput:
        try:
            if long_press:
                success = await self._driver.long_press(x, y, duration_ms=long_press_duration)
            else:
                success = await self._driver.tap(
                    x, y, duration_ms=100, times=times, delay_ms=delay_ms
                )
            return TapOutput(error=None if success else f"Tap failed at ({x}, {y})")
        except Exception as e:
            return TapOutput(error=str(e))

    async def tap_percentage(
        self,
        x_percent: int,
        y_percent: int,
        long_press: bool = False,
        long_press_duration: int = 1000,
    ) -> TapOutput:
        """Tap at percentage-based coordinates (0 to 100)."""
        norm_x = int(x_percent * 10)
        norm_y = int(y_percent * 10)
        success = await self._driver.tap_normalized(
            norm_x, norm_y, long_press=long_press, duration_ms=long_press_duration
        )
        return TapOutput(
            error=None if success else f"Tap percentage failed at ({x_percent}%, {y_percent}%)"
        )

    async def tap_element(
        self,
        resource_id: str | None = None,
        text: str | None = None,
        index: int = 0,
        long_press: bool = False,
        long_press_duration: int = 1000,
    ) -> TapOutput:
        """Tap on a UI element by finding it in the hierarchy."""
        query = ElementQuery(resource_id=resource_id, text=text, index=index)
        success = await self._driver.tap_element(
            query, long_press=long_press, duration_ms=long_press_duration
        )
        return TapOutput(error=None if success else f"Failed to tap element ({query.describe()})")

    async def swipe_coords(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration: int = 400,
    ) -> str | None:
        """Swipe between two coordinate points."""
        success = await self._driver.swipe(start_x, start_y, end_x, end_y, duration_ms=duration)
        return None if success else f"Swipe failed from ({start_x},{start_y}) to ({end_x},{end_y})"

    async def type_text(self, text: str, clear_existing: bool = True) -> bool:
        return await self._driver.input_text(text, clear_existing=clear_existing)

    async def take_screenshot(self) -> str:
        screen_data = await self._driver.get_screen_data()
        return screen_data.screenshot_base64

    async def launch_app(self, package_or_bundle_id: str) -> bool:
        return await self._driver.launch_app(package_or_bundle_id)

    async def terminate_app(self, package_or_bundle_id: str | None) -> bool:
        if not package_or_bundle_id:
            return False
        return await self._driver.stop_app(package_or_bundle_id)

    async def open_url(self, url: str) -> bool:
        await self._driver.execute_shell(f"am start -a android.intent.action.VIEW -d '{url}'")
        return True

    async def go_back(self) -> bool:
        return await self._driver.press_key("back")

    async def go_home(self) -> bool:
        return await self._driver.press_key("home")

    async def press_enter(self) -> bool:
        return await self._driver.press_key("enter")

    async def press_key(self, keycode: str) -> bool:
        return await self._driver.press_key(keycode)

    async def erase_text(self, nb_chars: int | None = None) -> bool:
        if nb_chars is not None and nb_chars > 0:
            for _ in range(nb_chars):
                await self._driver.press_key("delete")
            return True
        # Best-effort full clear: End -> Ctrl+A -> Delete
        try:
            clear_cmd = (
                "input keyevent 123 && "
                "input keycombination 113 29 && input keyevent 67 && "
                "input keyevent 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67"
            )
            await self._driver.execute_shell(clear_cmd)
        except Exception:
            for _ in range(30):
                await self._driver.press_key("delete")
        return True

    async def get_ui_elements(self) -> list[dict]:
        screen_data = await self._driver.get_screen_data()
        return screen_data.ui_elements or []

    async def get_screen_data(self) -> "ScreenDataResponse":
        """Get screen data including screenshot, UI hierarchy, dimensions, and platform."""
        data = await self._driver.get_screen_data()
        return ScreenDataResponse(
            base64=data.screenshot_base64,
            elements=data.ui_elements or [],
            width=data.width,
            height=data.height,
            platform=data.platform,
        )

    async def find_element(
        self,
        resource_id: str | None = None,
        text: str | None = None,
        index: int = 0,
    ) -> tuple[dict | None, str | None]:
        elem, _, error = await self._driver.find_element(
            ElementQuery(resource_id=resource_id, text=text, index=index)
        )
        return elem, error
