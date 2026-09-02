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
  shells, Validator/Flash declarations, the action server manifest). A schema change
  must be a conscious act: regenerate the fixture in the same commit and say why.
* **Bridge derivation** re-derives every cross-dialect parameter difference from the
  generated schemas and requires it to be declared in ``ActionSpec.param_bridge``.
  Undeclared divergence between what one agent sees and what another sees -- the
  historical definition of drift -- fails here instead of shipping.
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
        "report_failure_analysis",
    ]


# --- Bridge derivation: dialect differences must be declared -------------------------


def _operator_param_names(name: str) -> list[str]:
    schema = convert_to_openai_tool(operator_shell_tool(name))
    return list(schema["function"]["parameters"].get("properties", {}))


def _declaration_param_names(name: str) -> list[str]:
    return list(tool_declaration(name).parameters.get("properties", {}))


def test_param_bridge_is_exact():
    """Every operator<->declaration parameter difference is declared, none invented."""
    for spec in ACTION_SPECS.values():
        if spec.operator is None or spec.declaration is None:
            assert spec.param_bridge == {}, (
                f"'{spec.name}' declares a param_bridge without both dialects."
            )
            continue
        operator_names = _operator_param_names(spec.name)
        declaration_names = _declaration_param_names(spec.name)
        assert set(spec.param_bridge) == set(operator_names), (
            f"'{spec.name}': param_bridge keys must cover exactly the operator"
            f" params. bridge={sorted(spec.param_bridge)} operator={operator_names}"
        )
        assert set(spec.param_bridge.values()) == set(declaration_names), (
            f"'{spec.name}': param_bridge values must cover exactly the declaration"
            f" params. bridge={sorted(spec.param_bridge.values())}"
            f" declaration={declaration_names}"
        )


def test_renamed_params_are_documented():
    """A cross-dialect rename is a deliberate difference; it must carry prose."""
    for spec in ACTION_SPECS.values():
        renames = {k: v for k, v in spec.param_bridge.items() if k != v}
        if renames:
            assert spec.differences, (
                f"'{spec.name}' renames {renames} across dialects but documents no differences."
            )


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


def test_registry_wait_for_delay_seconds_drift_stays_dead():
    """Guards against re-growing a shadow device-action registration.

    ``artemis/tools/actions/device_actions.py`` used to shadow-register a second
    ``wait_for_delay`` taking *seconds* while every prompt teaches milliseconds
    (the last such surface, ``artemis/tools/mobile/exec_tools.py``, is deleted).
    The ToolRegistry must never again carry a ``wait_for_delay`` whose unit
    disagrees with the manifest's ``time_in_ms``.
    """
    from artemis.tools.base import ToolRegistry

    tool = ToolRegistry.get("wait_for_delay")
    if tool is None:
        return  # Not registered at all: nothing to drift.
    fields = set(tool.args_schema.model_json_schema().get("properties", {}))
    assert "seconds" not in fields
    assert "time_in_ms" in fields
