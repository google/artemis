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

"""Universal Base Device Driver Protocol.

Defines the contract for hardware and simulator device interaction,
abstracting low-level protocols (ADB, UIAutomator2, WebDriver, Cloud RPC)
behind a clean, unified async interface.
"""

from abc import ABC, abstractmethod
import asyncio
from enum import Enum
from pathlib import Path
import time
from typing import Any, Literal
from pydantic import BaseModel, Field


class SwipeDirection(str, Enum):
    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"


class KeyCode(str, Enum):
    HOME = "home"
    BACK = "back"
    ENTER = "enter"
    DELETE = "delete"
    POWER = "power"
    APP_SWITCH = "app_switch"
    VOLUME_UP = "volume_up"
    VOLUME_DOWN = "volume_down"


class ScreenData(BaseModel):
    """Encapsulates raw and parsed UI screen observation data."""

    screenshot_bytes: bytes = Field(..., description="Raw PNG/JPEG screenshot bytes")
    screenshot_base64: str = Field(..., description="Base64 encoded screenshot string")
    ui_hierarchy_xml: str | None = Field(default=None, description="Raw UI layout XML hierarchy")
    ui_elements: list[dict[str, Any]] = Field(
        default_factory=list, description="Parsed element node list"
    )
    width: int = Field(default=1080, description="Device screen width in pixels")
    height: int = Field(default=2400, description="Device screen height in pixels")
    timestamp: float = Field(default_factory=time.time, description="Capture timestamp")
    platform: str = Field(default="android", description="Platform identifier")

    class Config:
        arbitrary_types_allowed = True


class BaseDeviceDriver(ABC):
    """Abstract Base Class for mobile device and emulator drivers."""

    @property
    @abstractmethod
    def device_id(self) -> str:
        """Returns the unique hardware or simulator serial/identifier."""
        ...

    @property
    @abstractmethod
    def screen_size(self) -> tuple[int, int]:
        """Returns (width, height) in physical device pixels."""
        ...

    @abstractmethod
    async def connect(self) -> None:
        """Establishes connection to the target device."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Closes connection and releases allocated resources."""
        ...

    @abstractmethod
    async def get_screen_data(self, skip_settling: bool = False) -> ScreenData:
        """Captures real-time screenshot and UI element hierarchy.

        Args:
            skip_settling: If True, captures immediately without waiting for screen stabilization.
        """
        ...

    @abstractmethod
    async def tap(
        self,
        x: int,
        y: int,
        duration_ms: int = 100,
        times: int = 1,
        delay_ms: int = 100,
    ) -> bool:
        """Performs single or multiple tap interactions at absolute pixel coordinates.

        Args:
            x: Absolute horizontal coordinate in pixels.
            y: Absolute vertical coordinate in pixels.
            duration_ms: Touch hold duration in milliseconds.
            times: Number of consecutive taps (e.g. 2 for double tap, 7 for dev options).
            delay_ms: Delay in milliseconds between consecutive taps.
        """
        ...

    @abstractmethod
    async def long_press(self, x: int, y: int, duration_ms: int = 1000) -> bool:
        """Performs a long press at absolute pixel coordinates."""
        ...

    @abstractmethod
    async def swipe(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration_ms: int = 800,
    ) -> bool:
        """Performs a drag/swipe gesture from start to end pixel coordinates."""
        ...

    @abstractmethod
    async def swipe_direction(
        self,
        direction: SwipeDirection | Literal["up", "down", "left", "right"],
        duration_ms: int = 800,
    ) -> bool:
        """Performs standard cardinal scrolling gesture across device viewport."""
        ...

    @abstractmethod
    async def input_text(self, text: str, clear_existing: bool = True) -> bool:
        """Types text into the currently focused input field."""
        ...

    @abstractmethod
    async def press_key(self, key: KeyCode | str | int) -> bool:
        """Simulates physical or soft key press (e.g. HOME, BACK, ENTER)."""
        ...

    @abstractmethod
    async def launch_app(self, package_name: str) -> bool:
        """Launches target application package into the foreground."""
        ...

    @abstractmethod
    async def stop_app(self, package_name: str) -> bool:
        """Force-stops target application package."""
        ...

    @abstractmethod
    async def get_current_package(self) -> str | None:
        """Retrieves currently focused foreground application package name."""
        ...

    @abstractmethod
    async def execute_shell(self, command: str, timeout_seconds: float = 15.0) -> str:
        """Executes a system shell command directly on the device."""
        ...

    @abstractmethod
    async def start_video_recording(self, output_dir: Path | None = None) -> None:
        """Starts dynamic screen video capture in the background."""
        ...

    @abstractmethod
    async def stop_video_recording(self) -> str | None:
        """Stops background video capture and returns local recording file path."""
        ...

    async def wait_for_delay(self, seconds: float = 1.0) -> bool:
        """Waits for specified duration in seconds."""
        await asyncio.sleep(seconds)
        return True

    async def tap_normalized(
        self,
        norm_x: int,
        norm_y: int,
        long_press: bool = False,
        duration_ms: int = 1000,
        times: int = 1,
        delay_ms: int = 100,
    ) -> bool:
        """Taps at normalized (0-1000 scale) screen coordinates."""
        width, height = self.screen_size
        abs_x = int(max(0, min(width - 1, norm_x * width / 1000.0)))
        abs_y = int(max(0, min(height - 1, norm_y * height / 1000.0)))
        if long_press:
            return await self.long_press(abs_x, abs_y, duration_ms=duration_ms)
        return await self.tap(abs_x, abs_y, duration_ms=100, times=times, delay_ms=delay_ms)

    async def swipe_normalized(
        self,
        start_norm: list[int],
        end_norm: list[int],
        duration_ms: int = 800,
    ) -> bool:
        """Swipes between normalized (0-1000 scale) screen coordinates."""
        width, height = self.screen_size
        sx = int(max(0, min(width - 1, start_norm[0] * width / 1000.0)))
        sy = int(max(0, min(height - 1, start_norm[1] * height / 1000.0)))
        ex = int(max(0, min(width - 1, end_norm[0] * width / 1000.0)))
        ey = int(max(0, min(height - 1, end_norm[1] * height / 1000.0)))
        return await self.swipe(sx, sy, ex, ey, duration_ms=duration_ms)

    async def find_element(
        self,
        resource_id: str | None = None,
        text: str | None = None,
        index: int = 0,
        screen_data: ScreenData | None = None,
    ) -> tuple[dict[str, Any] | None, list[int] | None, str | None]:
        """Finds matching UI element in screen hierarchy by resource_id or text."""
        data = screen_data or await self.get_screen_data()
        elements = data.ui_elements or []

        matched = []
        for elem in elements:
            r_id = elem.get("resource_id") or elem.get("resource-id") or ""
            t_val = elem.get("text") or elem.get("content-desc") or ""

            if resource_id and resource_id in r_id:
                matched.append(elem)
            elif text and text.lower() in t_val.lower():
                matched.append(elem)

        if not matched:
            return None, None, f"Element not found (resource_id={resource_id}, text={text})"

        if index >= len(matched):
            return None, None, f"Element index {index} out of range (matched {len(matched)})"

        elem = matched[index]
        bounds = elem.get("bounds")
        center = elem.get("center")
        if not center and bounds and len(bounds) == 4:
            center = [(bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2]
        return elem, center, None

    async def tap_element(
        self,
        resource_id: str | None = None,
        text: str | None = None,
        index: int = 0,
        long_press: bool = False,
        duration_ms: int = 1000,
    ) -> bool:
        """Finds and taps a UI element by resource_id or text."""
        _, center, error = await self.find_element(resource_id=resource_id, text=text, index=index)
        if error or not center:
            return False
        if long_press:
            return await self.long_press(center[0], center[1], duration_ms=duration_ms)
        return await self.tap(center[0], center[1])
