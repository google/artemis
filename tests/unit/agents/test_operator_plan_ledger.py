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

"""Tests for operator plan-ledger handling."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from artemis.agents.operator.operator import OperatorNode
from artemis.agents.operator.prompts import (
    apply_operator_prompt_contract,
    load_operator_prompts,
    render_plan_ledger_bounce,
    unwritten_action_streak,
)
from artemis.config.agent import MemoryTranscriptConfig
from artemis.context import ArtemisContext
from artemis.utils.plan_grammar import parse_plan, render_plan_grammar_spec

LEGACY_TRANSCRIPT = MemoryTranscriptConfig(enabled=False)

PLAN_NO_LEAF = """- [x] Open Google Maps and search for SFO
- [/] Read the commute duration and record it into note `commute_eta_info`
  - verify: duration recorded
- [ ] Draft the ETA message
"""

PLAN_WITH_LEAF = """- [x] Open Google Maps and search for SFO
- [/] Read the commute duration and record it into note `commute_eta_info`
  - [x] Open route options for SFO
  - [/] Read the driving duration
    - [/] Tap the "Driving" tab so the fastest route is highlighted
  - verify: duration recorded
- [ ] Draft the ETA message
"""


def test_grammar_spec_teaches_deeper_nesting():
    spec = render_plan_grammar_spec(include_checks=False)
    assert "4 spaces for a sub-sub-goal" in spec
    assert "Only zero-indent lines count as milestones" in spec


def test_active_milestone_and_leaf():
    snap = parse_plan(PLAN_WITH_LEAF)
    milestone = snap.active_milestone()
    assert milestone is not None and milestone.text.startswith("Read the commute duration")
    children = snap.children_of(milestone)
    assert [c.text for c in children] == [
        "Open route options for SFO",
        "Read the driving duration",
        'Tap the "Driving" tab so the fastest route is highlighted',
    ]
    leaf = snap.active_leaf(milestone)
    assert leaf is not None and leaf.text.startswith('Tap the "Driving" tab')
    assert leaf.indent == 4

    snap = parse_plan(PLAN_NO_LEAF)
    milestone = snap.active_milestone()
    assert milestone is not None
    assert snap.active_leaf(milestone) is None
    assert snap.children_of(milestone) == ()


def test_active_milestone_fallbacks():
    all_pending = parse_plan("- [ ] First\n- [ ] Second\n")
    assert all_pending.active_milestone().text == "First"
    nested_only = parse_plan("- [ ] First\n  - [/] Doing it\n- [ ] Second\n")
    assert nested_only.active_milestone().text == "First"
    assert parse_plan("- [x] First\n- [x] Second\n").active_milestone() is None


def _action_step(n, wrote_plan=False, action=True):
    tool_calls = []
    if wrote_plan:
        tool_calls.append(
            {"name": "update_note", "args": {"key": "task_plan", "target": "a", "replacement": "b"}}
        )
    step = {"step_id": f"s{n}", "step_number": n, "tool_calls": tool_calls}
    if action:
        step["action_taken"] = [{"action": "tap"}]
    return step


def test_streak_counts_trailing_action_turns_without_plan_write():
    assert unwritten_action_streak([]) == 0
    assert unwritten_action_streak([_action_step(1, wrote_plan=True)]) == 0
    assert unwritten_action_streak([_action_step(1)]) == 1
    steps = [
        _action_step(1),
        _action_step(2, wrote_plan=True),
        _action_step(3),
        _action_step(4, action=False),
        _action_step(5),
    ]
    assert unwritten_action_streak(steps) == 2


def test_bounce_only_when_ledger_is_broken_or_stale():
    assert render_plan_ledger_bounce(PLAN_WITH_LEAF, streak=0, stale_turns=4) is None
    assert render_plan_ledger_bounce(PLAN_WITH_LEAF, streak=2, stale_turns=4) is None
    stale = render_plan_ledger_bounce(PLAN_WITH_LEAF, streak=3, stale_turns=4)
    assert stale and "stayed in progress for 4 action turns" in stale
    assert 'Tap the "Driving" tab' in stale
    assert render_plan_ledger_bounce(PLAN_WITH_LEAF, streak=9, stale_turns=0) is None

    broken = render_plan_ledger_bounce(PLAN_NO_LEAF, streak=0, stale_turns=4)
    assert broken and "has no in-progress `[/]` sub-goal beneath it" in broken
    assert "Read the commute duration" in broken

    assert render_plan_ledger_bounce("No task plan yet.", 0, 4) is None
    assert render_plan_ledger_bounce("- [x] Done\n- [x] Also done\n", 0, 4) is None


def test_prompt_teaches_the_ledger_once_without_per_turn_reminder():
    prompt = apply_operator_prompt_contract(load_operator_prompts()["main_template"])
    assert prompt.count("**Live Sub-goal Ledger**") == 1
    assert "mandatory every turn" not in prompt
    assert "need no plan write" in prompt
    assert "sub-sub-goal (4 spaces)" in prompt
    assert "structurally broken" in prompt


def _make_node(tmp_path, plan_text, steps=None):
    notes = tmp_path / "notes"
    notes.mkdir(parents=True)
    (notes / "task_plan.md").write_text(plan_text, encoding="utf-8")

    ctx = MagicMock(spec=ArtemisContext)
    ctx.execution_setup = None
    ctx.data_engine = MagicMock()
    ctx.data_engine.base_dir = str(tmp_path)
    ctx.data_engine.current_session_id = "s"
    ctx.data_engine.get_agent_friendly_steps.return_value = steps or []

    state = MagicMock()
    state.subagent_calls = []
    state.initial_goal = "goal"
    state.open_incident = None
    node = OperatorNode(ctx, transcript_config=LEGACY_TRANSCRIPT)
    return node, state, notes / "task_plan.md"


def _click_response(n_actions=1):
    response = MagicMock()
    response.content = ""
    response.tool_calls = [
        {"name": "click", "args": {"target": [50, 50]}, "id": f"call_{i}"} for i in range(n_actions)
    ]
    return response


def _llm(side_effect=None, return_value=None):
    llm = MagicMock()
    llm.ainvoke = AsyncMock(side_effect=side_effect, return_value=return_value)
    llm.bind_tools.return_value = llm
    return llm


@pytest.mark.asyncio
async def test_gate_bounces_broken_ledger_once_then_executes(tmp_path):
    node, state, _ = _make_node(tmp_path, PLAN_NO_LEAF)
    seen = []

    async def ainvoke(messages, *args, **kwargs):
        seen.append(list(messages))
        return _click_response()

    with patch("artemis.agents.operator.operator.get_llm", return_value=_llm(ainvoke)):
        update = await node(state)

    assert len(seen) == 2, "first submission bounced, second executed"
    bounce = [
        m
        for m in seen[1]
        if isinstance(getattr(m, "content", None), str)
        and m.content.startswith("Not executed: the active milestone")
    ]
    assert bounce and bounce[0].status == "error"
    assert json.loads(update["structured_decisions"])[0]["action"] == "tap"


@pytest.mark.asyncio
async def test_gate_lets_healthy_ledger_through_without_a_write(tmp_path):
    node, state, _ = _make_node(tmp_path, PLAN_WITH_LEAF, steps=[_action_step(1)])
    llm = _llm(return_value=_click_response())
    with patch("artemis.agents.operator.operator.get_llm", return_value=llm):
        update = await node(state)
    assert llm.ainvoke.await_count == 1
    assert update["structured_decisions"] is not None


@pytest.mark.asyncio
async def test_gate_bounces_stale_ledger(tmp_path):
    stale_history = [_action_step(1), _action_step(2), _action_step(3)]
    node, state, _ = _make_node(tmp_path, PLAN_WITH_LEAF, steps=stale_history)
    seen = []

    async def ainvoke(messages, *args, **kwargs):
        seen.append(list(messages))
        return _click_response()

    with (
        patch("artemis.agents.operator.operator.get_llm", return_value=_llm(ainvoke)),
        patch.object(OperatorNode, "_plan_ledger_stale_turns", return_value=4),
    ):
        await node(state)
    assert len(seen) == 2
    assert any(
        isinstance(getattr(m, "content", None), str)
        and "stayed in progress for 4 action turns" in m.content
        for m in seen[1]
    )


@pytest.mark.asyncio
async def test_gate_passes_when_plan_changed_in_turn(tmp_path):
    node, state, plan_path = _make_node(tmp_path, PLAN_NO_LEAF)

    async def ainvoke(messages, *args, **kwargs):
        plan_path.write_text(PLAN_WITH_LEAF, encoding="utf-8")
        return _click_response()

    llm = _llm(ainvoke)
    with patch("artemis.agents.operator.operator.get_llm", return_value=llm):
        update = await node(state)
    assert llm.ainvoke.await_count == 1
    assert update["structured_decisions"] is not None


@pytest.mark.asyncio
async def test_gate_exempts_bursts_and_open_incidents(tmp_path):
    node, state, _ = _make_node(tmp_path / "burst", PLAN_NO_LEAF)
    llm = _llm(return_value=_click_response(n_actions=2))
    with patch("artemis.agents.operator.operator.get_llm", return_value=llm):
        update = await node(state)
    assert llm.ainvoke.await_count == 1
    assert len(json.loads(update["structured_decisions"])) == 2

    node, state, _ = _make_node(tmp_path / "incident", PLAN_NO_LEAF)
    state.open_incident = {"category": "intercepted"}
    llm = _llm(return_value=_click_response())
    with patch("artemis.agents.operator.operator.get_llm", return_value=llm):
        await node(state)
    assert llm.ainvoke.await_count == 1


@pytest.mark.asyncio
async def test_gate_inactive_without_plan_or_when_disabled(tmp_path):
    llm = _llm(return_value=_click_response())

    node, state, _ = _make_node(tmp_path / "a", "No task plan yet.")
    with patch("artemis.agents.operator.operator.get_llm", return_value=llm):
        await node(state)
    assert llm.ainvoke.await_count == 1

    llm.ainvoke.reset_mock()
    node, state, _ = _make_node(tmp_path / "b", PLAN_NO_LEAF)
    with (
        patch("artemis.agents.operator.operator.get_llm", return_value=llm),
        patch.object(OperatorNode, "_plan_ledger_gate_enabled", return_value=False),
    ):
        await node(state)
    assert llm.ainvoke.await_count == 1
