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

from abc import ABC, abstractmethod
from typing import Any

from artemis.controllers.types import Bounds, CoordinatesSelectorRequest, TapOutput
from artemis.utils.video import VideoRecordingResult
from pydantic import BaseModel, Field


class _CyFunctionDetectorMeta(type):
    def __instancecheck__(self, instance):
        name = type(instance).__name__
        return (
            name
            in (
                "cyfunction",
                "cython_function_or_method",
                "builtin_function_or_method",
            )
            or "cyfunction" in name.lower()
        )


class CyFunctionDetector(metaclass=_CyFunctionDetectorMeta):
    pass


class ScreenDataResponse(BaseModel):
    model_config = {"ignored_types": (CyFunctionDetector,)}
    base64: str = Field(default="", description="Base64 encoded screenshot string")
    elements: list[dict[str, Any]] = Field(
        default_factory=list, description="Parsed UI hierarchy elements"
    )
    width: int = Field(default=1080, description="Device screen width in pixels")
    height: int = Field(default=2400, description="Device screen height in pixels")
    platform: str = Field(default="android", description="Platform identifier")


class MobileDeviceController(ABC):
    @abstractmethod
    async def tap(
        self,
        coords: CoordinatesSelectorRequest,
        long_press: bool = False,
        long_press_duration: int = 1000,
        times: int = 1,
        delay_ms: int = 100,
    ) -> TapOutput:
        raise NotImplementedError("Subclasses must implement this method")

    @abstractmethod
    async def swipe(
        self,
        start: CoordinatesSelectorRequest,
        end: CoordinatesSelectorRequest,
        duration: int = 400,
    ) -> str | None:
        """Swipe from start to end coordinates.

        Returns error message on failure, None on success.
        """
        raise NotImplementedError("Subclasses must implement this method")

    @abstractmethod
    async def screenshot(self) -> str:
        """Take a screenshot and return raw image data."""
        raise NotImplementedError("Subclasses must implement this method")

    @abstractmethod
    async def input_text(self, text: str) -> bool:
        """Input text at the currently focused field.

        Returns True on success, False on failure.
        """
        raise NotImplementedError("Subclasses must implement this method")

    @abstractmethod
    async def launch_app(self, package_or_bundle_id: str) -> bool:
        """Launch an application by package name (Android).

        Returns True on success, False on failure.
        """
        raise NotImplementedError("Subclasses must implement this method")

    @abstractmethod
    async def terminate_app(self, package_or_bundle_id: str | None) -> bool:
        """Terminate an application.

        Returns True on success, False on failure.
        """
        raise NotImplementedError("Subclasses must implement this method")

    @abstractmethod
    async def open_url(self, url: str) -> bool:
        """Open a URL.

        Returns True on success, False on failure.
        """
        raise NotImplementedError("Subclasses must implement this method")

    @abstractmethod
    async def press_back(self) -> bool:
        """Press the back button (Android).

        Returns True on success, False on failure.
        """
        raise NotImplementedError("Subclasses must implement this method")

    @abstractmethod
    async def press_home(self) -> bool:
        """Press the home button.

        Returns True on success, False on failure.
        """
        raise NotImplementedError("Subclasses must implement this method")

    @abstractmethod
    async def press_enter(self) -> bool:
        """Press the enter/return key.

        Returns True on success, False on failure.
        """
        raise NotImplementedError("Subclasses must implement this method")

    @abstractmethod
    async def press_key(self, keycode: str) -> bool:
        """Press a specific key event.

        Returns True on success, False on failure.
        """
        raise NotImplementedError("Subclasses must implement this method")

    @abstractmethod
    async def get_ui_hierarchy(self) -> list[dict]:
        """Get the UI element hierarchy.

        Returns a list of UI elements with their properties.
        """
        raise NotImplementedError("Subclasses must implement this method")

    @abstractmethod
    def find_element(
        self,
        ui_hierarchy: list[dict],
        resource_id: str | None = None,
        text: str | None = None,
        index: int = 0,
    ) -> tuple[dict | None, Bounds | None, str | None]:
        """Find a UI element in the hierarchy.

        Returns:
            Tuple of (element_dict, bounds, error_message)
        """
        raise NotImplementedError("Subclasses must implement this method")

    @abstractmethod
    async def cleanup(self) -> None:
        """Clean up resources (e.g., stop companion processes)."""
        raise NotImplementedError("Subclasses must implement this method")

    @abstractmethod
    async def erase_text(self, nb_chars: int | None = None) -> bool:
        """Erase the last nb_chars characters.

        Returns True on success, False on failure.
        """
        raise NotImplementedError("Subclasses must implement this method")

    @abstractmethod
    async def get_screen_data(self) -> "ScreenDataResponse":
        """Get screen data including screenshot (base64), UI hierarchy elements,

        screen dimensions, and platform.

        Returns:
            ScreenDataResponse with base64 screenshot, elements, width, height,
            platform
        """
        raise NotImplementedError("Subclasses must implement this method")

    @abstractmethod
    def get_compressed_b64_screenshot(self, image_base64: str, quality: int = 50) -> str:
        """Compress a base64 image.

        Returns the compressed base64 image.
        """
        raise NotImplementedError("Subclasses must implement this method")

    @abstractmethod
    async def start_video_recording(
        self,
        max_duration_seconds: int = 900,
    ) -> VideoRecordingResult:
        """Start screen recording on the device.

        Args:
            max_duration_seconds: Maximum recording duration in seconds.

        Returns:
            VideoRecordingResult with success status and message.
        """
        raise NotImplementedError("Subclasses must implement this method")

    @abstractmethod
    async def stop_video_recording(self) -> VideoRecordingResult:
        """Stop screen recording and retrieve the video file.

        Returns:
            VideoRecordingResult with success status, message, and video_path if
            successful.
        """
        raise NotImplementedError("Subclasses must implement this method")

    @abstractmethod
    async def extract_segment_metadata(
        self,
        start_relative_time: float,
        end_relative_time: float | None = None,
    ) -> VideoRecordingResult:
        """Get a video segment for a specific time range.

        Args:
            start_relative_time: Start time in seconds relative to session
              start.
            end_relative_time: End time in seconds relative to session start, or
              None for latest.

        Returns:
            VideoRecordingResult with success status, message, and video_path if
            successful.
        """
        raise NotImplementedError("Subclasses must implement this method")
