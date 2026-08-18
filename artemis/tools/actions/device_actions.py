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

"""Universal device action tools implemented on top of BaseDeviceDriver."""

import asyncio
from typing import Any, Literal
from artemis.drivers.base import BaseDeviceDriver
from artemis.tools.base import artemis_tool
from artemis.utils.coordinates import (
    compute_smart_swipe_coordinates,
    parse_swipe_parameters,
)
from artemis.utils.logger import get_logger
from pydantic import BaseModel, Field

logger = get_logger(__name__)


# Schemas
class ClickArgs(BaseModel):
    target: list[int] = Field(
        ..., description="Click target normalized coordinates [x, y] in 0-1000 scale."
    )


class LongPressArgs(BaseModel):
    target: list[int] = Field(
        ..., description="Long press target normalized coordinates [x, y] in 0-1000 scale."
    )
    duration: int = Field(1000, description="Long press duration in milliseconds (default 1000).")


class InputTextArgs(BaseModel):
    text: str = Field(..., description="The text content to input.")
    target: list[int] = Field(
        ..., description="Input target field normalized coordinates [x, y] in 0-1000 scale."
    )
    clear_exist: bool = Field(
        True, description="Whether to clear existing text in the input box before typing."
    )


class SwipeArgs(BaseModel):
    direction: Literal["up", "down", "left", "right"] | None = Field(
        None,
        description=(
            "Direction for scrolling and swiping: 'up' (drags bottom-to-top, scrolling down to reveal content below),"
            " 'down' (drags top-to-bottom, scrolling up to reveal content above),"
            " 'left' (drags right-to-left, scrolling right),"
            " 'right' (drags left-to-right, scrolling left)."
        ),
    )
    start: list[int] | None = Field(
        None,
        description=(
            "Start normalized coordinates [start_x, start_y] in 0-1000 scale for precise,"
            " local interactions (e.g. adjusting sliders, SeekBars, fine range selection, or drag-and-drop)."
        ),
    )
    end: list[int] | None = Field(
        None,
        description=(
            "End normalized coordinates [end_x, end_y] in 0-1000 scale for precise,"
            " local interactions (e.g. adjusting sliders, SeekBars, fine range selection, or drag-and-drop)."
        ),
    )
    action: Literal["up", "down", "left", "right"] | list[int] | None = Field(
        None,
        description=(
            "Backward-compatible swipe gesture: smart direction string ('up', 'down', 'left', 'right')"
            " OR precise custom coordinates [start_x, start_y, end_x, end_y] in 0-1000 scale."
        ),
    )
    duration: int = Field(
        800,
        description=(
            "Swipe/drag duration in milliseconds (default 800). For drag-and-drop, "
            "list reordering, or sliding/adjusting sliders (e.g. volume, brightness, SeekBars), "
            "set duration >= 1000 (e.g. 1500)."
        ),
    )


class PressKeyArgs(BaseModel):
    key: str = Field(
        ...,
        description="Key name to press (e.g. 'home', 'back', 'enter', 'delete', 'power').",
    )


class WaitForDelayArgs(BaseModel):
    seconds: float = Field(
        ...,
        description=(
            "Duration in seconds to wait (e.g., 2 for 2s, 60 for 1 min, 180 for 3 mins)."
            " Use whenever time needs to elapse for animations, loading, or scheduled delays."
        ),
    )


# Universal Tool Definitions
@artemis_tool(
    name="click",
    description="[ACTION] Clicks on the specified screen coordinate [x, y] in normalized 0-1000 scale.",
    args_schema=ClickArgs,
    category="action",
)
async def click(target: list[int], driver: BaseDeviceDriver) -> str:
    width, height = driver.screen_size
    abs_x = int(target[0] * width / 1000.0)
    abs_y = int(target[1] * height / 1000.0)
    success = await driver.tap(abs_x, abs_y)
    return (
        f"Click executed at normalized {target} (pixels: {abs_x}, {abs_y})"
        if success
        else f"Click failed at {target}"
    )


@artemis_tool(
    name="long_press",
    description="[ACTION] Long presses on the specified screen coordinate [x, y] in normalized 0-1000 scale.",
    args_schema=LongPressArgs,
    category="action",
)
async def long_press(
    target: list[int], duration: int = 1000, driver: BaseDeviceDriver = None
) -> str:
    width, height = driver.screen_size
    abs_x = int(target[0] * width / 1000.0)
    abs_y = int(target[1] * height / 1000.0)
    success = await driver.long_press(abs_x, abs_y, duration_ms=duration)
    return (
        f"Long press executed at normalized {target} for {duration}ms"
        if success
        else f"Long press failed at {target}"
    )


@artemis_tool(
    name="input_text",
    description="[ACTION] Clicks an input field target [x, y] and types text into it.",
    args_schema=InputTextArgs,
    category="action",
)
async def input_text(
    text: str, target: list[int], clear_exist: bool = True, driver: BaseDeviceDriver = None
) -> str:
    # First tap to focus
    width, height = driver.screen_size
    abs_x = int(target[0] * width / 1000.0)
    abs_y = int(target[1] * height / 1000.0)
    await driver.tap(abs_x, abs_y)
    await asyncio.sleep(0.2)

    # Then input text
    success = await driver.input_text(text, clear_existing=clear_exist)
    return (
        f"Input text '{text}' into target {target}"
        if success
        else f"Failed to input text into {target}"
    )


@artemis_tool(
    name="swipe",
    description=(
        "[ACTION] Performs smart directional swipe for browsing, or precise coordinate drag/slider gestures."
    ),
    args_schema=SwipeArgs,
    category="action",
)
async def swipe(
    action: Any = None,
    direction: str | None = None,
    start: list[int] | None = None,
    end: list[int] | None = None,
    duration: int = 800,
    driver: BaseDeviceDriver = None,
    **kwargs: Any,
) -> str:
    params = dict(kwargs)
    if action is not None:
        params["action"] = action
    if direction is not None:
        params["direction"] = direction
    if start is not None:
        params["start"] = start
    if end is not None:
        params["end"] = end
    params["duration"] = duration

    kind, target, dur = parse_swipe_parameters(params, default_duration=duration)

    if kind == "direction" and isinstance(target, str):
        width, height = driver.screen_size
        sx, sy, ex, ey, smart_dur = compute_smart_swipe_coordinates(
            direction=target,
            target=params.get("target"),
            width=width,
            height=height,
            duration=dur,
        )
        success = await driver.swipe(sx, sy, ex, ey, duration_ms=smart_dur)
        return f"Swiped {target}" if success else f"Swipe {target} failed"
    elif kind == "coords" and isinstance(target, list) and len(target) == 4:
        width, height = driver.screen_size
        start_x = int(target[0] * width / 1000.0)
        start_y = int(target[1] * height / 1000.0)
        end_x = int(target[2] * width / 1000.0)
        end_y = int(target[3] * height / 1000.0)
        success = await driver.swipe(start_x, start_y, end_x, end_y, duration_ms=dur)
        return (
            f"Swiped from {target[:2]} to {target[2:]}" if success else "Swipe coordinates failed"
        )
    return f"Invalid swipe action parameter: {action or direction or (start, end)}"


@artemis_tool(
    name="press_key",
    description="[ACTION] Presses a hardware or system key on the device ('home', 'back', 'enter', 'delete', 'power').",
    args_schema=PressKeyArgs,
    category="action",
)
async def press_key(key: str, driver: BaseDeviceDriver) -> str:
    success = await driver.press_key(key)
    return f"Key '{key}' pressed" if success else f"Failed to press key '{key}'"


@artemis_tool(
    name="wait_for_delay",
    description="[ACTION] Pauses execution for a specified number of seconds to allow UI loading or transition.",
    args_schema=WaitForDelayArgs,
    category="action",
)
async def wait_for_delay(seconds: float, driver: BaseDeviceDriver = None) -> str:
    await asyncio.sleep(seconds)
    return f"Waited for {seconds} seconds."
