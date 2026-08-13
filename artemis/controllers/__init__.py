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

"""Backward compatibility layer for legacy artemis.controllers module.
All new development should use artemis.drivers instead.
"""

from artemis.drivers.base import BaseDeviceDriver, KeyCode, ScreenData, SwipeDirection
from artemis.drivers.factory import create_driver, get_driver
from artemis.drivers.types import (
    Bounds,
    CoordinatesSelectorRequest,
    PercentagesSelectorRequest,
    SwipeRequest,
    TapOutput,
)

__all__ = [
    "BaseDeviceDriver",
    "KeyCode",
    "ScreenData",
    "SwipeDirection",
    "Bounds",
    "CoordinatesSelectorRequest",
    "PercentagesSelectorRequest",
    "SwipeRequest",
    "TapOutput",
    "create_driver",
    "get_driver",
]
