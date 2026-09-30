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

"""Validated, executor-neutral continuous touch plans (0..1000 coordinates)."""

from typing import Annotated, Any, Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

Coordinate = Annotated[StrictInt, Field(ge=0, le=1000)]
Point = Annotated[list[Coordinate], Field(min_length=2, max_length=2)]


class GesturePointer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Annotated[StrictInt, Field(ge=0, le=9)]
    path: Annotated[list[Point], Field(min_length=1, max_length=128)]
    control_points: Annotated[list[Point], Field(min_length=2, max_length=2)] | None = Field(
        default=None,
        description="Two cubic Bezier control points. When supplied, path must contain only start and end; Android builds the curve natively.",
    )

    @model_validator(mode="after")
    def curve_endpoints(self):
        if self.control_points is not None and len(self.path) != 2:
            raise ValueError("A cubic Bezier path requires exactly start and end")
        return self


class GesturePhase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    duration_ms: Annotated[StrictInt, Field(ge=1, le=5000)]
    pointers: Annotated[list[GesturePointer], Field(min_length=1, max_length=10)]


class LongPressDrag(BaseModel):
    """Hold, move to a caller-selected endpoint, optionally dwell, then release."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["long_press_drag"]
    start: Point
    end: Point
    control_points: Annotated[list[Point], Field(min_length=2, max_length=2)] | None = Field(
        default=None,
        description="Two cubic Bezier controls between start and end. Omit for a straight path.",
    )
    duration_ms: Annotated[StrictInt, Field(ge=1, le=5000)] = 800
    hold_ms: Annotated[StrictInt, Field(ge=1, le=5000)] | None = Field(
        default=None, description="Omit to use the device long-press timeout plus 150ms."
    )
    release_delay_ms: Annotated[StrictInt, Field(ge=0, le=5000)] = Field(
        default=0,
        description="Keep the same finger down at the endpoint for this long, then lift. Zero lifts immediately after moving; independent of movement duration.",
    )


GestureInput = GesturePhase | LongPressDrag


class GesturePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phases: Annotated[list[GestureInput], Field(min_length=1, max_length=32)]

    @model_validator(mode="after")
    def continuous(self):
        if any(isinstance(p, LongPressDrag) for p in self.phases):
            if len(self.phases) != 1:
                raise ValueError(
                    "long_press_drag must be the only entry; it includes hold, move and release"
                )
            return self
        if sum(p.duration_ms for p in self.phases) > 30000:
            raise ValueError("Total gesture duration must not exceed 30000ms")
        ends = None
        for phase in self.phases:
            ids = [p.id for p in phase.pointers]
            if len(ids) != len(set(ids)):
                raise ValueError("Pointer IDs must be unique within a phase")
            if ends is not None:
                if set(ids) != set(ends):
                    raise ValueError("Keep the same pointer IDs across all phases")
                if any(p.path[0] != ends[p.id] for p in phase.pointers):
                    raise ValueError("Each continuation must start at the previous endpoint")
            ends = {p.id: p.path[-1] for p in phase.pointers}
        return self


def validate_phases(phases: Any) -> list[dict]:
    """Validate before dispatch; never coerce strings/floats/bools into coordinates."""
    return GesturePlan(phases=phases).model_dump(exclude_none=True)["phases"]


def gesture_duration_bound_ms(phases: list[dict]) -> int:
    """Bound transport timeout including APK-resolved hold and endpoint dwell."""
    return sum(
        p["duration_ms"]
        + (
            p.get("hold_ms", 5000) + p.get("release_delay_ms", 0)
            if p.get("kind") == "long_press_drag"
            else 0
        )
        for p in phases
    )
