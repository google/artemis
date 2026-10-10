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

"""Tests for the Operator standing focus reminder.

Sliding-window models (e.g. Gemma's 512-token local window) reliably attend
only to the tail end of the whole prompt; the static system-prompt sections
restating the ledger protocol are outside it once the prompt grows. The
ActiveFocusPromptComponent re-states the active sub-goal plus the two rules
the bounce checks enforce as the last block of every observation, marked
ephemeral so it never reaches the committed transcript.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from artemis.agents.operator.operator import OperatorNode
from artemis.agents.operator.prompts import FOCUS_REMINDER_MARKER
from artemis.config.agent import MemoryTranscriptConfig
from artemis.context import ArtemisContext

LEGACY_TRANSCRIPT = MemoryTranscriptConfig(enabled=False)

PLAN_WITH_LEAF = """- [x] Open Google Maps and search for SFO
- [/] Read the commute duration and record it into note `commute_eta_info`
  - [/] Read the driving duration
    - [/] Tap the "Driving" tab so the fastest route is highlighted
- [ ] Draft the ETA message
"""

PLAN_NO_LEAF = """- [x] Open Google Maps and search for SFO
- [/] Read the commute duration and record it into note `commute_eta_info`
  - [ ] Read the driving duration
- [ ] Draft the ETA message
"""

PLAN_ALL_DONE = """- [x] Open Google Maps and search for SFO
- [x] Read the commute duration and record it into note `commute_eta_info`
"""


def _make_node(tmp_path, plan: str):
    notes = tmp_path / "notes"
    notes.mkdir(parents=True)
    (notes / "task_plan.md").write_text(plan, encoding="utf-8")

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


def _click_response(content="ok"):
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


def _last_human_texts(messages):
    """The text blocks of the final human message, in order."""
    human = [m for m in messages if getattr(m, "type", None) == "human"][-1]
    return [b.get("text", "") for b in human.content if isinstance(b, dict) and b.get("text")]


@pytest.mark.asyncio
async def test_focus_block_is_the_last_tail_block(tmp_path):
    node, state = _make_node(tmp_path, PLAN_WITH_LEAF)
    seen = []

    async def ainvoke(messages, *args, **kwargs):
        seen.append(list(messages))
        return _click_response()

    llm = MagicMock()
    llm.ainvoke = AsyncMock(side_effect=ainvoke)
    llm.bind_tools.return_value = llm
    with patch("artemis.agents.operator.operator.get_llm", return_value=llm):
        await node(state)

    blocks = _last_human_texts(seen[0])
    focus = [b for b in blocks if FOCUS_REMINDER_MARKER in b]
    assert len(focus) == 1, "exactly one focus block"
    assert blocks[-1] == focus[0], "focus block must be the tail's last block"
    assert "Read the commute duration" in focus[0]
    assert 'Tap the "Driving" tab' in focus[0]
    assert "update_note" in focus[0]
    assert "Turn-Ending Action" in focus[0]


@pytest.mark.asyncio
async def test_focus_block_nudges_a_leafless_milestone(tmp_path):
    node, state = _make_node(tmp_path, PLAN_NO_LEAF)
    seen = []

    async def ainvoke(messages, *args, **kwargs):
        seen.append(list(messages))
        return _click_response()

    llm = MagicMock()
    llm.ainvoke = AsyncMock(side_effect=ainvoke)
    llm.bind_tools.return_value = llm
    with patch("artemis.agents.operator.operator.get_llm", return_value=llm):
        await node(state)

    focus = [b for b in _last_human_texts(seen[0]) if FOCUS_REMINDER_MARKER in b]
    assert len(focus) == 1
    assert "no `[/]` sub-goal" in focus[0]


@pytest.mark.asyncio
async def test_focus_block_skipped_when_plan_is_done(tmp_path):
    node, state = _make_node(tmp_path, PLAN_ALL_DONE)
    seen = []

    async def ainvoke(messages, *args, **kwargs):
        seen.append(list(messages))
        return _click_response()

    llm = MagicMock()
    llm.ainvoke = AsyncMock(side_effect=ainvoke)
    llm.bind_tools.return_value = llm
    with patch("artemis.agents.operator.operator.get_llm", return_value=llm):
        await node(state)

    assert FOCUS_REMINDER_MARKER not in _texts(seen[0])
