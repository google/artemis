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

"""replay_steps: range replay, per-call step cap, whole-step token budget,
``[Screen]`` status lines, and the no-history degradation."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from artemis.tools.history import ReplayStepsTool, replay_steps, replay_steps_text


def _friendly_step(number, tool_result="The toggle is ON", **overrides):
    step = {
        "step_id": f"step_{number}",
        "step_number": number,
        "relative_time": f"{number * 3}.0s",
        "summary": f"Step {number} summary.",
        "action_taken": {"action": "click", "target": [0.5, 0.5], "target_text": "Save"},
        "last_execution_result": {"status": "success"},
        "interleaved_events": [
            {"type": "thought", "content": f"Thinking at step {number}."},
            {
                "type": "tool_call",
                "name": "ask_explorer",
                "args": {"question": "toggle?"},
                "result": tool_result,
            },
        ],
    }
    step.update(overrides)
    return step


def _reader(steps):
    reader = MagicMock()
    reader.get_agent_friendly_steps_in_range.side_effect = lambda s, e: [
        st for st in steps if s <= st["step_number"] <= e
    ]
    return reader


def test_replay_range_renders_every_step_in_full():
    reader = _reader([_friendly_step(3), _friendly_step(4, tool_result="The toggle is OFF " * 100)])
    out = replay_steps_text(reader, 3, 4)

    reader.get_agent_friendly_steps_in_range.assert_called_once_with(3, 4)
    assert "- **Step 3 (Start: 9.0s)**" in out
    assert "- **Step 4 (Start: 12.0s)**" in out
    assert "[Screen]: Step 3 summary." in out
    assert "Thinking at step 4." in out
    assert "`ask_explorer(" in out
    # Loose clamp: a ~1.8k-char tool result is replayed in full.
    assert "The toggle is OFF " * 100 in out
    assert "[Planned Action]: Tapped 'Save' at [0.5, 0.5]" in out


def test_replay_single_step_reversed_bounds_and_missing_step():
    reader = _reader([_friendly_step(2)])
    assert "- **Step 2 (Start: 6.0s)**" in replay_steps_text(reader, 2)
    reader.get_agent_friendly_steps_in_range.assert_called_once_with(2, 2)

    reader = _reader([_friendly_step(2), _friendly_step(3)])
    out = replay_steps_text(reader, 3, 2)
    reader.get_agent_friendly_steps_in_range.assert_called_once_with(2, 3)
    assert out.index("**Step 2") < out.index("**Step 3")

    assert replay_steps_text(_reader([]), 9) == "Error: step 9 not found."
    assert replay_steps_text(_reader([]), 9, 12) == "Error: no recorded steps in range 9–12."
    assert "must be integers" in replay_steps_text(_reader([]), "x")


def test_replay_caps_the_range_per_call():
    reader = _reader([_friendly_step(n) for n in range(1, 30)])
    out = replay_steps_text(reader, 1, 20, max_steps=5)
    reader.get_agent_friendly_steps_in_range.assert_called_once_with(1, 5)
    assert "**Step 5" in out
    assert "**Step 6" not in out
    assert "Call again from Step 6" in out


def test_replay_token_budget_drops_whole_trailing_steps():
    big = "x" * 4000  # ~1k tokens per step
    reader = _reader([_friendly_step(n, tool_result=big) for n in range(1, 6)])
    out = replay_steps_text(reader, 1, 5, max_steps=5, max_tokens=2500)

    # Two steps fit; the rest are dropped as whole steps, never cut mid-step.
    assert "**Step 1" in out and "**Step 2" in out
    assert "**Step 3" not in out
    assert out.count(big) == 2
    assert "Token budget reached: Steps 1–2 replayed; 3 more step(s)" in out
    assert "Call again from Step 3" in out


def test_replay_always_shows_at_least_the_first_step():
    reader = _reader([_friendly_step(1, tool_result="y" * 4000), _friendly_step(2)])
    out = replay_steps_text(reader, 1, 2, max_tokens=10)
    assert "**Step 1" in out
    assert "y" * 4000 in out
    assert "**Step 2" not in out


def test_replay_screen_line_states():
    with_text = replay_steps_text(_reader([_friendly_step(1)]), 1)
    assert "[Screen]: Step 1 summary." in with_text
    assert "[Summary]" not in with_text

    pending = replay_steps_text(
        _reader([_friendly_step(1, summary=None, extra_metadata={"summary_status": "pending"})]),
        1,
    )
    assert "[Screen]: (screen description pending)" in pending

    failed = replay_steps_text(
        _reader([_friendly_step(1, summary="", extra_metadata={"summary_status": "failed"})]),
        1,
    )
    assert "[Screen]: (screen description unavailable)" in failed

    no_status = replay_steps_text(_reader([_friendly_step(1, summary=None)]), 1)
    assert "[Screen]" not in no_status


def test_replay_without_history():
    assert "no execution history" in replay_steps_text(None, 1)


@pytest.mark.asyncio
async def test_tool_execute_reads_the_context_engine_and_degrades_without_one():
    reader = _reader([_friendly_step(2)])
    out = await ReplayStepsTool().execute(ctx=SimpleNamespace(data_engine=reader), start_step=2)
    assert "- **Step 2 (Start: 6.0s)**" in out

    out = await replay_steps.execute(ctx=SimpleNamespace(data_engine=None), start_step=1)
    assert "no execution history" in out
    assert replay_steps.is_available(SimpleNamespace(data_engine=None)) is False
    assert replay_steps.is_available(SimpleNamespace(data_engine=reader)) is True
