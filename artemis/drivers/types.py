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

"""Typed data models and geometry representations for device drivers."""

from typing import Literal
from pydantic import BaseModel, Field

from third_party.mobile_use.controllers.types import (
    Bounds,
    CoordinatesSelectorRequest,
    PercentagesSelectorRequest,
    TapOutput,
)

__all__ = [
    "Bounds",
    "CoordinatesSelectorRequest",
    "DeviceInfo",
    "PercentagesSelectorRequest",
    "SwipeRequest",
    "SwipeStartEndPercentagesRequest",
    "TapOutput",
]


class SwipeRequest(BaseModel):
    """Structured swipe gesture request with start and end pixel coordinates."""

    start_x: int
    start_y: int
    end_x: int
    end_y: int
    duration_ms: int = 400


class SwipeStartEndPercentagesRequest(BaseModel):
    """Normalized start and end percentage coordinates for swipe gestures."""

    start_x_percent: int = Field(ge=0, le=100)
    start_y_percent: int = Field(ge=0, le=100)
    end_x_percent: int = Field(ge=0, le=100)
    end_y_percent: int = Field(ge=0, le=100)
    duration_ms: int = 400


class DeviceInfo(BaseModel):
    """Hardware and OS metadata of connected device."""

    device_id: str
    platform: Literal["android", "ios", "web", "desktop", "mock"] = "android"
    model: str | None = None
    os_version: str | None = None
    width: int = 1080
    height: int = 2400
    density_dpi: int = 440
