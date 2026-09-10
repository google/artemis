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

"""Tests for the Operator reasoning reminder.

Thinking-mode models satisfy "reason before acting" inside their thought channel
and emit bare tool calls. After such a silent turn the next observation tail
carries a one-line reminder; nothing is bounced and no extra model call is made.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from artemis.agents.operator.operator import OperatorNode
from artemis.agents.operator.prompts import (
    REASONING_REMINDER,
    apply_operator_prompt_contract,
    load_operator_prompts,
)
from artemis.config.agent import MemoryTranscriptConfig
from artemis.context import ArtemisContext

LEGACY_TRANSCRIPT = MemoryTranscriptConfig(enabled=False)

PLAN_WITH_LEAF = """- [x] Open Google Maps and search for SFO
- [/] Read the commute duration and record it into note `commute_eta_info`
  - [/] Read the driving duration
    - [/] Tap the "Driving" tab so the fastest route is highlighted
- [ ] Draft the ETA message
"""


def _make_node(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir(parents=True)
    (notes / "task_plan.md").write_text(PLAN_WITH_LEAF, encoding="utf-8")

    ctx = MagicMock(spec=ArtemisContext)
    ctx.execution_setup = None
    ctx.data_engine = MagicMock()
    ctx.data_engine.base_dir = str(tmp_path)
    ctx.data_engine.current_session_id = "s"
    ctx.data_engine.get_agent_friendly_steps.return_value = []

    state = MagicMock()
    state.subagent_calls = []
    state.initial_goal = "goal"
    state.open_incident = None
    return OperatorNode(ctx, transcript_config=LEGACY_TRANSCRIPT), state


def _click_response(content):
    response = MagicMock()
    response.content = content
    response.tool_calls = [
        {
            "name": "click",
            "args": {"target": [50, 50], "target_description": "button"},
            "id": "call_0",
        }
    ]
    return response


def _texts(messages):
    out = []
    for m in messages:
        content = getattr(m, "content", None)
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            out.extend(b.get("text", "") for b in content if isinstance(b, dict))
    return "\n".join(out)


@pytest.mark.asyncio
async def test_reminder_follows_a_silent_turn_and_costs_no_extra_call(tmp_path):
    node, state = _make_node(tmp_path)
    seen = []
    replies = iter(
        [
            _click_response(""),  # turn 1: silent
            _click_response("Tapping the Driving tab."),  # turn 2: reminded, speaks
            _click_response([{"type": "thinking", "thinking": "internal"}]),  # turn 3: silent
            _click_response("Done."),  # turn 4
        ]
    )

    async def ainvoke(messages, *args, **kwargs):
        seen.append(list(messages))
        return next(replies)

    llm = MagicMock()
    llm.ainvoke = AsyncMock(side_effect=ainvoke)
    llm.bind_tools.return_value = llm
    with patch("artemis.agents.operator.operator.get_llm", return_value=llm):
        for _ in range(4):
            await node(state)

    assert llm.ainvoke.await_count == 4, "one model call per turn, never a bounce"
    assert REASONING_REMINDER not in _texts(seen[0])  # nothing to remind on turn 1
    assert REASONING_REMINDER in _texts(seen[1])  # turn 1 was silent
    assert REASONING_REMINDER not in _texts(seen[2])  # turn 2 spoke
    assert REASONING_REMINDER in _texts(seen[3])  # thinking-only turn 3 counts as silent
    assert _texts(seen[3]).count(REASONING_REMINDER) == 1


def test_ledger_silence_verdict_outranks_the_local_flag(tmp_path):
    """With a transcript ledger, the ledger's own judgment of the previous
    turn is authoritative; the local flag only serves the legacy path."""
    from artemis.memory import TranscriptLedger

    node, _ = _make_node(tmp_path)
    node._previous_turn_silent = True
    assert node._previous_turn_was_silent() is True  # no ledger: local flag

    ledger = MagicMock(spec=TranscriptLedger)
    ledger.last_turn_silent = False
    node.ctx.transcript_ledger = ledger
    assert node._previous_turn_was_silent() is False

    ledger.last_turn_silent = True
    node._previous_turn_silent = False
    assert node._previous_turn_was_silent() is True


def test_prompt_asks_for_one_paragraph_not_phase_sections():
    prompt = apply_operator_prompt_contract(load_operator_prompts()["main_template"])
    assert prompt.count("one short paragraph of connected prose reflecting these three phases") == 1
    assert "in your raw thinking" not in prompt
    assert "in your written reply" not in prompt
    assert (
        "bounced"
        not in prompt.split("# THE THREE-PHASE COGNITIVE PROTOCOL")[1].split("## Phase 1")[0]
    )
