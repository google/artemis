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

"""Contract layer between actuator backends and the agent-facing tool surface.

This module is the single place that declares *what a backend must provide*. It sits
between the actuator implementations (``artemis/mcp/actuators/``) and the agents that
consume tools, so that a backend -- ADB today, a robot arm tomorrow -- can omit tools
it cannot perform, and add tools nobody anticipated, without any agent knowing.

Two rules govern the split between required and optional:

1. A tool is REQUIRED only when a prompt depends on it *structurally*, i.e. the prompt
   cannot be assembled around its absence. Today that is ``click_sequence`` alone: the
   two ``*_disappeared_analyzer.md`` prompts are built around "atomic chained execution
   to defeat turn latency", which *is* ``click_sequence``.
2. Everything else is OPTIONAL, because every other prompt reference lives in a tool
   enumeration slot or a self-contained instruction block that the assembly layer
   (``artemis/agents/prompt_assembly.py``) renders conditionally. An optional tool that
   a backend does not implement disappears from the declarations *and* from the prompt,
   so it costs the model nothing.

``tests/unit/mcp/test_action_manifest.py`` re-derives rule 2 by scanning the prompt
sources, so adding a tool name to a prompt without classifying it here fails CI.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ActuatorContractError",
    "AGENT_ROLES",
    "ALL_KNOWN_TOOLS",
    "BACKEND_INDEPENDENT_TOOLS",
    "DEVICE_ACTIONS",
    "ExtensionTool",
    "INTERNAL_ACTIONS",
    "MIN_EXTENSION_DESCRIPTION_LENGTH",
    "OPTIONAL_ACTIONS",
    "REQUIRED_ACTIONS",
    "available_device_actions",
    "filter_declarations",
    "validate_actuator",
]


# --- Tool classification -------------------------------------------------------------

#: Device actions a backend MUST implement. See rule 1 in the module docstring.
#:
#: ``click_sequence`` implies the ability to tap, so a backend providing it can
#: trivially provide ``click`` as well; ``click`` is nonetheless optional because no
#: prompt depends on it structurally once the enumeration slots are assembled.
REQUIRED_ACTIONS: frozenset[str] = frozenset(
    {
        "click_sequence",
    }
)

#: Device actions a backend MAY implement. Absent ones vanish from both the tool
#: declarations and the rendered prompts.
OPTIONAL_ACTIONS: frozenset[str] = frozenset(
    {
        "click",
        "long_press",
        "input_text",
        "swipe",
        "press_key",
        "manage_app",
        "wait_for_delay",
        # The four below appear in no prompt at all. `wait_for_text` corroborates the
        # classification: it is listed in the legacy ACTION_TOOL_NAMES but has never had
        # a ToolDeclaration, i.e. it has always been unreachable by any LLM.
        "wait_for_text",
        "open_link",
        "erase_one_char",
        "focus_and_clear_text",
    }
)

#: Used by the client-side adapter only; never declared to any LLM, so the
#: "no cognitive load" rule does not apply. The adapter cannot function without them,
#: so they are validated as strictly as REQUIRED_ACTIONS.
INTERNAL_ACTIONS: frozenset[str] = frozenset(
    {
        "observe_screen",
        "take_screenshot",
        "get_ui_hierarchy",
    }
)

#: Tools that are not device control at all -- notes, sub-agents, diagnostics, and the
#: loop-termination sentinels. They are unaffected by which actuator is installed.
#:
#: ``run_adb_command`` and ``manage_app`` ride the *system channel* rather than physical
#: actuation: a robot arm driving a handset still has ADB attached, so ``am start``
#: suffices and no icon-hunting is required. (``manage_app`` is nonetheless classified
#: as an optional device action above, since it is declared through the same path.)
BACKEND_INDEPENDENT_TOOLS: frozenset[str] = frozenset(
    {
        "read_note",
        "list_notes",
        "save_note",
        "update_note",
        "append_note",
        "ask_explorer",
        "ask_diagnoser",
        "ask_committee",
        "video_analyzer",
        "run_adb_command",
        "manage_task",
        "analyze_task_output",
        "reply_to_checker",
        "report_task_status",
        "report_failure_analysis",
    }
)

#: Everything an actuator may legitimately claim in ``capabilities()``.
DEVICE_ACTIONS: frozenset[str] = REQUIRED_ACTIONS | OPTIONAL_ACTIONS | INTERNAL_ACTIONS

#: Every name the manifest knows about. Extension tools must not collide with these.
ALL_KNOWN_TOOLS: frozenset[str] = DEVICE_ACTIONS | BACKEND_INDEPENDENT_TOOLS

#: Agent roles an extension tool may target.
AGENT_ROLES: frozenset[str] = frozenset(
    {
        "operator",
        "flash",
        "failure_analyzer",
        "validator",
    }
)

#: Extension descriptions carry the entire usage contract (see ``ExtensionTool``), so a
#: one-word description is a bug rather than a terse style choice.
MIN_EXTENSION_DESCRIPTION_LENGTH = 40


class ActuatorContractError(RuntimeError):
    """Raised when an actuator does not satisfy the manifest contract.

    Always raised at session start rather than mid-run, so a misconfigured backend
    fails before the agent has taken any action on the device.
    """


@dataclass(frozen=True)
class ExtensionTool:
    """A tool contributed by a backend that the manifest does not know about.

    Extension tools are invisible to the prompt layer by construction: prompts are
    static text that cannot anticipate future backends, so an extension gets **no**
    prompt-level teaching. Its ``description`` must therefore be self-contained --
    stating when to reach for it and what it does -- or the model will never use it
    correctly. :func:`validate_actuator` enforces a length floor as a crude guard.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Awaitable[Any]]
    targets: frozenset[str] = field(default=AGENT_ROLES)


# --- Contract validation -------------------------------------------------------------


def validate_actuator(actuator: Any) -> None:
    """Checks an actuator against the manifest, raising on any violation.

    Called once from ``get_action_session`` before the first device interaction.
    """
    caps = frozenset(actuator.capabilities())

    missing_required = REQUIRED_ACTIONS - caps
    if missing_required:
        raise ActuatorContractError(
            f"{type(actuator).__name__} does not implement required action(s): "
            f"{sorted(missing_required)}. These are structurally depended on by prompts "
            f"and cannot be assembled away."
        )

    missing_internal = INTERNAL_ACTIONS - caps
    if missing_internal:
        raise ActuatorContractError(
            f"{type(actuator).__name__} does not implement internal action(s): "
            f"{sorted(missing_internal)}. The client-side adapter cannot observe the "
            f"device without them."
        )

    unknown = caps - DEVICE_ACTIONS
    if unknown:
        raise ActuatorContractError(
            f"{type(actuator).__name__} claims capabilities unknown to the manifest: "
            f"{sorted(unknown)}. Backend-specific tools belong in extensions(), not "
            f"capabilities()."
        )

    _validate_extensions(actuator)


def _validate_extensions(actuator: Any) -> None:
    """Validates extension tools for name collisions and self-sufficient descriptions."""
    extensions = list(actuator.extensions())
    seen: set[str] = set()

    for ext in extensions:
        if ext.name in ALL_KNOWN_TOOLS:
            raise ActuatorContractError(
                f"Extension tool '{ext.name}' collides with a manifest tool of the same "
                f"name. Extension names must be unique across the whole tool surface."
            )
        if ext.name in seen:
            raise ActuatorContractError(
                f"{type(actuator).__name__} registers extension '{ext.name}' more than once."
            )
        seen.add(ext.name)

        unknown_targets = frozenset(ext.targets) - AGENT_ROLES
        if unknown_targets:
            raise ActuatorContractError(
                f"Extension tool '{ext.name}' targets unknown agent role(s): "
                f"{sorted(unknown_targets)}. Valid roles: {sorted(AGENT_ROLES)}."
            )

        if len(ext.description.strip()) < MIN_EXTENSION_DESCRIPTION_LENGTH:
            raise ActuatorContractError(
                f"Extension tool '{ext.name}' has a description of "
                f"{len(ext.description.strip())} characters. Extensions receive no "
                f"prompt-level guidance, so the description must state when to use the "
                f"tool and what it does (minimum "
                f"{MIN_EXTENSION_DESCRIPTION_LENGTH} characters)."
            )


# --- Surface derivation --------------------------------------------------------------


def available_device_actions(actuator: Any) -> frozenset[str]:
    """Returns the LLM-facing device actions this actuator supports.

    Internal actions are excluded: they are adapter plumbing and never reach a model.
    """
    caps = frozenset(actuator.capabilities())
    return (REQUIRED_ACTIONS | OPTIONAL_ACTIONS) & caps


def filter_declarations(
    declarations: list[Any],
    actuator: Any,
    agent: str,
) -> list[Any]:
    """Filters a declaration list down to what ``actuator`` actually provides.

    Declarations for known tools keep coming from the hand-tuned ``ToolDeclaration``
    constants -- this layer only removes entries, it never rewrites descriptions.
    Extension declarations are appended from the actuator itself, since the manifest
    cannot know them in advance.

    Args:
        declarations: Declarations the agent would use if every tool existed.
        actuator: The installed backend.
        agent: One of :data:`AGENT_ROLES`; selects which extensions apply.

    Returns:
        Declarations in their original order, with unavailable device actions dropped
        and this agent's extension tools appended.
    """
    if agent not in AGENT_ROLES:
        raise ValueError(f"Unknown agent role '{agent}'. Valid roles: {sorted(AGENT_ROLES)}.")

    available = available_device_actions(actuator)

    kept = [
        decl
        for decl in declarations
        # Only device actions are gated. Backend-independent tools and anything the
        # manifest does not classify (e.g. an agent's own bespoke tool) pass through.
        if _declaration_name(decl) not in DEVICE_ACTIONS
        or _declaration_name(decl) in available
    ]

    for ext in actuator.extensions():
        if agent in ext.targets:
            kept.append(_extension_declaration(ext))

    return kept


def _declaration_name(declaration: Any) -> str:
    """Reads a tool name off either a ToolDeclaration mapping or a LangChain tool."""
    name = getattr(declaration, "name", None)
    if name is None and isinstance(declaration, dict):
        name = declaration.get("name")
    return name or ""


def _extension_declaration(ext: ExtensionTool) -> Any:
    """Renders an ExtensionTool into the same ToolDeclaration shape as known tools."""
    from artemis.core.tool_declaration import ToolDeclaration

    return ToolDeclaration(
        name=ext.name,
        description=ext.description,
        parameters=ext.input_schema,
    )
