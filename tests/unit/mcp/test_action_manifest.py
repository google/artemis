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

"""Prompt <-> manifest consistency and actuator contract tests.

The scan below re-derives the manifest classification from the prompt sources, so
adding a tool name to a prompt without classifying it in
``artemis/mcp/action_manifest.py`` fails here rather than at runtime.
"""

import json
from pathlib import Path
import re

import pytest

from artemis.core.tool_declaration import ToolDeclaration
from artemis.mcp.action_manifest import (
    ActuatorContractError,
    AGENT_ROLES,
    ALL_KNOWN_TOOLS,
    BACKEND_INDEPENDENT_TOOLS,
    DEVICE_ACTIONS,
    ExtensionTool,
    INTERNAL_ACTIONS,
    OPTIONAL_ACTIONS,
    REQUIRED_ACTIONS,
    available_device_actions,
    filter_declarations,
    validate_actuator,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

# Prompt sources belonging to the three agents this manifest governs (plus the
# Validator's safety net). Other agents (log_analyzer, image_processor, ...) have their
# own tool universes and are deliberately out of scope.
SCOPED_PROMPT_FILES = [
    "artemis/agents/flash/flash_runner.md",
    "artemis/agents/validator/failure_analyzer.md",
    "artemis/agents/validator/target_disappeared_analyzer.md",
    "artemis/agents/validator/pixel_target_disappeared_analyzer.md",
    "artemis/agents/validator/pixel_safety_net.md",
]

# Backtick-quoted identifiers in scoped prompts that are not tools: parameter names and
# note keys. Anything new landing here should be a conscious decision.
NON_TOOL_IDENTIFIERS = frozenset({"analysis", "task_plan"})

_TOOL_REF = re.compile(r"(?:`([a-z][a-z0-9_]{2,})`|\b([a-z][a-z0-9_]{2,})\()")


def _scoped_prompt_sources() -> dict[str, str]:
    """Collects all in-scope prompt texts, keyed by a short source label."""
    sources: dict[str, str] = {}
    for rel in SCOPED_PROMPT_FILES:
        path = REPO_ROOT / rel
        sources[path.name] = path.read_text(encoding="utf-8")

    operator_json = REPO_ROOT / "artemis/agents/operator/operator.json"
    for key, text in json.loads(operator_json.read_text(encoding="utf-8")).items():
        sources[f"operator.json:{key}"] = text

    # Only the prompt string constants from prompts.py -- scanning the module as text
    # would pick up Python code (`len(`, `getattr(` ...).
    from artemis.agents.operator import prompts as operator_prompts

    for name in dir(operator_prompts):
        value = getattr(operator_prompts, name)
        if name.startswith("_") and isinstance(value, str) and len(value) > 80:
            sources[f"prompts.py:{name}"] = value

    return sources


def _referenced_tools() -> dict[str, set[str]]:
    """Returns {identifier: source labels} for every tool-shaped reference in scope."""
    found: dict[str, set[str]] = {}
    for label, text in _scoped_prompt_sources().items():
        for match in _TOOL_REF.finditer(text):
            name = match.group(1) or match.group(2)
            found.setdefault(name, set()).add(label)
    return found


# --- Prompt <-> manifest invariants --------------------------------------------------


def test_scan_finds_prompt_sources():
    """The scan must cover all expected sources; silent scope loss would void the suite."""
    sources = _scoped_prompt_sources()
    assert "flash_runner.md" in sources
    assert "operator.json:main_template" in sources
    assert "operator.json:troubleshooter_template" in sources
    assert any(label.startswith("prompts.py:") for label in sources)


def test_every_prompt_tool_reference_is_classified():
    """Every tool referenced by an in-scope prompt must be known to the manifest."""
    unclassified = {
        name: sorted(labels)
        for name, labels in _referenced_tools().items()
        if name not in ALL_KNOWN_TOOLS and name not in NON_TOOL_IDENTIFIERS
    }
    assert not unclassified, (
        "Prompts reference tools the manifest does not classify. Add each to the "
        f"appropriate set in artemis/mcp/action_manifest.py: {unclassified}"
    )


def test_required_actions_are_structurally_referenced():
    """A REQUIRED action must actually appear in a prompt; otherwise demote it."""
    referenced = set(_referenced_tools())
    unreferenced = REQUIRED_ACTIONS - referenced
    assert not unreferenced, (
        f"REQUIRED_ACTIONS not referenced by any prompt: {sorted(unreferenced)}. "
        "Required status exists only to protect structural prompt dependencies."
    )


def test_internal_actions_never_appear_in_prompts():
    """INTERNAL actions are adapter plumbing and must stay invisible to every model."""
    leaked = INTERNAL_ACTIONS & set(_referenced_tools())
    assert not leaked, f"Internal actions leaked into prompts: {sorted(leaked)}"


def test_manifest_sets_are_disjoint():
    sets = {
        "REQUIRED_ACTIONS": REQUIRED_ACTIONS,
        "OPTIONAL_ACTIONS": OPTIONAL_ACTIONS,
        "INTERNAL_ACTIONS": INTERNAL_ACTIONS,
        "BACKEND_INDEPENDENT_TOOLS": BACKEND_INDEPENDENT_TOOLS,
    }
    names = list(sets)
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            overlap = sets[a] & sets[b]
            assert not overlap, f"{a} and {b} overlap: {sorted(overlap)}"


# --- Actuator contract ---------------------------------------------------------------


class _StubActuator:
    """Minimal actuator test double with configurable capabilities and extensions."""

    def __init__(self, caps=None, exts=None):
        self._caps = frozenset(caps if caps is not None else DEVICE_ACTIONS)
        self._exts = list(exts or [])

    def capabilities(self):
        return self._caps

    def extensions(self):
        return self._exts


def _extension(**overrides) -> ExtensionTool:
    async def _noop(**_kwargs):
        return "ok"

    defaults = dict(
        name="calibrate_arm",
        description=(
            "Runs the robot arm's touch calibration sweep. Use before the first "
            "physical action of a session, or after any missed tap."
        ),
        input_schema={"type": "object", "properties": {}},
        handler=_noop,
        targets=frozenset({"operator", "flash"}),
    )
    defaults.update(overrides)
    return ExtensionTool(**defaults)


def test_full_actuator_passes_validation():
    validate_actuator(_StubActuator())


def test_minimal_actuator_passes_validation():
    """REQUIRED + INTERNAL alone is a legal backend; every optional action may be absent."""
    validate_actuator(_StubActuator(caps=REQUIRED_ACTIONS | INTERNAL_ACTIONS))


def test_missing_required_action_fails_fast():
    caps = (DEVICE_ACTIONS - {"click_sequence"})
    with pytest.raises(ActuatorContractError, match="click_sequence"):
        validate_actuator(_StubActuator(caps=caps))


def test_missing_internal_action_fails_fast():
    caps = DEVICE_ACTIONS - {"observe_screen"}
    with pytest.raises(ActuatorContractError, match="observe_screen"):
        validate_actuator(_StubActuator(caps=caps))


def test_unknown_capability_is_rejected():
    """Backend-specific tools must go through extensions(), not capabilities()."""
    with pytest.raises(ActuatorContractError, match="calibrate_arm"):
        validate_actuator(_StubActuator(caps=DEVICE_ACTIONS | {"calibrate_arm"}))


def test_extension_name_collision_fails_fast():
    with pytest.raises(ActuatorContractError, match="collides"):
        validate_actuator(_StubActuator(exts=[_extension(name="click")]))


def test_duplicate_extension_names_fail_fast():
    with pytest.raises(ActuatorContractError, match="more than once"):
        validate_actuator(_StubActuator(exts=[_extension(), _extension()]))


def test_extension_with_thin_description_is_rejected():
    """Extensions get no prompt-level teaching; the description carries the contract."""
    with pytest.raises(ActuatorContractError, match="description"):
        validate_actuator(_StubActuator(exts=[_extension(description="Calibrates.")]))


def test_extension_with_unknown_target_is_rejected():
    with pytest.raises(ActuatorContractError, match="unknown agent role"):
        validate_actuator(_StubActuator(exts=[_extension(targets=frozenset({"planner"}))]))


# --- Surface derivation --------------------------------------------------------------


def _declarations(*names: str) -> list[ToolDeclaration]:
    return [
        ToolDeclaration(name=n, description=f"{n} tool", parameters={"type": "object"})
        for n in names
    ]


def test_available_device_actions_excludes_internal():
    actions = available_device_actions(_StubActuator())
    assert actions == REQUIRED_ACTIONS | OPTIONAL_ACTIONS
    assert not actions & INTERNAL_ACTIONS


def test_filter_drops_unimplemented_optional_actions():
    actuator = _StubActuator(caps=DEVICE_ACTIONS - {"swipe", "manage_app"})
    decls = _declarations("click", "swipe", "manage_app", "read_note")
    kept = [d.name for d in filter_declarations(decls, actuator, agent="flash")]
    assert kept == ["click", "read_note"]


def test_filter_passes_through_unclassified_agent_tools():
    """Agent-specific tools the manifest does not know must never be gated."""
    actuator = _StubActuator(caps=REQUIRED_ACTIONS | INTERNAL_ACTIONS)
    decls = _declarations("click_sequence", "reply_to_checker", "some_bespoke_tool")
    kept = [d.name for d in filter_declarations(decls, actuator, agent="operator")]
    assert kept == ["click_sequence", "reply_to_checker", "some_bespoke_tool"]


def test_filter_appends_extensions_for_targeted_agents_only():
    actuator = _StubActuator(exts=[_extension(targets=frozenset({"flash"}))])
    decls = _declarations("click")

    flash = [d.name for d in filter_declarations(decls, actuator, agent="flash")]
    operator = [d.name for d in filter_declarations(decls, actuator, agent="operator")]

    assert flash == ["click", "calibrate_arm"]
    assert operator == ["click"]


def test_filter_extension_declaration_shape():
    """Extension declarations must be indistinguishable in shape from known ones."""
    actuator = _StubActuator(exts=[_extension()])
    decls = filter_declarations([], actuator, agent="operator")
    assert len(decls) == 1
    ext_decl = decls[0]
    assert isinstance(ext_decl, ToolDeclaration)
    assert ext_decl.name == "calibrate_arm"
    assert ext_decl.parameters == {"type": "object", "properties": {}}


def test_filter_rejects_unknown_agent_role():
    with pytest.raises(ValueError, match="Unknown agent role"):
        filter_declarations([], _StubActuator(), agent="planner")


def test_all_agent_roles_are_covered():
    assert AGENT_ROLES == {"operator", "flash", "failure_analyzer", "validator"}
