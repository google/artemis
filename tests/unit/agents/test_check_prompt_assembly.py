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

"""§1.3 assembly discipline: prompts are functions of configuration x scenario.
Both check gates off => zero context pollution; a plan carrying check lines
gets the behavior explainer regardless of switches."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from artemis.agents.operator.prompts import (
    CheckItemsExplainerPromptComponent,
    FeedbackPromptComponent,
    PromptBuilder,
    TemplatePromptComponent,
)
from artemis.agents.planner.planner import build_planner_system_blocks
from artemis.context import ArtemisContext, ExecutionSetup
from artemis.graph.state import State

PLANNER_JSON = Path(__file__).resolve().parents[3] / "artemis/agents/planner/planner.json"
OPERATOR_JSON = Path(__file__).resolve().parents[3] / "artemis/agents/operator/operator.json"


def _planner_data():
    return json.loads(PLANNER_JSON.read_text(encoding="utf-8"))


def test_planner_blocks_without_checks_are_byte_identical_to_legacy():
    data = _planner_data()
    baseline = list(data["modes"]["initial_plan"]["system"])
    assert build_planner_system_blocks(data, "initial_plan", include_checks=False) == baseline
    # And the disabled assembly never references the check blocks
    rendered = "\n\n".join(
        data["blocks"][b]
        for b in build_planner_system_blocks(data, "initial_plan", include_checks=False)
    )
    assert "- verify:" not in rendered
    assert "- assert:" not in rendered


def test_planner_blocks_with_checks_mount_generation_and_audit():
    data = _planner_data()
    initial = build_planner_system_blocks(data, "initial_plan", include_checks=True)
    assert initial[-1] == "check_generation"
    validator = build_planner_system_blocks(data, "validator", include_checks=True)
    assert validator[-1] == "check_audit"

    generation = data["blocks"]["check_generation"]
    # Restraint, honesty about post-hoc auditing, and no silent ordering promises
    assert "restraint" in generation.lower() or "Do NOT attach" in generation
    assert "post-hoc" in generation.lower()
    assert "Never silently promise" in generation

    audit = data["blocks"]["check_audit"]
    assert "REJECT" in audit


def _mock_ctx(tmp_path=None, plan: str | None = None, **setup_kwargs):
    ctx = MagicMock(spec=ArtemisContext)
    ctx.execution_setup = ExecutionSetup(**setup_kwargs) if setup_kwargs is not None else None
    if plan is not None and tmp_path is not None:
        notes = tmp_path / "notes"
        notes.mkdir(parents=True, exist_ok=True)
        (notes / "task_plan.md").write_text(plan, encoding="utf-8")
        ctx.data_engine = MagicMock()
        ctx.data_engine.base_dir = tmp_path
    else:
        ctx.data_engine = None
    return ctx


def _state(**overrides):
    state = MagicMock(spec=State)
    state.operator_feedback = None
    for k, v in overrides.items():
        setattr(state, k, v)
    return state


@pytest.mark.asyncio
async def test_explainer_renders_iff_plan_has_check_lines(tmp_path):
    component = CheckItemsExplainerPromptComponent()

    # Plan without check lines -> nothing rendered
    builder = PromptBuilder()
    ctx = _mock_ctx(tmp_path, plan="- [ ] G\n")
    await component(builder, _state(), ctx)
    assert builder.human_parts == []

    # Plan with check lines -> explainer rendered
    builder2 = PromptBuilder()
    ctx2 = _mock_ctx(tmp_path, plan="- [ ] G\n  - verify: V\n")
    await component(builder2, _state(), ctx2)
    joined = "\n".join(p for p in builder2.human_parts if isinstance(p, str))
    assert "check lines" in joined
    assert "independent Checker" in joined
    assert "automatically restored" in joined


@pytest.mark.asyncio
async def test_explainer_is_content_driven_not_switch_driven(tmp_path):
    """A resumed old plan with check lines still gets the explanation even when
    every check switch is off."""
    component = CheckItemsExplainerPromptComponent()
    builder = PromptBuilder()
    ctx = _mock_ctx(tmp_path, plan="- [ ] G\n  - assert: A\n", disable_checker=True)
    await component(builder, _state(), ctx)
    joined = "\n".join(p for p in builder.human_parts if isinstance(p, str))
    assert "check lines" in joined


async def _render_main_template(**setup_kwargs) -> str:
    component = TemplatePromptComponent()
    builder = PromptBuilder()
    ctx = _mock_ctx(**setup_kwargs)
    ctx.actuator = None
    state = _state(initial_goal="G")
    prompts = json.loads(OPERATOR_JSON.read_text(encoding="utf-8"))
    await component(builder, state, ctx, prompts=prompts, plan_and_history="")
    return "".join(builder.system_parts) + (builder.human_footer or "")


@pytest.mark.asyncio
async def test_main_template_diagnosis_trigger_gated_on_verification():
    """The rejection/finding diagnosis trigger is a dead instruction unless a
    mechanism that can produce rejections or findings is active (§1.3)."""
    # Factory default: checks off (master alias) AND planner validation off.
    default = await _render_main_template(disable_checker=True, disable_planner_validation=True)
    assert "Verification Finding" not in default
    assert "Ambiguous Validation Rejection" not in default

    # Checks on -> the trigger is real and must be documented.
    with_checks = await _render_main_template(
        disable_checker=False, disable_planner_validation=True
    )
    assert "Ambiguous Validation Rejection or Verification Finding" in with_checks

    # Planner validation alone can also produce rejections.
    with_validation = await _render_main_template(
        disable_checker=True, disable_planner_validation=False
    )
    assert "Ambiguous Validation Rejection or Verification Finding" in with_validation


@pytest.mark.asyncio
async def test_check_feedback_component_appends_findings_only_when_present():
    component = FeedbackPromptComponent()

    builder = PromptBuilder()
    await component(builder, _state(operator_feedback=None), _mock_ctx())
    assert builder.human_parts == []

    builder2 = PromptBuilder()
    await component(
        builder2,
        _state(operator_feedback=["[verify failed] 'X': missing"]),
        _mock_ctx(),
    )
    joined = "\n".join(p for p in builder2.human_parts if isinstance(p, str))
    assert "Verification Findings" in joined
    assert "[verify failed] 'X': missing" in joined
