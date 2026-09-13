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

"""Remote Cloud Mobile Device Driver."""

from pathlib import Path
from typing import Literal
from artemis.drivers.base import BaseDeviceDriver, KeyCode, ScreenData, SwipeDirection
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


class CloudDeviceDriver(BaseDeviceDriver):
    """Driver communicating with remote Android devices via Cloud Gateway RPC."""

    def __init__(
        self,
        device_id: str = "cloud-device",
        gateway_url: str = "http://127.0.0.1:8000",
        session_id: str = "default-cloud-session",
        width: int = 1080,
        height: int = 2400,
    ):
        self._device_id = device_id
        self.gateway_url = gateway_url
        self.session_id = session_id
        self._width = width
        self._height = height

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def screen_size(self) -> tuple[int, int]:
        return (self._width, self._height)

    async def connect(self) -> None:
        logger.info(f"Connecting to Cloud Device {self._device_id} at {self.gateway_url}")

    async def disconnect(self) -> None:
        logger.info(f"Disconnecting Cloud Device {self._device_id}")

    async def get_screen_data(self, skip_settling: bool = False) -> ScreenData:
        return ScreenData(
            screenshot_bytes=b"",
            screenshot_base64="",
            width=self._width,
            height=self._height,
            platform="cloud",
        )

    async def tap(
        self,
        x: int,
        y: int,
        duration_ms: int = 100,
        times: int = 1,
        delay_ms: int = 100,
    ) -> bool:
        return True

    async def long_press(self, x: int, y: int, duration_ms: int = 1000) -> bool:
        return True

    async def swipe(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration_ms: int = 400,
    ) -> bool:
        return True

    async def swipe_direction(
        self,
        direction: SwipeDirection | Literal["up", "down", "left", "right"],
        duration_ms: int = 400,
    ) -> bool:
        return True

    async def input_text(self, text: str, clear_existing: bool = True) -> bool:
        return True

    async def press_key(self, key: KeyCode | str | int) -> bool:
        return True

    async def launch_app(self, package_name: str) -> bool:
        return True

    async def stop_app(self, package_name: str) -> bool:
        return True

    async def get_current_package(self) -> str | None:
        return None

    async def execute_shell(self, command: str, timeout_seconds: float = 15.0) -> str:
        return f"cloud_output: {command}"

    async def start_video_recording(self, output_dir: Path | None = None) -> None:
        pass

    async def stop_video_recording(self) -> str | None:
        return None
