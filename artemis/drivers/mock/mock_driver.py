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

"""Mock Device Driver for headless unit testing and offline simulation."""

import base64
from io import BytesIO
from pathlib import Path
from typing import Any, Literal
from PIL import Image

from artemis.drivers.base import BaseDeviceDriver, KeyCode, ScreenData, SwipeDirection


class MockDeviceDriver(BaseDeviceDriver):
    """In-memory mock driver for fast, reliable unit and integration tests."""

    def __init__(
        self,
        device_id: str = "mock-device-001",
        width: int = 1080,
        height: int = 2400,
        initial_package: str = "com.android.settings",
    ):
        self._device_id = device_id
        self._width = width
        self._height = height
        self._current_package = initial_package
        self.action_history: list[dict[str, Any]] = []
        self.connected = True
        self.recording = False

        # Create blank 1x1 black image for minimal mock bytes
        img = Image.new("RGB", (width, height), color="black")
        buf = BytesIO()
        img.save(buf, format="PNG")
        self._mock_bytes = buf.getvalue()
        self._mock_b64 = base64.b64encode(self._mock_bytes).decode("utf-8")
        self._mock_xml = (
            "<hierarchy><node text='Settings' class='android.widget.TextView'/></hierarchy>"
        )

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def screen_size(self) -> tuple[int, int]:
        return (self._width, self._height)

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def get_screen_data(self, skip_settling: bool = False) -> ScreenData:
        return ScreenData(
            screenshot_bytes=self._mock_bytes,
            screenshot_base64=self._mock_b64,
            ui_hierarchy_xml=self._mock_xml,
            ui_elements=[{"text": "Settings", "bounds": [0, 0, self._width, 100]}],
            width=self._width,
            height=self._height,
            platform="mock",
        )

    async def tap(
        self,
        x: int,
        y: int,
        duration_ms: int = 100,
        times: int = 1,
        delay_ms: int = 100,
    ) -> bool:
        self.action_history.append(
            {
                "action": "tap",
                "x": x,
                "y": y,
                "times": times,
                "duration_ms": duration_ms,
            }
        )
        return True

    async def long_press(self, x: int, y: int, duration_ms: int = 1000) -> bool:
        self.action_history.append(
            {
                "action": "long_press",
                "x": x,
                "y": y,
                "duration_ms": duration_ms,
            }
        )
        return True

    async def swipe(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration_ms: int = 400,
    ) -> bool:
        self.action_history.append(
            {
                "action": "swipe",
                "start": (start_x, start_y),
                "end": (end_x, end_y),
                "duration_ms": duration_ms,
            }
        )
        return True

    async def swipe_direction(
        self,
        direction: SwipeDirection | Literal["up", "down", "left", "right"],
        duration_ms: int = 400,
    ) -> bool:
        dir_val = direction.value if hasattr(direction, "value") else str(direction).lower()
        self.action_history.append(
            {
                "action": "swipe_direction",
                "direction": dir_val,
                "duration_ms": duration_ms,
            }
        )
        return True

    async def input_text(self, text: str, clear_existing: bool = True) -> bool:
        self.action_history.append(
            {
                "action": "input_text",
                "text": text,
                "clear_existing": clear_existing,
            }
        )
        return True

    async def press_key(self, key: KeyCode | str | int) -> bool:
        key_val = key.value if hasattr(key, "value") else str(key).lower()
        self.action_history.append(
            {
                "action": "press_key",
                "key": key_val,
            }
        )
        return True

    async def launch_app(self, package_name: str) -> bool:
        self._current_package = package_name
        self.action_history.append(
            {
                "action": "launch_app",
                "package": package_name,
            }
        )
        return True

    async def stop_app(self, package_name: str) -> bool:
        self.action_history.append(
            {
                "action": "stop_app",
                "package": package_name,
            }
        )
        return True

    async def get_current_package(self) -> str | None:
        return self._current_package

    async def execute_shell(self, command: str, timeout_seconds: float = 15.0) -> str:
        self.action_history.append(
            {
                "action": "execute_shell",
                "command": command,
            }
        )
        return f"mock_output: {command}"

    async def start_video_recording(self, output_dir: Path | None = None) -> None:
        self.recording = True

    async def stop_video_recording(self) -> str | None:
        self.recording = False
        return "/tmp/mock_recording.mp4"
