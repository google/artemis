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

"""A model's successful HTTP response must not permit unlimited empty turns."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from langchain_core.messages import AIMessage, HumanMessage
from pydantic import ValidationError
import pytest

from artemis.agents.operator.operator import OperatorNode
from artemis.agents.operator.progress_guard import (
    OperatorNoActionLimitError,
    OperatorProgressGuard,
    configured_progress_guard,
)
from artemis.config.agent import AgentGlobalConfig, ExecutionConfig, MemoryTranscriptConfig
from artemis.context import ArtemisContext
from artemis.graph.state import State


@pytest.mark.parametrize("actions", [None, []])
def test_consecutive_no_decisions_are_bounded_across_turns(actions):
    guard = OperatorProgressGuard(max_no_action_turns=4)
    for _ in range(3):
        guard.record_turn(actions, "- [/] Observe the page")
    with pytest.raises(OperatorNoActionLimitError, match="operator_no_action_limit"):
        guard.record_turn(actions, "- [/] Observe the page")
    assert guard.consecutive_no_action_turns == 4


def test_observation_then_action_resets_the_streak():
    guard = OperatorProgressGuard(max_no_action_turns=3)
    guard.record_turn(None, "- [/] Inspect the page")
    guard.record_turn([], "- [/] Inspect the page")
    guard.record_turn([{"action": "wait", "seconds": 1}], "- [/] Inspect the page")
    assert guard.consecutive_no_action_turns == 0
    guard.record_turn(None, "- [/] Inspect the page")
    assert guard.consecutive_no_action_turns == 1


def test_notes_and_unchanged_task_do_not_reset_empty_turn_budget():
    guard = OperatorProgressGuard(max_no_action_turns=2)
    guard.record_turn(None, "- [/] Inspect the page\n  - [ ] Read another note")
    with pytest.raises(OperatorNoActionLimitError):
        guard.record_turn(None, "- [/] Inspect the page\n  - [ ] Read a different note")


@pytest.mark.parametrize("plan", [None, "", "- [!] Unable to proceed"])
def test_missing_or_blocked_plan_is_not_success(plan):
    with pytest.raises(OperatorNoActionLimitError):
        OperatorProgressGuard(max_no_action_turns=1).record_turn(None, plan)


def test_completed_plan_can_reach_existing_graph_settlement():
    guard = OperatorProgressGuard(max_no_action_turns=2)
    guard.record_turn(None, "- [/] Observe the page")
    guard.record_turn(None, "- [x] Observe the page")
    assert guard.consecutive_no_action_turns == 0


def test_continuous_plan_requires_user_stop_to_bypass_guard():
    plan = "- [x] [Loop:continuous] Watch the page"
    with pytest.raises(OperatorNoActionLimitError):
        OperatorProgressGuard(max_no_action_turns=1).record_turn(None, plan)
    OperatorProgressGuard(max_no_action_turns=1).record_turn(None, plan, user_stopped=True)


def test_pro_execution_setting_configures_the_guard(monkeypatch):
    config = AgentGlobalConfig.model_validate({"pro": {"execution": {"max_no_action_turns": 3}}})
    assert config.pro.execution.max_no_action_turns == 3
    monkeypatch.setattr("artemis.config.load_agent_config", lambda: config)
    assert configured_progress_guard().max_no_action_turns == 3


def test_default_no_action_limit_is_four(monkeypatch):
    config = AgentGlobalConfig()
    assert config.pro.execution.max_no_action_turns == 4
    monkeypatch.setattr("artemis.config.load_agent_config", lambda: config)
    assert configured_progress_guard().max_no_action_turns == 4


@pytest.mark.parametrize("value", [0, -1, 21])
def test_budget_cannot_be_disabled_or_grow_unbounded(value):
    with pytest.raises(ValidationError):
        ExecutionConfig(max_no_action_turns=value)


@pytest.fixture
def fake_operator(monkeypatch):
    ctx = MagicMock(spec=ArtemisContext)
    ctx.execution_setup = None
    ctx.data_engine = None
    ctx.trace_id = "unit-progress-guard"
    ctx.actuator = None
    ctx.transcript_ledger = None
    monkeypatch.setattr("artemis.config.load_agent_config", lambda: AgentGlobalConfig())
    node = OperatorNode(ctx, transcript_config=MemoryTranscriptConfig(enabled=False))
    node._get_history_and_plan = lambda: ([], "- [/] Inspect the page")
    node._read_task_plan_text = lambda: "- [/] Inspect the page"
    node._build_prompt = AsyncMock(side_effect=lambda *a, **k: [HumanMessage(content="Observe.")])
    node._stage_transcript_turn = MagicMock()
    registry = SimpleNamespace(active_task_summaries=lambda: [], pop_unnotified_finished=lambda: [])
    monkeypatch.setattr(
        "artemis.agents.operator.operator.get_adb_task_registry", lambda ctx: registry
    )
    llm = MagicMock()
    llm.bind_tools.return_value = llm
    monkeypatch.setattr("artemis.agents.operator.operator.get_llm", lambda **kwargs: llm)
    state = State.initial("Inspect a mock page")
    state.latest_ui_hierarchy = []
    state.operator_raw_data = {
        "screenshot_b64": "AA==",
        "xml_hierarchy": [],
        "width": 100,
        "height": 200,
    }
    return node, state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content", ["I cannot proceed without the required input.", "[]", "null", ""]
)
async def test_actual_operator_repeated_plain_replies_fail_and_keep_feedback(
    fake_operator, monkeypatch, content
):
    node, state = fake_operator
    response = AIMessage(content=content)
    completion = AsyncMock(return_value=response)
    monkeypatch.setattr("artemis.agents.operator.operator.acomplete", completion)
    for _ in range(3):
        result = await node(state)
        assert result["structured_decisions"] is None
        assert response in node._stage_transcript_turn.call_args.args[0]
    with pytest.raises(OperatorNoActionLimitError, match="task is not successful"):
        await node(state)
    assert completion.await_count == 4  # No hidden coercive tool retry.
    assert response in node._stage_transcript_turn.call_args.args[0]


@pytest.mark.asyncio
async def test_cancelled_or_pending_turn_does_not_consume_guard_budget(fake_operator, monkeypatch):
    node, state = fake_operator
    pause = asyncio.Event()

    async def pending_model(*args, **kwargs):
        await pause.wait()
        return AIMessage(content="")

    monkeypatch.setattr("artemis.agents.operator.operator.acomplete", pending_model)
    task = asyncio.create_task(node(state))
    await asyncio.sleep(0)
    assert node._progress_guard.consecutive_no_action_turns == 0
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert node._progress_guard.consecutive_no_action_turns == 0


@pytest.mark.asyncio
async def test_helper_only_tool_budget_is_also_bounded_across_graph_turns(
    fake_operator, monkeypatch
):
    node, state = fake_operator
    node._invoke_llm_loop = AsyncMock(return_value=(None, None, None, True))
    for _ in range(3):
        result = await node(state)
        assert result["operator_tool_limit_exceeded"] is True
    with pytest.raises(OperatorNoActionLimitError):
        await node(state)
