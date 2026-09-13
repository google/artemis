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

"""Consistency contract between the stdio ADB MCP server and the action manifest.

``artemis/mcp/adb_server.py`` deliberately keeps its historical *pixel-space*
contract for external MCP clients (absolute pixel coordinates, combined
``coordinates`` list parameters, ``KEYCODE_*`` vocabulary), so its FastMCP tools
are hand-written rather than generated from ``artemis.mcp.action_specs``. This
suite is the compensating contract:

* **Fixture pin** -- the full generated schema of every adb_server tool is
  frozen; any signature or docstring change must regenerate the fixture in the
  same commit (external clients see this schema directly).
* **Projection check** -- every device-action tool is mapped onto its canonical
  ``ActionSpec`` wire dialect. Parameters that mirror a wire parameter must
  agree on JSON type, requiredness, and default. Deliberate legacy divergences
  are declared in ``DECLARED_DIVERGENCES`` and are *ratcheted*: if the two
  surfaces converge, the stale declaration fails too, so the table always
  matches reality.

Together the two guards make silent drift between the second declaration
surface and the canonical manifest impossible in either direction.
"""

import json
from pathlib import Path

import pytest

from artemis.mcp.action_specs import ACTION_SPECS, ParamSpec

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "action_surfaces"

FIXTURE_HINT = (
    "The generated schema differs from the pinned fixture. If this change is"
    " intentional, regenerate the fixture in the same commit and explain the schema"
    " change in the commit message; external MCP clients see this schema directly."
)


# --- The declared projection ----------------------------------------------------------

#: adb_server tool name -> (canonical action name, {adb param -> wire param}).
#: Params absent from the mapping are legacy packaging with no one-to-one wire
#: counterpart (the combined ``coordinates`` lists); their shape is asserted
#: separately below and pinned by the fixture.
ADB_TOOL_PROJECTION: dict[str, tuple[str, dict[str, str]]] = {
    "tap": ("click", {"times": "times", "delay_ms": "delay_ms"}),
    "long_press_on": ("long_press", {"duration": "duration_ms"}),
    "swipe": ("swipe", {"duration": "duration_ms"}),
    "focus_and_input_text": ("input_text", {"text": "text", "clear_before_input": "clear_exist"}),
    "press_key": ("press_key", {"keycode": "key"}),
    "open_link": ("open_link", {"url": "url"}),
    "erase_one_char": ("erase_one_char", {}),
    "focus_and_clear_text": ("focus_and_clear_text", {}),
}

#: Combined-coordinates legacy packaging: adb tool -> (marker in docstring, arity).
#: ``coordinates`` packs the wire dialect's coordinate pair(s) into one list.
COORDINATE_PACKING: dict[str, tuple[str, int]] = {
    "tap": ("[x, y]", 2),
    "long_press_on": ("[x, y]", 2),
    "swipe": ("[start_x, start_y, end_x, end_y]", 4),
    "focus_and_input_text": ("[x, y]", 2),
    "focus_and_clear_text": ("[x, y]", 2),
}

#: Deliberate legacy divergences, (adb tool, adb param) -> reason. Each entry is
#: ratcheted: the test fails if the surfaces actually agree, so this table can
#: never go stale.
DECLARED_DIVERGENCES: dict[tuple[str, str], str] = {
    ("swipe", "duration"): (
        "legacy stdio default is 400ms; the canonical wire duration_ms defaults"
        " to 800ms (adaptive-duration smart swipes only exist in agent dialects)"
    ),
    ("focus_and_input_text", "clear_before_input"): (
        "legacy stdio appends by default (False); the canonical wire clear_exist"
        " defaults to True (replace)"
    ),
}

_JSON_TYPE: dict[type, str] = {int: "integer", str: "string", bool: "boolean"}


def _wire_param(action: str, name: str) -> ParamSpec:
    spec = ACTION_SPECS[action]
    assert spec.wire is not None, f"'{action}' has no wire dialect."
    for p in spec.wire.params:
        if p.name == name:
            return p
    raise AssertionError(f"wire dialect of '{action}' has no param '{name}'")


async def _adb_server_tools() -> dict:
    from artemis.mcp import adb_server

    tools = await adb_server.mcp.list_tools()
    return {t.name: t for t in tools}


def _prop(tool, name: str) -> dict:
    props = tool.inputSchema.get("properties", {})
    assert name in props, f"tool '{tool.name}' lost parameter '{name}'"
    return props[name]


# --- Fixture pin -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adb_server_manifest_matches_fixture():
    expected = json.loads((FIXTURES / "adb_server_manifest.json").read_text(encoding="utf-8"))
    tools = await _adb_server_tools()
    generated = {
        name: {"description": t.description, "inputSchema": t.inputSchema}
        for name, t in tools.items()
    }
    assert set(generated) == set(expected), FIXTURE_HINT
    for name in expected:
        assert generated[name] == expected[name], f"adb_server tool '{name}': {FIXTURE_HINT}"


# --- Projection consistency -------------------------------------------------------------


@pytest.mark.asyncio
async def test_projection_covers_all_wire_backed_adb_tools():
    """Every adb_server tool that mirrors a canonical action is in the projection."""
    tools = await _adb_server_tools()
    missing = set(ADB_TOOL_PROJECTION) - set(tools)
    assert not missing, f"projection references adb_server tools that no longer exist: {missing}"
    for adb_name, (canonical, _) in ADB_TOOL_PROJECTION.items():
        assert canonical in ACTION_SPECS, (
            f"adb_server tool '{adb_name}' maps to '{canonical}', which the canonical"
            " manifest does not know."
        )
        assert ACTION_SPECS[canonical].wire is not None, (
            f"adb_server tool '{adb_name}' maps to '{canonical}', which has no wire dialect."
        )


@pytest.mark.asyncio
async def test_mirrored_params_agree_with_wire_dialect():
    """Type/requiredness/default of every mirrored parameter matches the wire spec,
    except where a divergence is declared -- and declared divergences must be real."""
    tools = await _adb_server_tools()
    for adb_name, (canonical, param_map) in ADB_TOOL_PROJECTION.items():
        tool = tools[adb_name]
        required = set(tool.inputSchema.get("required", []))
        for adb_param, wire_name in param_map.items():
            wire = _wire_param(canonical, wire_name)
            prop = _prop(tool, adb_param)
            expected_type = _JSON_TYPE.get(wire.annotation)
            if expected_type is not None:
                assert prop.get("type") == expected_type, (
                    f"'{adb_name}.{adb_param}': schema type {prop.get('type')!r} !="
                    f" canonical wire '{canonical}.{wire_name}' type {expected_type!r}"
                )
            adb_required = adb_param in required
            adb_default = prop.get("default")
            diverged_key = (adb_name, adb_param)
            agrees = adb_required == wire.required and (
                wire.required or adb_default == wire.default
            )
            if diverged_key in DECLARED_DIVERGENCES:
                assert not agrees, (
                    f"'{adb_name}.{adb_param}' is declared divergent from"
                    f" '{canonical}.{wire_name}' but the surfaces now agree -- remove"
                    f" the stale entry from DECLARED_DIVERGENCES:"
                    f" {DECLARED_DIVERGENCES[diverged_key]}"
                )
            else:
                assert agrees, (
                    f"'{adb_name}.{adb_param}' drifted from canonical wire"
                    f" '{canonical}.{wire_name}': required {adb_required} vs"
                    f" {wire.required}, default {adb_default!r} vs {wire.default!r}."
                    " Either re-align, or declare the divergence in"
                    " DECLARED_DIVERGENCES with a reason."
                )


@pytest.mark.asyncio
async def test_coordinate_packing_shape_is_documented():
    """The combined `coordinates` legacy parameter keeps its documented arity."""
    tools = await _adb_server_tools()
    for adb_name, (marker, _arity) in COORDINATE_PACKING.items():
        tool = tools[adb_name]
        prop = _prop(tool, "coordinates")
        assert prop.get("type") == "array", f"'{adb_name}.coordinates' must be an array"
        assert prop.get("items", {}).get("type") == "integer", (
            f"'{adb_name}.coordinates' must be a list of integers"
        )
        assert "coordinates" in set(tool.inputSchema.get("required", [])), (
            f"'{adb_name}.coordinates' must stay required"
        )
        assert marker in (tool.description or ""), (
            f"'{adb_name}' docstring no longer documents the coordinate shape"
            f" '{marker}'; external clients rely on this wording."
        )


@pytest.mark.asyncio
async def test_press_key_vocabulary_divergence_is_documented():
    """adb_server's press_key takes raw KEYCODE_* strings; the canonical wire takes
    bare lowercase words. The canonical spec documents that divergence in prose."""
    tools = await _adb_server_tools()
    desc = tools["press_key"].description or ""
    assert "KEYCODE_" in desc, (
        "adb_server press_key docstring must keep teaching KEYCODE_* vocabulary"
    )
    assert "key" in ACTION_SPECS["press_key"].differences, (
        "the canonical manifest no longer documents the press_key vocabulary split"
    )
