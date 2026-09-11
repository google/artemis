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
#
# Portions of this file are derived from mobile-use (https://github.com/minitap-ai/mobile-use)
# Copyright 2025-2026 Minitap, Inc. Licensed under the Apache License 2.0.

"""Abstract device controller contract and telemetry interfaces for ARTEMIS.

Defines the hardware interaction lifecycle including touch gestures, text entry,
process lifecycle operations, display capture, and multi-segment recording.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from artemis.controllers.types import Bounds, CoordinatesSelectorRequest, TapOutput
from artemis.utils.video import VideoRecordingResult


class ScreenDataResponse(BaseModel):
    """Aggregate snapshot containing live display frames and parsed node hierarchies."""

    model_config = ConfigDict(extra="ignore")

    base64: str = Field(default="", description="Base64 encoded JPEG/PNG frame capture.")
    elements: list[dict[str, Any]] = Field(default_factory=list, description="Parsed hierarchy node elements.")
    width: int = Field(default=1080, description="Active viewport width in physical pixels.")
    height: int = Field(default=2400, description="Active viewport height in physical pixels.")
    platform: str = Field(default="android", description="Target platform identifier.")


class MobileDeviceController(ABC):
    """Abstract driver interface establishing device actuation and telemetry operations."""

    @abstractmethod
    async def tap(
        self,
        coords: CoordinatesSelectorRequest,
        long_press: bool = False,
        long_press_duration: int = 1000,
        times: int = 1,
        delay_ms: int = 100,
    ) -> TapOutput:
        """Dispatch single, repeated, or long-press tap events to the target coordinates."""
        raise NotImplementedError

    @abstractmethod
    async def swipe(
        self,
        start: CoordinatesSelectorRequest,
        end: CoordinatesSelectorRequest,
        duration: int = 400,
    ) -> str | None:
        """Execute directional drag or fling between two coordinate positions."""
        raise NotImplementedError

    @abstractmethod
    async def screenshot(self) -> str:
        """Capture current display frame as an encoded image payload."""
        raise NotImplementedError

    @abstractmethod
    async def input_text(self, text: str) -> bool:
        """Stream textual characters into the currently active input field."""
        raise NotImplementedError

    @abstractmethod
    async def launch_app(self, package_name: str) -> bool:
        """Bring target Android application package into foreground."""
        raise NotImplementedError

    @abstractmethod
    async def terminate_app(self, package_name: str | None) -> bool:
        """Force-stop application process matching the target package."""
        raise NotImplementedError

    @abstractmethod
    async def open_url(self, url: str) -> bool:
        """Dispatch system VIEW intent for the specified URI."""
        raise NotImplementedError

    @abstractmethod
    async def press_back(self) -> bool:
        """Trigger Android BACK system navigation key event."""
        raise NotImplementedError

    @abstractmethod
    async def press_home(self) -> bool:
        """Trigger Android HOME system navigation key event."""
        raise NotImplementedError

    @abstractmethod
    async def press_enter(self) -> bool:
        """Trigger ENTER / ACTION_DONE key event on virtual keyboard."""
        raise NotImplementedError

    @abstractmethod
    async def press_key(self, keycode: str) -> bool:
        """Dispatch raw Android keycode event (e.g., 'KEYCODE_TAB')."""
        raise NotImplementedError

    @abstractmethod
    async def get_ui_hierarchy(self) -> list[dict[str, Any]]:
        """Retrieve flattened or nested accessibility tree node descriptors."""
        raise NotImplementedError

    @abstractmethod
    def find_element(
        self,
        ui_hierarchy: list[dict[str, Any]],
        resource_id: str | None = None,
        text: str | None = None,
        index: int = 0,
    ) -> tuple[dict[str, Any] | None, Bounds | None, str | None]:
        """Locate target element within parsed hierarchy by ID or label matching."""
        raise NotImplementedError

    @abstractmethod
    async def cleanup(self) -> None:
        """Tear down companion sessions, tunnels, and streaming handles."""
        raise NotImplementedError

    @abstractmethod
    async def erase_text(self, nb_chars: int | None = None) -> bool:
        """Dispatch backspace events to clear trailing characters in focused field."""
        raise NotImplementedError

    @abstractmethod
    async def get_screen_data(self) -> ScreenDataResponse:
        """Capture unified screen frame, layout tree, and viewport dimensions."""
        raise NotImplementedError

    @abstractmethod
    def get_compressed_b64_screenshot(self, image_base64: str, quality: int = 50) -> str:
        """Produce compressed image payload for bandwidth-constrained turns."""
        raise NotImplementedError

    @abstractmethod
    async def start_video_recording(
        self,
        max_duration_seconds: int = 900,
    ) -> VideoRecordingResult:
        """Initialize session screen capture recording."""
        raise NotImplementedError

    @abstractmethod
    async def stop_video_recording(self) -> VideoRecordingResult:
        """Terminate active screen capture and finalize video artifact."""
        raise NotImplementedError

    @abstractmethod
    async def extract_segment_metadata(
        self,
        start_relative_time: float,
        end_relative_time: float | None = None,
    ) -> VideoRecordingResult:
        """Slice video segment for the requested relative execution epoch."""
        raise NotImplementedError
