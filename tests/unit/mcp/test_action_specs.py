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

"""The canonical action manifest's conformance suite.

Two kinds of protection:

* **Fixture pins** freeze the exact schema each model surface receives (operator
  shells, Flash declarations, the action server manifest). A schema change must be a
  conscious act: regenerate the fixture in the same commit and say why.
* **Projection identity** checks that the Flash declaration of every shared action
  is the operator shell's own description and parameters, so the two profiles can
  never see different tools under one name -- the historical definition of drift.
"""

import json
from pathlib import Path

from langchain_core.utils.function_calling import convert_to_openai_tool
import pytest

from artemis.agents.operator.prompts import (
    _PHYSICAL_ACTIONS_ORDER,
    _TURN_ENDING_ORDER,
    OPERATOR_PROMPT_TOOLSET,
)
from artemis.agents.validator.tool_declarations import VALIDATOR_TOOLS_DECLARATION
from artemis.mcp.action_manifest import (
    INTERNAL_ACTIONS,
    OPTIONAL_ACTIONS,
    REQUIRED_ACTIONS,
)
from artemis.mcp.action_names import OPERATOR_ACTION_TO_CANONICAL
from artemis.mcp.action_specs import (
    ACTION_SPECS,
    OPERATOR_SHELL_ORDER,
    operator_shell_tool,
    tool_declaration,
    wire_dialects,
)
from artemis.mcp.actuators.base import Actuator

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "action_surfaces"

FIXTURE_HINT = (
    "The generated schema differs from the pinned fixture. If this change is"
    " intentional, regenerate the fixture in the same commit and explain the schema"
    " change in the commit message; models see this schema directly."
)


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# --- Fixture pins --------------------------------------------------------------------


def test_operator_shells_match_fixture():
    expected = _fixture("operator_shells.json")
    assert set(expected) == set(OPERATOR_SHELL_ORDER)
    for name in OPERATOR_SHELL_ORDER:
        generated = convert_to_openai_tool(operator_shell_tool(name))
        assert generated == expected[name], f"operator shell '{name}': {FIXTURE_HINT}"


def test_tool_declarations_match_fixture():
    expected = _fixture("tool_declarations.json")
    by_constant = {
        "CLICK_TOOL": "click",
        "CLICK_SEQUENCE_TOOL": "click_sequence",
        "LONG_PRESS_TOOL": "long_press",
        "INPUT_TEXT_TOOL": "input_text",
        "SWIPE_TOOL": "swipe",
        "PRESS_KEY_TOOL": "press_key",
        "MANAGE_APP_TOOL": "manage_app",
        "WAIT_FOR_DELAY_TOOL": "wait_for_delay",
    }
    assert set(expected) == set(by_constant)
    for constant, name in by_constant.items():
        assert dict(tool_declaration(name)) == expected[constant], (
            f"declaration '{name}': {FIXTURE_HINT}"
        )


@pytest.mark.asyncio
async def test_action_server_manifest_matches_fixture():
    from artemis.mcp.action_server import build_action_server
    from artemis.mcp.actuators import MockActuator

    expected = _fixture("action_server_manifest.json")
    server = build_action_server(MockActuator())
    tools = await server.list_tools()
    generated = {
        t.name: {
            "description": t.description,
            "inputSchema": t.inputSchema,
            "outputSchema": t.outputSchema,
        }
        for t in tools
    }
    assert set(generated) == set(expected)
    for name in expected:
        assert generated[name] == expected[name], f"server tool '{name}': {FIXTURE_HINT}"


def test_validator_declaration_order_is_stable():
    assert [d.name for d in VALIDATOR_TOOLS_DECLARATION] == [
        "click",
        "long_press",
        "input_text",
        "swipe",
        "press_key",
        "read_note",
        "list_notes",
        "manage_app",
        "wait_for_delay",
    ]


# --- Projection identity: Flash binds the operator shell, projected ------------------


def _without_optional_noise(schema: dict) -> dict:
    """The pydantic-generated property schema minus what only marks optionality.

    ``default`` and the ``null`` alternative say "optional"; the declaration says
    that through ``required`` alone.
    """
    out = {k: v for k, v in schema.items() if k not in ("anyOf", "default")}
    if "anyOf" in schema:
        members = [m for m in schema["anyOf"] if m.get("type") != "null"]
        out.update(members[0] if len(members) == 1 else {"anyOf": members})
    return out


def test_flash_declarations_are_the_operator_shells_projected():
    """One definition, two bindings: for every action with an operator shell, the
    Flash ``ToolDeclaration`` carries the shell's description, parameter names,
    descriptions, types (index-or-coordinates unions included) and required set."""
    for spec in ACTION_SPECS.values():
        if spec.operator is None:
            continue
        shell = convert_to_openai_tool(operator_shell_tool(spec.name))["function"]
        declaration = tool_declaration(spec.name)
        assert declaration.description == shell["description"], spec.name
        shell_props = shell["parameters"]["properties"]
        decl_props = declaration.parameters["properties"]
        assert list(decl_props) == list(shell_props), spec.name
        for param, schema in shell_props.items():
            assert decl_props[param] == _without_optional_noise(schema), (
                f"'{spec.name}.{param}' diverges between the operator shell and the"
                " Flash declaration."
            )
        assert declaration.parameters["required"] == shell["parameters"].get("required", []), (
            spec.name
        )


def test_point_targets_accept_an_index_or_coordinates_in_both_profiles():
    """click / long_press / input_text address an element by index or by
    normalized coordinates; the description is required only for coordinates."""
    for name in ("click", "long_press", "input_text"):
        target = tool_declaration(name).parameters["properties"]["target"]
        assert target["anyOf"] == [
            {"type": "integer"},
            {"type": "array", "items": {"type": "integer"}},
        ], name
        assert "target_description" not in tool_declaration(name).parameters["required"], name
    assert tool_declaration("click_sequence").parameters["required"] == [
        "sequence",
        "target_descriptions",
    ]


def test_shared_descriptions_are_identical_between_profiles():
    for name in ("swipe", "press_key", "manage_app", "wait_for_delay"):
        assert tool_declaration(name).description == operator_shell_tool(name).description


def test_documented_differences_concern_the_wire_only():
    """The agent dialect is single-sourced; a spec may only document how the
    agent dialect differs from the wire (spelling, vocabulary, resolution)."""
    for spec in ACTION_SPECS.values():
        assert "declaration dialect" not in spec.differences, spec.name


# --- Manifest coverage ---------------------------------------------------------------


def test_specs_cover_exactly_the_llm_reachable_device_actions():
    assert set(ACTION_SPECS) == (REQUIRED_ACTIONS | OPTIONAL_ACTIONS)
    assert not set(ACTION_SPECS) & INTERNAL_ACTIONS


def test_operator_shell_order_covers_operator_dialects():
    with_operator = {s.name for s in ACTION_SPECS.values() if s.operator is not None}
    assert set(OPERATOR_SHELL_ORDER) == with_operator
    assert len(OPERATOR_SHELL_ORDER) == len(set(OPERATOR_SHELL_ORDER))


def test_every_wire_dialect_matches_the_actuator_protocol():
    for spec in wire_dialects():
        method = getattr(Actuator, spec.name, None)
        assert callable(method), f"wire dialect '{spec.name}' has no corresponding Actuator method."


def test_prompt_enum_orders_come_from_the_manifest():
    assert _PHYSICAL_ACTIONS_ORDER == OPERATOR_SHELL_ORDER
    assert set(_TURN_ENDING_ORDER) == set(OPERATOR_SHELL_ORDER)
    assert set(OPERATOR_SHELL_ORDER) <= OPERATOR_PROMPT_TOOLSET


def test_operator_verbs_lower_onto_manifest_actions():
    """The Operator's internal decision verbs resolve to canonical manifest names."""
    unresolved = set(OPERATOR_ACTION_TO_CANONICAL.values()) - set(ACTION_SPECS)
    assert not unresolved, (
        f"action_names maps operator verbs onto names the manifest does not know:"
        f" {sorted(unresolved)}"
    )


def test_manifest_wait_for_delay_uses_milliseconds():
    """The manifest's ``wait_for_delay`` takes ``time_in_ms``, never *seconds*.

    ``artemis/tools/actions/device_actions.py`` used to shadow-register a second
    ``wait_for_delay`` taking *seconds* while every prompt teaches milliseconds.
    The shadow ToolRegistry channel is deleted; the manifest is the only surface
    left, and its unit must stay milliseconds.
    """
    fields = set(tool_declaration("wait_for_delay").parameters["properties"])
    assert "seconds" not in fields
    assert "time_in_ms" in fields
