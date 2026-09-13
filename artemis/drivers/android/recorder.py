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

"""Android screen recording and video transcode utilities."""

import asyncio
from pathlib import Path
from typing import Any
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


class ScreenRecorder:
    """Manages background screen recording sessions using adb screenrecord."""

    def __init__(self, adb_device: Any, output_path: Path):
        self.device = adb_device
        self.output_path = output_path
        self.is_recording = False
        self._remote_path = "/sdcard/artemis_record.mp4"

    async def start(self) -> None:
        """Starts remote screen recording."""
        self.is_recording = True
        logger.info("Starting screen recording on device...")
        # Start background screenrecord
        asyncio.create_task(
            asyncio.to_thread(self.device.shell, f"screenrecord {self._remote_path}")
        )

    async def stop(self) -> Path | None:
        """Stops screen recording and pulls file to host."""
        if not self.is_recording:
            return None
        self.is_recording = False
        logger.info("Stopping screen recording...")
        try:
            await asyncio.to_thread(self.device.shell, "pkill -2 screenrecord")
            await asyncio.sleep(1.0)
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(self.device.sync.pull, self._remote_path, str(self.output_path))
            return self.output_path
        except Exception as e:
            logger.warning(f"Failed to pull screen recording: {e}")
            return None
