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

An action has one agent-facing dialect and one wire dialect:

* ``operator`` -- the agent dialect. Both profiles receive the same indexed
  "Visible UI Elements" list, so target parameters accept ``int`` element indices
  as well as normalized coordinates. The Pro Operator binds it as a LangChain shell
  (declaration-only, "Action Recorded": the Operator's translate step lowers the
  call into structured decisions); the FlashRunner binds the very same description
  and parameters projected onto a JSON ``ToolDeclaration`` (:func:`tool_declaration`),
  and ``McpActionExecutor`` resolves an index against the current element list.
  One definition, two bindings: the two profiles can no longer drift apart.
* ``declaration`` -- a JSON-only agent dialect, used solely for actions that have
  no operator shell (``click_sequence`` is declared to Flash alone).
* ``wire`` -- what the action MCP server serves, mirroring the actuator protocol
  one-to-one: normalized coordinates, millisecond durations, no addressing sugar.
  The client-side executor (``McpActionExecutor``) lowers the agent dialect onto it.

A coordinate target names nothing by itself, so the agent dialect pairs it with
``target_description`` (``target_descriptions`` for a ``click_sequence``): the
model's own statement of what it is aiming at. It is required whenever the target
is a coordinate, recorded verbatim on the action, and never sent to the wire. An
element index needs none -- its text, bounds and id are read from the indexed
list -- and the recorded fields stay separate (``target_text``/``target_bounds``/...
for observed elements, ``target_description`` for described coordinates) so no
reader can mistake the model's belief for an observation.

``tests/unit/mcp/test_action_specs.py`` pins the generated schema of every model
surface and checks that the Flash declaration of each shared action is exactly the
operator shell's schema, so a divergence between what one profile sees and what the
other sees -- the historical definition of drift -- fails CI instead of shipping.

The classification of actions (required/optional/internal) stays in
``action_manifest``; this module holds their schemas and teaching text.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import cache
import inspect
import types
from typing import Annotated, Any, Literal, Union, get_args, get_origin

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
    """The agent dialect: index-capable targets, one wording for both profiles.

    Bound by the Pro Operator as a declaration-only LangChain shell and by the
    FlashRunner as the JSON projection of the same parameters.
    """

    description: str
    params: tuple[ParamSpec, ...]


@dataclass(frozen=True)
class DeclarationDialect:
    """A JSON-only agent dialect, for actions without an operator shell."""

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
        operator: The agent dialect (Operator shell and, projected, the Flash
            declaration), or ``None`` when neither profile binds this action as a
            shell.
        declaration: A JSON-only agent dialect for actions without an operator
            shell, or ``None`` (an action can be wire-only, reachable through
            executors but never declared to a model directly).
        wire: Action-server dialect; ``None`` only for purely virtual actions.
        differences: Human-readable record of every deliberate divergence between
            the agent dialect and the wire (semantics, not just spelling). Empty
            means the dialects align modulo the documented dialect philosophy above.
    """

    name: str
    operator: OperatorDialect | None = None
    declaration: DeclarationDialect | None = None
    wire: WireDialect | None = None
    differences: str = ""


# --- Shared type aliases -------------------------------------------------------------

#: Agent-dialect target: element index into the indexed UI list, or a normalized
#: [x, y] pair.
IndexOrCoords = int | list[int]

Direction = Literal["up", "down", "left", "right"]

#: Wording for the description that a coordinate target must carry. An element
#: index already names its element (its text, id and bounds are recorded from the
#: indexed list); a bare coordinate names nothing, so the model states what it is
#: aiming at and that statement is recorded as ``target_description``.
_TARGET_DESCRIPTION = (
    "What the target is, in a few words (e.g. 'play button', 'search input',"
    " 'video body'). REQUIRED when target is a coordinate pair; ignored for an"
    " element index."
)


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
                    "target_description",
                    str | None,
                    _TARGET_DESCRIPTION,
                    required=False,
                    default=None,
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
        wire=WireDialect(
            description="Tap at a 0-1000 normalized [x, y] coordinate.",
            params=(
                ParamSpec("target", list[int]),
                ParamSpec("times", int, required=False, default=1),
                ParamSpec("delay_ms", int, required=False, default=100),
            ),
            bind=_wire_click,
        ),
        differences=(
            "target: the agent dialect accepts an element index or a coordinate pair"
            " (an index is resolved client-side against the indexed element list); the"
            " wire takes coordinates only. target_description: recorded, never sent"
            " to the wire; required only for coordinate targets."
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
                                "Normalized coordinates [x, y] in 0-1000 scale (e.g., [500, 280])."
                            ),
                        },
                        "description": (
                            "List of targets to tap in sequence, e.g. [[500, 280], [885, 362]]."
                        ),
                    },
                    "target_descriptions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "What each target is, in a few words, one entry per sequence"
                            " entry in the same order (e.g. ['video body', 'skip button'])."
                            " Required: a coordinate names nothing by itself."
                        ),
                    },
                    "delay_ms": {
                        "type": "integer",
                        "description": (
                            "Delay between consecutive taps in milliseconds (default 50ms)."
                        ),
                    },
                },
                "required": ["sequence", "target_descriptions"],
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
            " burst instead); sequence entries are normalized coordinate pairs in both"
            " dialects (an element index is refused, never resolved client-side)."
            " target_descriptions is recorded per entry and never sent to the wire."
            " Unlike click, a burst is not resolved against the element list: its"
            " whole point is to outrun a transient UI, and the list was captured"
            " before the trigger tap."
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
                    "target_description",
                    str | None,
                    _TARGET_DESCRIPTION,
                    required=False,
                    default=None,
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
        wire=WireDialect(
            description="Long-press at a 0-1000 normalized [x, y] coordinate.",
            params=(
                ParamSpec("target", list[int]),
                ParamSpec("duration_ms", int, required=False, default=1000),
            ),
            bind=_wire_long_press,
        ),
        differences=(
            "target: the agent dialect accepts an element index or a coordinate pair;"
            " the wire takes coordinates only. target_description: recorded, never"
            " sent to the wire; required only for coordinate targets."
            " duration: the agent dialect spells the duration parameter `duration`"
            " (structured decisions and recorded traces carry that key); the wire"
            " spells it `duration_ms`. Executors accept both spellings."
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
                    "target_description",
                    str | None,
                    _TARGET_DESCRIPTION,
                    required=False,
                    default=None,
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
        wire=WireDialect(
            description=("Type text, optionally focusing a 0-1000 normalized [x, y] target first."),
            params=(
                ParamSpec("text", str),
                ParamSpec("target", list[int] | None, required=False, default=None),
                ParamSpec("clear_exist", bool, required=False, default=True),
            ),
            bind=_wire_input_text,
        ),
        differences=(
            "target: the agent dialect accepts an element index or a coordinate pair,"
            " and requires a target; the wire allows omitting the target to type into"
            " the already-focused field. target_description: recorded, never sent to"
            " the wire; required only for coordinate targets."
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
                "    target_description: What is being dragged, in a few words (e.g. 'brightness slider knob'). REQUIRED for coordinate gestures ('start'/'end' or a coordinates list); ignored for directional scrolling.\n"
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
                    "target_description",
                    str | None,
                    "What is being dragged, in a few words (e.g. 'brightness slider"
                    " knob'). REQUIRED for coordinate gestures ('start'/'end' or a"
                    " coordinates list); ignored for directional scrolling.",
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
        wire=WireDialect(
            description="Swipe between two 0-1000 normalized [x, y] points.",
            params=(
                ParamSpec("start", list[int]),
                ParamSpec("end", list[int]),
                ParamSpec("duration_ms", int, required=False, default=800),
            ),
            bind=_wire_swipe,
        ),
        differences=(
            "target_description: recorded, never sent to the wire; required for"
            " coordinate gestures only. gesture: the legacy combined"
            " direction-or-coordinates parameter (executors also accept the older"
            " `action` spelling). Smart directional swipes exist only in the agent"
            " dialect; the wire takes a resolved start/end pair, and direction"
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
        wire=WireDialect(
            description=(
                "Press a device key (home, back, enter, delete, tab, search, menu, app_switch)."
            ),
            params=(ParamSpec("key", str),),
            bind=_wire_press_key,
        ),
        differences=(
            "key vocabulary: the agent dialect is a closed uppercase enum (the"
            " Operator's translate step emits Android KEYCODE_* names); the wire"
            " takes the actuator's bare lowercase key words. `to_canonical_call` and"
            " the executors normalize between them."
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
        wire=WireDialect(
            description="Launch or stop an app by human-readable name or package.",
            params=(
                ParamSpec("action", str),
                ParamSpec("app_name", str),
            ),
            bind=_wire_manage_app,
        ),
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
        wire=WireDialect(
            description="Wait for a fixed number of milliseconds.",
            params=(ParamSpec("time_in_ms", int),),
            bind=_wire_wait_for_delay,
        ),
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


_SCALAR_JSON: dict[Any, dict[str, str]] = {
    bool: {"type": "boolean"},
    int: {"type": "integer"},
    float: {"type": "number"},
    str: {"type": "string"},
}


def _json_schema(annotation: Any) -> dict[str, Any]:
    """Projects one Python-typed parameter annotation onto JSON schema.

    ``None`` members of a union only make the parameter optional (its
    ``required`` flag says so); they never appear in the projected schema, so
    the declaration carries no ``null`` alternatives or ``default`` noise.
    """
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        members = [a for a in get_args(annotation) if a is not type(None)]
        schemas = [_json_schema(m) for m in members]
        return schemas[0] if len(schemas) == 1 else {"anyOf": schemas}
    if origin is Literal:
        return {"type": "string", "enum": list(get_args(annotation))}
    if origin is list:
        (item,) = get_args(annotation) or (str,)
        return {"type": "array", "items": _json_schema(item)}
    try:
        return dict(_SCALAR_JSON[annotation])
    except KeyError:
        raise TypeError(f"No JSON projection for parameter annotation {annotation!r}.") from None


def _projected_parameters(dialect: OperatorDialect) -> dict[str, Any]:
    """The operator shell's parameters as a JSON-schema ``parameters`` object."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for p in dialect.params:
        schema = _json_schema(p.annotation)
        if p.description:
            schema["description"] = p.description
        properties[p.name] = schema
        if p.required:
            required.append(p.name)
    return {"type": "object", "properties": properties, "required": required}


@cache
def tool_declaration(name: str) -> ToolDeclaration:
    """Builds the Flash ``ToolDeclaration`` for one action.

    A shared action is the operator shell projected onto JSON (same description,
    same parameters, same targeting semantics); a JSON-only action uses its own
    ``DeclarationDialect``.
    """
    spec = ACTION_SPECS[name]
    if spec.declaration is not None:
        return ToolDeclaration(
            name=name,
            description=spec.declaration.description,
            parameters=spec.declaration.parameters,
        )
    if spec.operator is not None:
        return ToolDeclaration(
            name=name,
            description=spec.operator.description,
            parameters=_projected_parameters(spec.operator),
        )
    raise ValueError(f"Action '{name}' has no agent dialect to declare.")


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
