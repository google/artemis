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

import asyncio
from typing import Any, Literal

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langgraph.types import Command
from pydantic import BaseModel, Field

from artemis.constants import VALIDATOR_MESSAGES_KEY
from artemis.context import ArtemisContext
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.data_engine.trace import trace_langchain_tool
from artemis.drivers.base import BaseDeviceDriver
from artemis.graph.state import State
from artemis.tools.base import ArtemisTool, ToolCategory, ToolRegistry
from artemis.tools.tool_wrapper import ToolWrapper
from artemis.tools.types import CyFunctionDetector
from artemis.utils.coordinates import (
    compute_smart_swipe_coordinates,
    parse_swipe_parameters,
)
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


class ClickArgs(BaseModel):
    """Arguments schema for clicking on normalized coordinates."""

    model_config = {"ignored_types": (CyFunctionDetector,)}
    target: list[int] = Field(
        ...,
        description=("Click target normalized coordinates [x, y] in 0-1000 scale."),
    )


class LongPressArgs(BaseModel):
    """Arguments schema for long pressing on normalized coordinates."""

    model_config = {"ignored_types": (CyFunctionDetector,)}
    target: list[int] = Field(
        ...,
        description=("Long press target normalized coordinates [x, y] in 0-1000 scale."),
    )
    duration: int = Field(1000, description="Long press duration in milliseconds (default 1000).")


class InputTextArgs(BaseModel):
    """Arguments schema for inputting text into normalized coordinates."""

    model_config = {"ignored_types": (CyFunctionDetector,)}
    text: str = Field(
        ...,
        description="The text content to input. Supports multi-line strings with '\\n'.",
    )
    target: list[int] = Field(
        ...,
        description=("Input target field normalized coordinates [x, y] in 0-1000 scale."),
    )
    clear_exist: bool = Field(
        True,
        description=(
            "Whether to clear existing text before typing (True to replace all"
            " text, False to append at the end of existing content)."
        ),
    )


class SwipeArgs(BaseModel):
    """Arguments schema for performing swipe gestures."""

    model_config = {"ignored_types": (CyFunctionDetector,)}
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
    target: int | list[int] | str | None = Field(
        None,
        description=(
            "Optional target element index (e.g. 2) or container bounds [left, top, right, bottom] to scope the directional swipe within."
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
            "Optional swipe/drag duration in milliseconds (default 800). For drag-and-drop,"
            " list reordering, or sliding/adjusting sliders (e.g., volume, brightness, SeekBars),"
            " set duration >= 1000 (e.g. 1500). If omitted for directional swipe, duration is computed automatically."
        ),
    )


class PressKeyArgs(BaseModel):
    """Arguments schema for pressing system keys."""

    model_config = {"ignored_types": (CyFunctionDetector,)}
    key: Literal["ENTER", "BACK", "HOME", "APP_SWITCH"] = Field(
        ...,
        description=("Standard Android system button name (ENTER, BACK, HOME, APP_SWITCH)."),
    )


CLICK_DOCSTRING = "[ACTION] Click on the target normalized coordinates on the screen."


class ClickTool(ArtemisTool):
    """Universal tool for clicking on target normalized coordinates on the screen."""

    def __init__(self, category: ToolCategory = "action"):
        super().__init__(
            name="click",
            description=CLICK_DOCSTRING,
            args_schema=ClickArgs,
            category=category,
        )

    # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    async def execute(
        self,
        driver: BaseDeviceDriver | None = None,
        ctx: ArtemisContext | None = None,
        target: list[int] | None = None,
        tool_call_id: str | None = None,
        state: State | None = None,
        **kwargs: Any,
    ) -> Any:
        tgt = target if target is not None else (kwargs.get("target") or kwargs.get("Target") or [])
        tcid = tool_call_id if tool_call_id is not None else kwargs.get("tool_call_id")
        st = state if state is not None else kwargs.get("state")

        width = 1080
        height = 2400
        if ctx and hasattr(ctx, "device") and ctx.device:
            width = getattr(ctx.device, "device_width", 1080)
            height = getattr(ctx.device, "device_height", 2400)
        elif driver and hasattr(driver, "screen_size"):
            width, height = driver.screen_size

        try:
            if not tgt or len(tgt) < 2:
                raise ValueError(f"Invalid target coordinates: {tgt}")
            nx, ny = tgt[0], tgt[1]
            x = int(max(0, min(width - 1, nx * width / 1000)))
            y = int(max(0, min(height - 1, ny * height / 1000)))

            if driver is not None and hasattr(driver, "tap"):
                success = await driver.tap(x, y)
                outcome = (
                    f"Clicked at [{nx}, {ny}] (normalized) successfully."
                    if success
                    else f"Click failed at {tgt}"
                )
            elif ctx is not None:
                controller = UnifiedMobileController(ctx)
                result = await controller.tap_at(x, y)
                success = result.error is None
                outcome = (
                    f"Clicked at [{nx}, {ny}] (normalized) successfully."
                    if success
                    else f"Failed to click: {result.error}"
                )
            else:
                success = False
                outcome = "Error during click: No driver or context provided."
        except Exception as e:  # pylint: disable=broad-exception-caught
            success = False
            outcome = f"Error during click: {e}"

        if st and callable(getattr(st, "asanitize_update", None)):
            tool_message = ToolMessage(
                tool_call_id=tcid or "",
                content=outcome,
                status="success" if success else "error",
            )
            return Command(
                update=await st.asanitize_update(
                    ctx=ctx,
                    update={VALIDATOR_MESSAGES_KEY: [tool_message]},
                    agent="validator",
                )
            )

        return outcome


# Universal tool instance & aliases
click = ClickTool()
Click = ClickTool
ToolRegistry.register(click)


def get_click_tool(ctx: ArtemisContext) -> BaseTool:
    """Exports click as a LangChain BaseTool."""
    return trace_langchain_tool(click.to_langchain_tool(ctx), ctx)


LONG_PRESS_DOCSTRING = "[ACTION] Long press on the target normalized coordinates on the screen."


class LongPressTool(ArtemisTool):
    """Universal tool for long pressing on target normalized coordinates on the screen."""

    def __init__(self, category: ToolCategory = "action"):
        super().__init__(
            name="long_press",
            description=LONG_PRESS_DOCSTRING,
            args_schema=LongPressArgs,
            category=category,
        )

    # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    async def execute(
        self,
        driver: BaseDeviceDriver | None = None,
        ctx: ArtemisContext | None = None,
        target: list[int] | None = None,
        duration: int = 1000,
        tool_call_id: str | None = None,
        state: State | None = None,
        **kwargs: Any,
    ) -> Any:
        tgt = target if target is not None else (kwargs.get("target") or kwargs.get("Target") or [])
        dur = (
            duration
            if duration is not None
            else (kwargs.get("duration") or kwargs.get("Duration") or 1000)
        )
        tcid = tool_call_id if tool_call_id is not None else kwargs.get("tool_call_id")
        st = state if state is not None else kwargs.get("state")

        width = 1080
        height = 2400
        if ctx and hasattr(ctx, "device") and ctx.device:
            width = getattr(ctx.device, "device_width", 1080)
            height = getattr(ctx.device, "device_height", 2400)
        elif driver and hasattr(driver, "screen_size"):
            width, height = driver.screen_size

        try:
            if not tgt or len(tgt) < 2:
                raise ValueError(f"Invalid target coordinates: {tgt}")
            nx, ny = tgt[0], tgt[1]
            x = int(max(0, min(width - 1, nx * width / 1000)))
            y = int(max(0, min(height - 1, ny * height / 1000)))

            if driver is not None and hasattr(driver, "long_press"):
                success = await driver.long_press(x, y, duration_ms=dur)
                outcome = (
                    f"Long pressed at [{nx}, {ny}] (normalized) successfully."
                    if success
                    else f"Failed to long press: Long press failed at {tgt}"
                )
            elif ctx is not None:
                controller = UnifiedMobileController(ctx)
                result = await controller.tap_at(x, y, long_press=True, long_press_duration=dur)
                success = result.error is None
                outcome = (
                    f"Long pressed at [{nx}, {ny}] (normalized) successfully."
                    if success
                    else f"Failed to long press: {result.error}"
                )
            else:
                success = False
                outcome = "Error during long press: No driver or context provided."
        except Exception as e:  # pylint: disable=broad-exception-caught
            success = False
            outcome = f"Error during long press: {e}"

        if st and callable(getattr(st, "asanitize_update", None)):
            tool_message = ToolMessage(
                tool_call_id=tcid or "",
                content=outcome,
                status="success" if success else "error",
            )
            return Command(
                update=await st.asanitize_update(
                    ctx=ctx,
                    update={VALIDATOR_MESSAGES_KEY: [tool_message]},
                    agent="validator",
                )
            )

        return outcome


# Universal tool instance & aliases
long_press = LongPressTool()
LongPress = LongPressTool
ToolRegistry.register(long_press)


def get_long_press_tool(ctx: ArtemisContext) -> BaseTool:
    """Exports long_press as a LangChain BaseTool."""
    return trace_langchain_tool(long_press.to_langchain_tool(ctx), ctx)


INPUT_TEXT_DOCSTRING = "[ACTION] Type text into the target input field coordinates."


class InputTextTool(ArtemisTool):
    """Universal tool for typing text into target input field coordinates."""

    def __init__(self, category: ToolCategory = "action"):
        super().__init__(
            name="input_text",
            description=INPUT_TEXT_DOCSTRING,
            args_schema=InputTextArgs,
            category=category,
        )

    # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    async def execute(
        self,
        driver: BaseDeviceDriver | None = None,
        ctx: ArtemisContext | None = None,
        text: str | None = None,
        target: list[int] | None = None,
        clear_exist: bool = True,
        tool_call_id: str | None = None,
        state: State | None = None,
        **kwargs: Any,
    ) -> Any:
        txt = text if text is not None else (kwargs.get("text") or kwargs.get("Text") or "")
        tgt = target if target is not None else (kwargs.get("target") or kwargs.get("Target") or [])
        clr = (
            clear_exist
            if clear_exist is not None
            else kwargs.get("clear_exist", kwargs.get("ClearExist", True))
        )
        tcid = tool_call_id if tool_call_id is not None else kwargs.get("tool_call_id")
        st = state if state is not None else kwargs.get("state")

        width = 1080
        height = 2400
        if ctx and hasattr(ctx, "device") and ctx.device:
            width = getattr(ctx.device, "device_width", 1080)
            height = getattr(ctx.device, "device_height", 2400)
        elif driver and hasattr(driver, "screen_size"):
            width, height = driver.screen_size

        try:
            if not tgt or len(tgt) < 2:
                raise ValueError(f"Invalid target coordinates: {tgt}")
            nx, ny = tgt[0], tgt[1]
            x = int(max(0, min(width - 1, nx * width / 1000)))
            y = int(max(0, min(height - 1, ny * height / 1000)))

            if driver is not None and hasattr(driver, "input_text"):
                await driver.tap(x, y)
                await asyncio.sleep(0.2)
                success = await driver.input_text(txt, clear_existing=clr)
                outcome = (
                    f"Typed '{txt}' successfully."
                    if success
                    else f"Failed to input text into {tgt}"
                )
            elif ctx is not None:
                controller = UnifiedMobileController(ctx)
                tap_result = await controller.tap_at(x, y)
                if tap_result.error:
                    raise RuntimeError(f"Failed to focus input field: {tap_result.error}")

                if clr:
                    await controller.erase_text()
                else:
                    await controller.press_key("123")  # Move to end

                success = await controller.type_text(txt, clear_existing=False)
                outcome = f"Typed '{txt}' successfully." if success else "Failed to type text."
            else:
                success = False
                outcome = "Error during input text: No driver or context provided."
        except Exception as e:  # pylint: disable=broad-exception-caught
            success = False
            outcome = f"Error during input text: {e}"

        if st and callable(getattr(st, "asanitize_update", None)):
            tool_message = ToolMessage(
                tool_call_id=tcid or "",
                content=outcome,
                status="success" if success else "error",
            )
            return Command(
                update=await st.asanitize_update(
                    ctx=ctx,
                    update={VALIDATOR_MESSAGES_KEY: [tool_message]},
                    agent="validator",
                )
            )

        return outcome


# Universal tool instance & aliases
input_text = InputTextTool()
InputText = InputTextTool
ToolRegistry.register(input_text)


def get_input_text_tool(ctx: ArtemisContext) -> BaseTool:
    """Exports input_text as a LangChain BaseTool."""
    return trace_langchain_tool(input_text.to_langchain_tool(ctx), ctx)


SWIPE_DOCSTRING = (
    "[ACTION] Perform a swipe, drag, or slider-adjustment gesture on the"
    " screen.\n\n"
    "• Directional Scrolling ('direction'): Recommended for general"
    " browsing and standard page scrolling in most scenarios. Automatically"
    " computes safe swipe vectors and adaptive duration, retains a ~40% visual overlap"
    " anchor for zero-omission traversal, and prevents inertial flings. Supports"
    " scoping to a sub-container via 'target'. If it fails on certain custom layouts,"
    " fall back to specifying exact coordinates ('start' and 'end') directly.\n"
    "• Precise Coordinate Gestures ('start', 'end'): Best for local,"
    " fine-grained interactions such as adjusting sliders/SeekBars (e.g.,"
    " volume, brightness, progress bars), drag-and-drop / list reordering,"
    " or as a reliable fallback when directional scrolling fails on specific"
    " containers. Always drag slightly PAST the target position to overcome"
    " touch slop and reliably trigger the update. When setting a slider to"
    " Maximum (100%) or Minimum (0%), swipe fully to the extreme boundary."
)


class SwipeTool(ArtemisTool):
    """Universal tool for performing swipe gestures on the screen."""

    def __init__(self, category: ToolCategory = "action"):
        super().__init__(
            name="swipe",
            description=SWIPE_DOCSTRING,
            args_schema=SwipeArgs,
            category=category,
        )

    # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-branches,too-many-locals,too-many-statements
    async def execute(
        self,
        driver: BaseDeviceDriver | None = None,
        ctx: ArtemisContext | None = None,
        action: Literal["up", "down", "left", "right"] | list[int] | str | None = None,
        direction: Literal["up", "down", "left", "right"] | str | None = None,
        start: list[int] | None = None,
        end: list[int] | None = None,
        duration: int = 800,
        tool_call_id: str | None = None,
        state: State | None = None,
        **kwargs: Any,
    ) -> Any:
        params = dict(kwargs)
        if action is not None:
            params["action"] = action
        if direction is not None:
            params["direction"] = direction
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end
        if duration is not None:
            params["duration"] = duration

        kind, target, dur = parse_swipe_parameters(params, default_duration=duration or 800)

        tcid = tool_call_id if tool_call_id is not None else kwargs.get("tool_call_id")
        st = state if state is not None else kwargs.get("state")

        width = 1080
        height = 2400
        if ctx and hasattr(ctx, "device") and ctx.device:
            width = getattr(ctx.device, "device_width", 1080)
            height = getattr(ctx.device, "device_height", 2400)
        elif driver and hasattr(driver, "screen_size"):
            width, height = driver.screen_size

        try:
            if kind is None or target is None:
                raise ValueError(
                    f"Invalid swipe action parameter: {action or direction or (start, end) or kwargs}"
                )

            if (
                driver is not None
                and hasattr(driver, "swipe_direction")
                and hasattr(driver, "swipe")
            ):
                if kind == "direction" and isinstance(target, str):
                    success = await driver.swipe_direction(target, duration_ms=dur)
                    outcome = (
                        "Swipe completed successfully." if success else f"Swipe {target} failed"
                    )
                elif kind == "coords" and isinstance(target, list) and len(target) == 4:
                    start_x = int(max(0, min(width - 1, target[0] * width / 1000)))
                    start_y = int(max(0, min(height - 1, target[1] * height / 1000)))
                    end_x = int(max(0, min(width - 1, target[2] * width / 1000)))
                    end_y = int(max(0, min(height - 1, target[3] * height / 1000)))
                    success = await driver.swipe(start_x, start_y, end_x, end_y, duration_ms=dur)
                    outcome = (
                        "Swipe completed successfully."
                        if success
                        else f"Swipe coordinates {target} failed"
                    )
                else:
                    raise ValueError(f"Invalid swipe action parameter: {target}")
            elif ctx is not None:
                controller = UnifiedMobileController(ctx)

                if kind == "direction" and isinstance(target, str):
                    indexed_elems = getattr(state, "indexed_elements", None) if state else None
                    ui_hier = getattr(state, "ui_tree", None) if state else None
                    start_x, start_y, end_x, end_y, smart_dur = compute_smart_swipe_coordinates(
                        direction=target,
                        target=params.get("target"),
                        indexed_elements=indexed_elems,
                        ui_hierarchy=ui_hier,
                        width=width,
                        height=height,
                        duration=dur,
                    )
                    dur = smart_dur
                elif kind == "coords" and isinstance(target, list) and len(target) == 4:
                    nx1, ny1, nx2, ny2 = target
                    start_x = int(max(0, min(width - 1, nx1 * width / 1000)))
                    start_y = int(max(0, min(height - 1, ny1 * height / 1000)))
                    end_x = int(max(0, min(width - 1, nx2 * width / 1000)))
                    end_y = int(max(0, min(height - 1, ny2 * height / 1000)))
                else:
                    raise ValueError(f"Invalid swipe action parameter: {target}")

                error = await controller.swipe_coords(start_x, start_y, end_x, end_y, dur)
                success = error is None
                outcome = "Swipe completed successfully." if success else f"Swipe failed: {error}"
            else:
                success = False
                outcome = "Error during swipe: No driver or context provided."
        except Exception as e:  # pylint: disable=broad-exception-caught
            success = False
            outcome = f"Error during swipe: {e}"

        if st and callable(getattr(st, "asanitize_update", None)):
            tool_message = ToolMessage(
                tool_call_id=tcid or "",
                content=outcome,
                status="success" if success else "error",
            )
            return Command(
                update=await st.asanitize_update(
                    ctx=ctx,
                    update={VALIDATOR_MESSAGES_KEY: [tool_message]},
                    agent="validator",
                )
            )

        return outcome


# Universal tool instance & aliases
swipe = SwipeTool()
Swipe = SwipeTool
ToolRegistry.register(swipe)


def get_swipe_tool(ctx: ArtemisContext) -> BaseTool:
    """Exports swipe as a LangChain BaseTool."""
    return trace_langchain_tool(swipe.to_langchain_tool(ctx), ctx)


PRESS_KEY_DOCSTRING = "[ACTION] Press a physical or virtual system button."


class PressKeyTool(ArtemisTool):
    """Universal tool for pressing a physical or virtual system button."""

    def __init__(self, category: ToolCategory = "action"):
        super().__init__(
            name="press_key",
            description=PRESS_KEY_DOCSTRING,
            args_schema=PressKeyArgs,
            category=category,
        )

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    async def execute(
        self,
        driver: BaseDeviceDriver | None = None,
        ctx: ArtemisContext | None = None,
        key: Literal["ENTER", "BACK", "HOME", "APP_SWITCH"] | str | None = None,
        tool_call_id: str | None = None,
        state: State | None = None,
        **kwargs: Any,
    ) -> Any:
        k = key if key is not None else (kwargs.get("key") or kwargs.get("Key") or "")
        tcid = tool_call_id if tool_call_id is not None else kwargs.get("tool_call_id")
        st = state if state is not None else kwargs.get("state")

        try:
            if not k:
                raise ValueError("Key parameter is required.")

            if driver is not None and hasattr(driver, "press_key"):
                success = await driver.press_key(k)
                outcome = (
                    f"Pressed key '{k}' successfully." if success else f"Failed to press key '{k}'."
                )
            elif ctx is not None:
                controller = UnifiedMobileController(ctx)
                success = False
                if k == "ENTER":
                    success = await controller.press_enter()
                elif k == "BACK":
                    success = await controller.go_back()
                elif k == "HOME":
                    success = await controller.go_home()
                elif k == "APP_SWITCH":
                    success = await controller.press_key("KEYCODE_APP_SWITCH")
                else:
                    success = await controller.press_key(k)

                outcome = (
                    f"Pressed key '{k}' successfully." if success else f"Failed to press key '{k}'."
                )
            else:
                success = False
                outcome = "Error during press key: No driver or context provided."
        except Exception as e:  # pylint: disable=broad-exception-caught
            success = False
            outcome = f"Error during press key: {e}"

        if st and callable(getattr(st, "asanitize_update", None)):
            tool_message = ToolMessage(
                tool_call_id=tcid or "",
                content=outcome,
                status="success" if success else "error",
            )
            return Command(
                update=await st.asanitize_update(
                    ctx=ctx,
                    update={VALIDATOR_MESSAGES_KEY: [tool_message]},
                    agent="validator",
                )
            )

        return outcome


# Universal tool instance & aliases
press_key = PressKeyTool()
PressKey = PressKeyTool
ToolRegistry.register(press_key)


def get_press_key_tool(ctx: ArtemisContext) -> BaseTool:
    """Exports press_key as a LangChain BaseTool."""
    return trace_langchain_tool(press_key.to_langchain_tool(ctx), ctx)


# Wrappers to expose
click_wrapper = ToolWrapper(
    tool_fn_getter=get_click_tool,
    on_success_fn=lambda *args, **kwargs: "Success",
    on_failure_fn=lambda *args, **kwargs: "Failure",
)
long_press_wrapper = ToolWrapper(
    tool_fn_getter=get_long_press_tool,
    on_success_fn=lambda *args, **kwargs: "Success",
    on_failure_fn=lambda *args, **kwargs: "Failure",
)
input_text_wrapper = ToolWrapper(
    tool_fn_getter=get_input_text_tool,
    on_success_fn=lambda *args, **kwargs: "Success",
    on_failure_fn=lambda *args, **kwargs: "Failure",
)
swipe_wrapper = ToolWrapper(
    tool_fn_getter=get_swipe_tool,
    on_success_fn=lambda *args, **kwargs: "Success",
    on_failure_fn=lambda *args, **kwargs: "Failure",
)
press_key_wrapper = ToolWrapper(
    tool_fn_getter=get_press_key_tool,
    on_success_fn=lambda *args, **kwargs: "Success",
    on_failure_fn=lambda *args, **kwargs: "Failure",
)
