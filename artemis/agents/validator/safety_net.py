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

"""Safety net and system dialog auto-recovery."""

from typing import Any
from artemis.drivers.base import BaseDeviceDriver
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


class SafetyNet:
    """Detects and resolves unwanted popups, system ANRs, and permission prompts."""

    SYSTEM_DIALOG_PATTERNS = [
        "is not responding",
        "Wait",
        "Close app",
        "Allow only while using the app",
        "While using the app",
        "Don't allow",
    ]

    @classmethod
    async def check_and_recover(cls, screen_data: Any, driver: BaseDeviceDriver) -> bool:
        """Inspects screen elements for crash/permission dialogs and dismisses them."""
        for element in screen_data.ui_elements:
            text = element.get("text", "")
            if any(pattern.lower() in text.lower() for pattern in cls.SYSTEM_DIALOG_PATTERNS):
                logger.info(
                    f"SafetyNet detected system dialog '{text}', attempting auto-dismiss..."
                )
                center = element.get("center", [500, 500])
                await driver.tap(center[0], center[1])
                return True
        return False
