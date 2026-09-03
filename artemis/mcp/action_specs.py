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

"""The canonical device-action manifest: every dialect of every action, side by side.

Historically each device action was declared independently at three model-facing
sites -- the Operator's inline LangChain shells, the Validator/Flash
``ToolDeclaration`` constants, and the action MCP server's function signatures -- and
the declarations drifted apart (``click.target`` took an element index in one place
and only coordinates in another). This module is now the single place all three are
*defined*; the historical sites import their surface from here, generated.

An action has up to three dialects, and the differences between them are **deliberate
and declared**, not accidental:

* ``operator`` -- the LangChain shell tools the Operator binds. The Operator receives
  an indexed "Visible UI Elements" list and its prompt teaches index-first targeting,
  so target parameters accept ``int`` element indices as well as normalized
  coordinates. The shells are declaration-only ("Action Recorded"): the Operator's
  translate step lowers them into structured decisions.
* ``declaration`` -- the JSON ``ToolDeclaration`` bound by the Validator's
  FlashRunner. These agents act on coordinates directly (the
  element list they see carries coordinates), so target parameters are coordinate
  pairs and the wording teaches the act-then-observe loop ("The screen after X will
  be returned automatically").
* ``wire`` -- what the action MCP server serves, mirroring the actuator protocol
  one-to-one: normalized coordinates, millisecond durations, no addressing sugar.
  Client-side executors (``McpActionExecutor``, ``MobileActionExecutor``) lower the
  agent dialects onto it.

Each ``ActionSpec.param_bridge`` maps operator parameter names onto declaration
parameter names, so every rename (``duration`` -> ``duration_ms``, ``gesture`` ->
``action``) is written down exactly once. ``tests/unit/mcp/test_action_specs.py``
re-derives the bridge from the generated schemas, so an undeclared divergence --
today's definition of drift -- fails CI instead of shipping.

The classification of actions (required/optional/internal) stays in
``action_manifest``; this module holds their schemas and teaching text.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import cache
import inspect
from typing import Annotated, Any, Literal

from langchain_core.tools import StructuredTool
from mcp.types import CallToolResult
from pydantic import Field, create_model

from artemis.core.tool_declaration import ToolDeclaration
from artemis.mcp.action_types import ActionResult

__all__ = [
    "ACTION_SPECS",
    "ActionSpec",
    "DeclarationDialect",
    "EXCEPTION_PREFIXES",
    "OPERATOR_SHELL_ORDER",
    "OperatorDialect",
    "ParamSpec",
    "WireDialect",
    "exception_prefix",
    "make_wire_handler",
    "operator_shell_tool",
    "tool_declaration",
    "wire_dialects",
]


# --- Spec structure ------------------------------------------------------------------


@dataclass(frozen=True)
class ParamSpec:
    """One parameter of a Python-typed dialect (operator shell or wire)."""

    name: str
    annotation: Any
    description: str | None = None
    required: bool = True
    default: Any = None


@dataclass(frozen=True)
class OperatorDialect:
    """The Operator's LangChain shell: index-capable targets, declaration-only body."""

    description: str
    params: tuple[ParamSpec, ...]


@dataclass(frozen=True)
class DeclarationDialect:
    """The Validator/Flash JSON declaration, wording preserved."""

    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class WireDialect:
    """The action-server tool mirroring one actuator method.

    ``bind`` receives the actuator and the validated, default-filled argument dict
    and performs the (tiny) call-site conversion the historical hand-written wrapper
    performed -- typically ``int()`` coercion of coordinate pairs.
    """

    description: str
    params: tuple[ParamSpec, ...]
    bind: Callable[[Any, dict[str, Any]], Awaitable[ActionResult]]


@dataclass(frozen=True)
class ActionSpec:
    """All dialects of one canonical device action, plus their declared differences.

    Attributes:
        name: The canonical tool name every dialect shares.
        operator: Operator shell dialect, or ``None`` when the Operator does not bind
            this action.
        declaration: Validator/Flash dialect, or ``None`` when no JSON declaration
            exists (an action can be wire-only, reachable through executors but never
            declared to a model directly).
        wire: Action-server dialect; ``None`` only for purely virtual actions.
        param_bridge: operator param name -> declaration param name, for every
            operator param, when both dialects exist. Identical names map to
            themselves; renames are the deliberate dialect differences.
        differences: Human-readable record of every deliberate cross-dialect
            divergence (semantics, not just spelling). Empty means the dialects
            align modulo the documented dialect philosophy above.
    """

    name: str
    operator: OperatorDialect | None = None
    declaration: DeclarationDialect | None = None
    wire: WireDialect | None = None
    param_bridge: dict[str, str] = field(default_factory=dict)
    differences: str = ""


# --- Shared type aliases -------------------------------------------------------------

#: Operator-dialect target: element index into the indexed UI list, or a normalized
#: [x, y] pair. Only the Operator sees this union; see the module docstring.
IndexOrCoords = int | list[int]

Direction = Literal["up", "down", "left", "right"]

_COORD_PAIR_JSON = {
    "type": "array",
    "items": {"type": "integer"},
}


# --- Wire bindings -------------------------------------------------------------------
# Each binding reproduces the exact conversion of the historical hand-written FastMCP
# wrapper it replaces. Arguments arrive validated and default-filled.


async def _wire_click(actuator: Any, a: dict[str, Any]) -> ActionResult:
    return await actuator.click(
        int(a["target"][0]), int(a["target"][1]), times=a["times"], delay_ms=a["delay_ms"]
    )


async def _wire_click_sequence(actuator: Any, a: dict[str, Any]) -> ActionResult:
    points = [(int(p[0]), int(p[1])) for p in a["sequence"]]
    return await actuator.click_sequence(points, delay_ms=a["delay_ms"])


async def _wire_long_press(actuator: Any, a: dict[str, Any]) -> ActionResult:
    return await actuator.long_press(
        int(a["target"][0]), int(a["target"][1]), duration_ms=a["duration_ms"]
    )


async def _wire_input_text(actuator: Any, a: dict[str, Any]) -> ActionResult:
    target = a["target"]
    norm = (int(target[0]), int(target[1])) if target else None
    return await actuator.input_text(a["text"], norm, clear_exist=a["clear_exist"])


async def _wire_swipe(actuator: Any, a: dict[str, Any]) -> ActionResult:
    start, end = a["start"], a["end"]
    return await actuator.swipe(
        (int(start[0]), int(start[1])), (int(end[0]), int(end[1])), a["duration_ms"]
    )


async def _wire_press_key(actuator: Any, a: dict[str, Any]) -> ActionResult:
    return await actuator.press_key(a["key"])


async def _wire_manage_app(actuator: Any, a: dict[str, Any]) -> ActionResult:
    return await actuator.manage_app(a["action"], a["app_name"])


async def _wire_wait_for_delay(actuator: Any, a: dict[str, Any]) -> ActionResult:
    return await actuator.wait_for_delay(a["time_in_ms"])


async def _wire_wait_for_text(actuator: Any, a: dict[str, Any]) -> ActionResult:
    return await actuator.wait_for_text(a["text"], a["wait_state"], a["timeout_ms"])


async def _wire_open_link(actuator: Any, a: dict[str, Any]) -> ActionResult:
    return await actuator.open_link(a["url"])


async def _wire_erase_one_char(actuator: Any, a: dict[str, Any]) -> ActionResult:
    return await actuator.erase_one_char()


async def _wire_focus_and_clear_text(actuator: Any, a: dict[str, Any]) -> ActionResult:
    return await actuator.focus_and_clear_text(int(a["target"][0]), int(a["target"][1]))


# --- The manifest --------------------------------------------------------------------

_SPECS: tuple[ActionSpec, ...] = (
    ActionSpec(
        name="click",
        operator=OperatorDialect(
            description=(
                "[ACTION] Click on the target location on the screen (supports element"
                " index or absolute normalized coordinates)."
            ),
            params=(
                ParamSpec(
                    "target",
                    IndexOrCoords,
                    "Click target. Can be an element index number (int, e.g. 3) OR"
                    " normalized coordinates (list of 2 integers, e.g. [500, 600]).",
                ),
                ParamSpec(
                    "times",
                    int,
                    "Number of consecutive clicks on this target. Use this for"
                    " double-clicks or multi-clicks (e.g. 7 to enter developer"
                    " mode). Default is 1.",
                    required=False,
                    default=1,
                ),
                ParamSpec(
                    "delay_ms",
                    int,
                    "Delay in milliseconds between consecutive clicks. Default is 100.",
                    required=False,
                    default=100,
                ),
            ),
        ),
        declaration=DeclarationDialect(
            description=(
                "[ACTION] Tap/click on the target coordinate on the screen. The screen"
                " after click will be returned automatically. For buttons, checkboxes,"
                " tabs, icons, items in a list, tap ON the element. For text fields /"
                " search bars / input boxes, tap INSIDE the input box to focus it."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Normalized coordinates [x, y] in 0-1000 scale.",
                    },
                    "times": {
                        "type": "integer",
                        "description": (
                            "Number of taps to perform (default 1). Set to 2 for double click."
                        ),
                    },
                    "delay_ms": {
                        "type": "integer",
                        "description": "Delay between taps in milliseconds (default 100).",
                    },
                },
                "required": ["target"],
            },
        ),
        wire=WireDialect(
            description="Tap at a 0-1000 normalized [x, y] coordinate.",
            params=(
                ParamSpec("target", list[int]),
                ParamSpec("times", int, required=False, default=1),
                ParamSpec("delay_ms", int, required=False, default=100),
            ),
            bind=_wire_click,
        ),
        param_bridge={"target": "target", "times": "times", "delay_ms": "delay_ms"},
        differences=(
            "target: operator accepts an element index or a coordinate pair; the"
            " declaration and wire dialects take coordinates only."
        ),
    ),
    ActionSpec(
        name="click_sequence",
        declaration=DeclarationDialect(
            description=(
                "[ACTION] Executes a sequence of taps one by one in order on the"
                " specified targets (e.g. [[500, 280], [885, 362]]). The screen will be returned ONLY after all clicks"
                " in the sequence have completed."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "sequence": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": (
                                "Normalized coordinates [x, y] in 0-1000 scale (e.g., [500, 280]),"
                                " or a single integer element index."
                            ),
                        },
                        "description": (
                            "List of targets to tap in sequence, e.g. [[500, 280], [885, 362]]."
                        ),
                    },
                    "delay_ms": {
                        "type": "integer",
                        "description": (
                            "Delay between consecutive taps in milliseconds (default 50ms)."
                        ),
                    },
                },
                "required": ["sequence"],
            },
        ),
        wire=WireDialect(
            description=("Tap a series of 0-1000 normalized [x, y] points in one atomic burst."),
            params=(
                ParamSpec("sequence", list[list[int]]),
                ParamSpec("delay_ms", int, required=False, default=50),
            ),
            bind=_wire_click_sequence,
        ),
        differences=(
            "Declared to Flash only (the Pro Operator chains actions as a fast-action"
            " burst instead); sequence entries may be element indices at the declaration layer"
            " (resolved client-side), while the wire takes coordinate pairs only."
        ),
    ),
    ActionSpec(
        name="long_press",
        operator=OperatorDialect(
            description=(
                "[ACTION] Long press on the target location on the screen (supports"
                " element index or absolute normalized coordinates)."
            ),
            params=(
                ParamSpec(
                    "target",
                    IndexOrCoords,
                    "Long press target. Can be an element index number (int, e.g."
                    " 3) OR normalized coordinates (list of 2 integers, e.g. [500,"
                    " 600]).",
                ),
                ParamSpec(
                    "duration",
                    int,
                    "Long press duration in milliseconds (default 1000).",
                    required=False,
                    default=1000,
                ),
            ),
        ),
        declaration=DeclarationDialect(
            description=(
                "[ACTION] Long press on a target coordinate on the screen. The screen"
                " after long pressing will be returned automatically."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Normalized coordinates [x, y] in 0-1000 scale.",
                    },
                    "duration_ms": {
                        "type": "integer",
                        "description": "Duration of press in milliseconds (default 1000ms).",
                    },
                },
                "required": ["target"],
            },
        ),
        wire=WireDialect(
            description="Long-press at a 0-1000 normalized [x, y] coordinate.",
            params=(
                ParamSpec("target", list[int]),
                ParamSpec("duration_ms", int, required=False, default=1000),
            ),
            bind=_wire_long_press,
        ),
        param_bridge={"target": "target", "duration": "duration_ms"},
        differences=(
            "target: operator accepts an element index or a coordinate pair."
            " duration: the operator spells the duration parameter `duration` (its"
            " structured decisions and recorded traces carry that key); the"
            " declaration and wire dialects spell it `duration_ms`. Executors accept"
            " both spellings."
        ),
    ),
    ActionSpec(
        name="input_text",
        operator=OperatorDialect(
            description=(
                "[ACTION] Type text into the target input field (supports replacing"
                " whole text or appending to the end, and multi-line strings with"
                " '\\n')."
            ),
            params=(
                ParamSpec(
                    "text",
                    str,
                    "The text content to input. Supports multi-line content with '\\n'.",
                ),
                ParamSpec(
                    "target",
                    IndexOrCoords,
                    "Input target field. Can be an input box element index number"
                    " (int, e.g. 3) OR normalized coordinates (list of 2 integers,"
                    " e.g. [500, 600]).",
                ),
                ParamSpec(
                    "clear_exist",
                    bool,
                    "Whether to clear existing text before typing. True (default):"
                    " clear/replace entire text. False: append at the end of"
                    " existing content.",
                    required=False,
                    default=True,
                ),
            ),
        ),
        declaration=DeclarationDialect(
            description=(
                "[ACTION] Type text into an input field on the screen. The screen after"
                " typing will be returned automatically. Automatically taps inside the"
                " input box at target [x, y] to focus before typing."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Text to type into the focused input field.",
                    },
                    "target": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Coordinates [x, y] of the input box in 0-1000 scale.",
                    },
                    "clear_exist": {
                        "type": "boolean",
                        "description": (
                            "Whether to clear existing text in the input box before typing"
                            " (default True)."
                        ),
                    },
                },
                "required": ["text", "target"],
            },
        ),
        wire=WireDialect(
            description=("Type text, optionally focusing a 0-1000 normalized [x, y] target first."),
            params=(
                ParamSpec("text", str),
                ParamSpec("target", list[int] | None, required=False, default=None),
                ParamSpec("clear_exist", bool, required=False, default=True),
            ),
            bind=_wire_input_text,
        ),
        param_bridge={"target": "target", "text": "text", "clear_exist": "clear_exist"},
        differences=(
            "target: operator accepts an element index or a coordinate pair, and"
            " requires a target; the wire dialect allows omitting the target to type"
            " into the already-focused field."
        ),
    ),
    ActionSpec(
        name="swipe",
        operator=OperatorDialect(
            description=(
                "[ACTION] Perform a swipe, drag, or slider-adjustment gesture on the screen.\n"
                "\n"
                "• Directional Scrolling ('direction'): Recommended for general browsing and standard page scrolling in most scenarios. Automatically computes safe swipe vectors and adaptive duration, retains a ~40% visual overlap anchor for zero-omission traversal, and prevents inertial flings. Supports scoping to a sub-container via 'target'. If it fails on certain custom layouts, fall back to specifying exact coordinates ('start' and 'end') directly.\n"
                "• Precise Coordinate Gestures ('start', 'end'): Best for local, fine-grained interactions such as adjusting sliders/SeekBars (e.g., volume, brightness, progress bars), drag-and-drop / list reordering, or as a reliable fallback when directional scrolling fails on specific containers. Always drag slightly PAST the target position to overcome touch slop and reliably trigger the update. When setting a slider to Maximum (100%) or Minimum (0%), swipe fully to the extreme boundary.\n"
                "\n"
                "Args:\n"
                "    direction: Smart directional scrolling ('up', 'down', 'left', 'right'). Automatically computes safe swipe vectors, retaining 40% visual overlap: 'up' (reveal content below), 'down' (reveal content above), 'left', 'right'.\n"
                "    start: Start normalized coordinates [start_x, start_y] in 0-1000 scale.\n"
                "    end: End normalized coordinates [end_x, end_y] in 0-1000 scale.\n"
                "    target: Optional target element index (e.g. 2) or container bounds [left, top, right, bottom] to scope the directional swipe within.\n"
                "    gesture: Backward-compatible parameter: direction string OR custom coordinates list [start_x, start_y, end_x, end_y] in 0-1000 scale.\n"
                "    duration: Optional gesture duration in milliseconds (default 800)."
            ),
            params=(
                ParamSpec(
                    "direction",
                    Direction | None,
                    "Direction for scrolling and swiping: 'up' (drags bottom-to-top, scrolling down to reveal content below),"
                    " 'down' (drags top-to-bottom, scrolling up to reveal content above),"
                    " 'left' (drags right-to-left, scrolling right),"
                    " 'right' (drags left-to-right, scrolling left).",
                    required=False,
                    default=None,
                ),
                ParamSpec(
                    "start",
                    list[int] | None,
                    "Start normalized coordinates [start_x, start_y] in 0-1000 scale for precise,"
                    " local interactions (e.g. adjusting sliders, SeekBars, fine range selection, or drag-and-drop).",
                    required=False,
                    default=None,
                ),
                ParamSpec(
                    "end",
                    list[int] | None,
                    "End normalized coordinates [end_x, end_y] in 0-1000 scale for precise,"
                    " local interactions (e.g. adjusting sliders, SeekBars, fine range selection, or drag-and-drop).",
                    required=False,
                    default=None,
                ),
                ParamSpec(
                    "target",
                    int | list[int] | str | None,
                    "Optional target element index (e.g. 2) or container bounds [left, top, right, bottom] to scope the directional swipe within.",
                    required=False,
                    default=None,
                ),
                ParamSpec(
                    "gesture",
                    Direction | list[int] | None,
                    "Backward-compatible swipe gesture: smart direction string ('up', 'down', 'left', 'right')"
                    " OR precise custom coordinates [start_x, start_y, end_x, end_y] in 0-1000 scale.",
                    required=False,
                    default=None,
                ),
                ParamSpec(
                    "duration",
                    int | None,
                    "Optional swipe/drag duration in milliseconds (default 800; computed"
                    " automatically for directional swipes).",
                    required=False,
                    default=None,
                ),
            ),
        ),
        declaration=DeclarationDialect(
            description=(
                "[ACTION] Perform a swipe, drag, or slider-adjustment gesture on the screen. The screen and UI hierarchy after swipe will be returned automatically.\n\n"
                "• Directional Scrolling ('direction' or 'action'): Recommended for general browsing and standard page scrolling in most scenarios. Automatically computes safe swipe vectors and adaptive duration, retains a ~40% visual overlap anchor for zero-omission traversal, and prevents inertial flings. Supports scoping to a sub-container via 'target'. If it fails on certain custom layouts, fall back to specifying exact coordinates ('start' and 'end') directly.\n"
                "• Precise Coordinate Gestures ('start', 'end' or coordinates list): Best for local, fine-grained interactions such as adjusting sliders/SeekBars (e.g., volume, brightness, progress bars), drag-and-drop / list reordering, or as a reliable fallback when directional scrolling fails on specific containers. Always drag slightly PAST the target position to overcome touch slop and reliably trigger the update. When setting a slider to Maximum (100%) or Minimum (0%), swipe fully to the extreme boundary."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": ["up", "down", "left", "right"],
                        "description": (
                            "Direction for scrolling and swiping: 'up' (drags bottom-to-top, scrolling down to reveal content below),"
                            " 'down' (drags top-to-bottom, scrolling up to reveal content above),"
                            " 'left' (drags right-to-left, scrolling right),"
                            " 'right' (drags left-to-right, scrolling left)."
                        ),
                    },
                    "start": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": (
                            "Start normalized coordinates [start_x, start_y] in 0-1000 scale."
                        ),
                    },
                    "end": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": (
                            "End normalized coordinates [end_x, end_y] in 0-1000 scale."
                        ),
                    },
                    "target": {
                        "description": (
                            "Optional target element index (e.g. 2) or container bounds"
                            " [left, top, right, bottom] to scope the directional swipe within."
                        ),
                    },
                    "action": {
                        "description": (
                            "Backward-compatible swipe gesture: smart direction string ('up', 'down', 'left', 'right')"
                            " OR precise custom coordinates [start_x, start_y, end_x, end_y] in 0-1000 scale."
                        ),
                    },
                    "duration": {
                        "type": "integer",
                        "description": (
                            "Optional swipe/drag duration in milliseconds (default 800). For drag-and-drop,"
                            " list reordering, or sliding/adjusting sliders (e.g., volume, brightness, SeekBars),"
                            " set duration >= 1000 (e.g. 1500). If omitted for directional swipe, duration is computed automatically."
                        ),
                    },
                },
            },
        ),
        wire=WireDialect(
            description="Swipe between two 0-1000 normalized [x, y] points.",
            params=(
                ParamSpec("start", list[int]),
                ParamSpec("end", list[int]),
                ParamSpec("duration_ms", int, required=False, default=800),
            ),
            bind=_wire_swipe,
        ),
        param_bridge={
            "direction": "direction",
            "start": "start",
            "end": "end",
            "target": "target",
            "gesture": "action",
            "duration": "duration",
        },
        differences=(
            "gesture/action: the legacy combined direction-or-coordinates parameter is"
            " spelled `gesture` in the operator dialect and `action` in the"
            " declaration dialect (`action` collides with the operator's structured"
            " decision verb field). Smart directional swipes exist only in the agent"
            " dialects; the wire takes a resolved start/end pair, and direction"
            " resolution happens client-side against the live UI tree."
        ),
    ),
    ActionSpec(
        name="press_key",
        operator=OperatorDialect(
            description=(
                "[ACTION] Press a physical or virtual system button (e.g. ENTER, BACK,"
                " HOME, APP_SWITCH)."
            ),
            params=(
                ParamSpec(
                    "key",
                    Literal["ENTER", "BACK", "HOME", "APP_SWITCH"],
                    "Standard Android system button name (ENTER, BACK, HOME, APP_SWITCH).",
                ),
            ),
        ),
        declaration=DeclarationDialect(
            description=(
                "[ACTION] Press a physical or virtual system button. The screen after"
                " pressing will be returned automatically."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": (
                            "Standard Android system button name (ENTER, BACK, HOME, APP_SWITCH)."
                        ),
                    }
                },
                "required": ["key"],
            },
        ),
        wire=WireDialect(
            description=(
                "Press a device key (home, back, enter, delete, tab, search, menu, app_switch)."
            ),
            params=(ParamSpec("key", str),),
            bind=_wire_press_key,
        ),
        param_bridge={"key": "key"},
        differences=(
            "key vocabulary: the operator dialect is a closed uppercase enum (its"
            " translate step emits Android KEYCODE_* names); the declaration dialect"
            " is an open string taught the same uppercase names; the wire takes the"
            " actuator's bare lowercase key words. `to_canonical_call` and the"
            " executors normalize between them."
        ),
    ),
    ActionSpec(
        name="manage_app",
        operator=OperatorDialect(
            description="[ACTION] Launch or force stop a specified application.",
            params=(
                ParamSpec("action", Literal["launch", "stop"], "The action type."),
                ParamSpec(
                    "app_name",
                    str,
                    "Display name or package name of the application.",
                ),
            ),
        ),
        declaration=DeclarationDialect(
            description=(
                "[ACTION] Launch or force stop a specified application. The screen"
                " after launch/stop will be returned automatically."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "The action type ('launch' or 'stop').",
                    },
                    "app_name": {
                        "type": "string",
                        "description": "Display name or package name of the application.",
                    },
                },
                "required": ["action", "app_name"],
            },
        ),
        wire=WireDialect(
            description="Launch or stop an app by human-readable name or package.",
            params=(
                ParamSpec("action", str),
                ParamSpec("app_name", str),
            ),
            bind=_wire_manage_app,
        ),
        param_bridge={"action": "action", "app_name": "app_name"},
    ),
    ActionSpec(
        name="wait_for_delay",
        operator=OperatorDialect(
            description=(
                "[ACTION] Pause execution and wait for a specified duration in milliseconds.\n"
                "\n"
                "Use this whenever you need time to elapse—whether for UI loading, animations,\n"
                "screen transitions, or longer scheduled delays and intervals specified in the task."
            ),
            params=(
                ParamSpec(
                    "time_in_ms",
                    int,
                    "The exact duration to wait in milliseconds. Accurately convert the"
                    " required time duration into milliseconds based on your objective"
                    " or plan (e.g., 2000 for 2s, 5000 for 5s, 60000 for 1 minute,"
                    " 180000 for 3 minutes, 300000 for 5 minutes).",
                ),
            ),
        ),
        declaration=DeclarationDialect(
            description=(
                "[ACTION] Pause execution and wait for a specified duration in milliseconds."
                " The screen after pause will be returned automatically."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "time_in_ms": {
                        "type": "integer",
                        "description": (
                            "The exact duration to pause in milliseconds (e.g., 2000 for 2s,"
                            " 60000 for 1 min, 180000 for 3 mins, 300000 for 5 mins)."
                            " Convert any required waiting duration into milliseconds."
                        ),
                    }
                },
                "required": ["time_in_ms"],
            },
        ),
        wire=WireDialect(
            description="Wait for a fixed number of milliseconds.",
            params=(ParamSpec("time_in_ms", int),),
            bind=_wire_wait_for_delay,
        ),
        param_bridge={"time_in_ms": "time_in_ms"},
    ),
    ActionSpec(
        name="wait_for_text",
        declaration=None,  # Listed in the legacy tool-name sets but never declared to
        # any LLM; kept wire-reachable for executors and external MCP clients.
        wire=WireDialect(
            description="Wait for text to appear on or disappear from the screen.",
            params=(
                ParamSpec("text", str),
                ParamSpec("wait_state", str, required=False, default="appear"),
                ParamSpec("timeout_ms", int, required=False, default=5000),
            ),
            bind=_wire_wait_for_text,
        ),
    ),
    ActionSpec(
        name="open_link",
        wire=WireDialect(
            description="Open a URL on the device.",
            params=(ParamSpec("url", str),),
            bind=_wire_open_link,
        ),
    ),
    ActionSpec(
        name="erase_one_char",
        wire=WireDialect(
            description="Erase a single character in the focused field.",
            params=(),
            bind=_wire_erase_one_char,
        ),
    ),
    ActionSpec(
        name="focus_and_clear_text",
        wire=WireDialect(
            description=("Focus the field at a 0-1000 normalized [x, y] and clear its text."),
            params=(ParamSpec("target", list[int]),),
            bind=_wire_focus_and_clear_text,
        ),
    ),
)

#: Canonical manifest, keyed by action name, in declaration order.
ACTION_SPECS: dict[str, ActionSpec] = {spec.name: spec for spec in _SPECS}

#: The order the Operator binds its shells (historically the factory-dict order,
#: identical to the prompt's "Physical device actions" enumeration order).
OPERATOR_SHELL_ORDER: tuple[str, ...] = (
    "click",
    "input_text",
    "swipe",
    "press_key",
    "manage_app",
    "wait_for_delay",
    "long_press",
)

#: Exception wording per action; matches the historical executor `except` arms.
EXCEPTION_PREFIXES: dict[str, str] = {
    "click": "Error during click",
    "click_sequence": "Error executing click sequence",
    "long_press": "Error during long press",
    "input_text": "Error during input text",
    "swipe": "Error during swipe",
    "press_key": "Error during press_key",
    "manage_app": "Error during manage_app",
    "wait_for_delay": "Error during wait_for_delay",
    "wait_for_text": "Error during wait_for_text",
}


def exception_prefix(action: str) -> str:
    """Returns the historical human-readable exception prefix for an action."""
    return EXCEPTION_PREFIXES.get(action, f"Error during {action}")


# --- Projections ---------------------------------------------------------------------


@cache
def _operator_args_model(name: str):
    """Builds (once) the pydantic argument model for an operator shell."""
    dialect = ACTION_SPECS[name].operator
    if dialect is None:
        raise ValueError(f"Action '{name}' has no operator dialect.")
    fields: dict[str, Any] = {}
    for p in dialect.params:
        if p.required:
            fields[p.name] = (p.annotation, Field(description=p.description))
        else:
            fields[p.name] = (
                p.annotation,
                Field(default=p.default, description=p.description),
            )
    return create_model(name, **fields)


def operator_shell_tool(name: str) -> StructuredTool:
    """Builds the Operator's declaration-only shell for one action.

    The body is never executed by the Operator loop -- action calls are translated
    into structured decisions instead -- but returns the historical marker string for
    any caller that does invoke it.
    """
    dialect = ACTION_SPECS[name].operator
    if dialect is None:
        raise ValueError(f"Action '{name}' has no operator dialect.")
    return StructuredTool(
        name=name,
        description=dialect.description,
        args_schema=_operator_args_model(name),
        func=lambda **kwargs: "Action Recorded",
    )


def tool_declaration(name: str) -> ToolDeclaration:
    """Builds the Validator/Flash ``ToolDeclaration`` for one action."""
    dialect = ACTION_SPECS[name].declaration
    if dialect is None:
        raise ValueError(f"Action '{name}' has no declaration dialect.")
    return ToolDeclaration(
        name=name,
        description=dialect.description,
        parameters=dialect.parameters,
    )


def wire_dialects() -> tuple[ActionSpec, ...]:
    """Returns every spec that has a wire dialect, in manifest order."""
    return tuple(spec for spec in _SPECS if spec.wire is not None)


def make_wire_handler(
    spec: ActionSpec,
    actuator: Any,
    wrap: Callable[[ActionResult], CallToolResult],
    wrap_exception: Callable[[str, Exception], CallToolResult],
) -> Callable[..., Awaitable[CallToolResult]]:
    """Builds the FastMCP tool function for one wire dialect.

    The returned coroutine carries an explicit ``__signature__`` so FastMCP derives
    the same argument model a literal ``async def`` produced historically, and the
    same ``Annotated[CallToolResult, ActionResult]`` return annotation so the
    structured-output schema is unchanged.
    """
    wire = spec.wire
    if wire is None:
        raise ValueError(f"Action '{spec.name}' has no wire dialect.")

    async def handler(**kwargs: Any) -> Any:
        try:
            return wrap(await wire.bind(actuator, kwargs))
        except Exception as e:  # pylint: disable=broad-exception-caught
            return wrap_exception(spec.name, e)

    return_annotation = Annotated[CallToolResult, ActionResult]
    parameters = [
        inspect.Parameter(
            p.name,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=p.annotation,
            default=inspect.Parameter.empty if p.required else p.default,
        )
        for p in wire.params
    ]
    handler.__name__ = spec.name
    handler.__qualname__ = spec.name
    handler.__doc__ = wire.description
    handler.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        parameters, return_annotation=return_annotation
    )
    handler.__annotations__ = {p.name: p.annotation for p in wire.params}
    handler.__annotations__["return"] = return_annotation
    return handler
