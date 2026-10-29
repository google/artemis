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

"""Geometry, element selector and tap result models shared by device drivers."""

from pydantic import BaseModel, ConfigDict, Field


class TapOutput(BaseModel):
    """Output from tap operations."""

    error: str | None = Field(default=None, description="Error message if tap failed")
    success: bool = Field(default=True, description="Whether the tap was successfully dispatched")


class ElementQuery(BaseModel):
    """Selects a UI element by resource id or text; ``index`` picks among the matches."""

    model_config = ConfigDict(frozen=True)
    resource_id: str | None = None
    text: str | None = None
    index: int = 0

    def describe(self) -> str:
        return f"resource_id={self.resource_id}, text={self.text}"


class CoordinatesSelectorRequest(BaseModel):
    """Absolute pixel coordinate pair."""

    model_config = ConfigDict(extra="forbid")
    x: int
    y: int

    def to_str(self) -> str:
        return f"{self.x}, {self.y}"


class PercentagesSelectorRequest(BaseModel):
    """Normalized percentage coordinate pair (0-100%)."""

    model_config = ConfigDict(extra="forbid")
    x_percent: int = Field(ge=0, le=100, description="X percentage (0-100)")
    y_percent: int = Field(ge=0, le=100, description="Y percentage (0-100)")

    def to_str(self) -> str:
        return f"{self.x_percent}%, {self.y_percent}%"

    def to_coords(self, width: int, height: int) -> CoordinatesSelectorRequest:
        """Convert percentages to absolute pixel coordinates."""
        x = min(max(int(width * self.x_percent / 100), 0), max(0, width - 1))
        y = min(max(int(height * self.y_percent / 100), 0), max(0, height - 1))
        return CoordinatesSelectorRequest(x=x, y=y)


class Bounds(BaseModel):
    """Bounding box defined by top-left (x1, y1) and bottom-right (x2, y2)."""

    x1: int
    y1: int
    x2: int
    y2: int

    @property
    def width(self) -> int:
        return max(0, self.x2 - self.x1)

    @property
    def height(self) -> int:
        return max(0, self.y2 - self.y1)

    def get_center(self) -> CoordinatesSelectorRequest:
        """Get the center point of the bounds."""
        return CoordinatesSelectorRequest(
            x=(self.x1 + self.x2) // 2,
            y=(self.y1 + self.y2) // 2,
        )
