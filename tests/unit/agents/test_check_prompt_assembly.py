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
Both check gates off => zero context pollution; with a gate on, the check-line
guidance lives once in the static system prompt, worded for that gate."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from artemis.agents.operator.prompts import (
    FeedbackPromptComponent,
    PromptBuilder,
    TemplatePromptComponent,
    render_transcript_static_system,
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
    # Advisory review: the audit flags weakened check standards, never blocks.
    assert "FLAG" in audit
    assert "REJECT" not in audit


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
async def test_check_line_guidance_lives_in_the_static_prompt_once():
    """The check-line guidance is a static-prompt section gated on the check
    gates (never a per-turn observation block), worded for the active gate."""
    off = await _render_main_template(disable_checker=True)
    assert "*Check Lines*" not in off
    assert "never declare a check passed" not in off

    final_only = await _render_main_template(disable_midway_checks=True, disable_final_check=False)
    assert final_only.count("*Check Lines*") == 1
    assert "never declare a check passed" in final_only
    assert "restores deleted ones unless user guidance called for the change" in final_only
    # No midway repair loop -> no repair budget to recite, and the grammar says so.
    assert "reopens its milestone at most" not in final_only
    assert "there is no midway repair loop" in final_only

    midway = await _render_main_template(disable_midway_checks=False, checkpoint_max_repairs=3)
    assert midway.count("*Check Lines*") == 1
    assert "reopens its milestone at most 3 times" in midway
    assert "- finding:" in midway


def test_transcript_static_system_recites_the_repair_budget_from_setup():
    ctx = _mock_ctx(disable_midway_checks=False, checkpoint_max_repairs=5)
    ctx.actuator = None
    prompts = json.loads(OPERATOR_JSON.read_text(encoding="utf-8"))
    text = render_transcript_static_system(prompts, ctx, _state(initial_goal="G"))
    assert text.count("*Check Lines*") == 1
    assert "reopens its milestone at most 5 times" in text

    quiet = _mock_ctx(disable_checker=True)
    quiet.actuator = None
    assert "*Check Lines*" not in render_transcript_static_system(
        prompts, quiet, _state(initial_goal="G")
    )


@pytest.mark.asyncio
async def test_static_prompt_declares_the_user_guidance_channel_once():
    text = await _render_main_template(disable_checker=True)
    assert text.count("--- User Guidance ---") == 1
    assert "outranks the task plan and its check lines" in text


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
    assert "Ambiguous Verification Finding" in with_checks

    # Planner validation alone can also produce (advisory) findings.
    with_validation = await _render_main_template(
        disable_checker=True, disable_planner_validation=False
    )
    assert "Ambiguous Verification Finding" in with_validation


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
