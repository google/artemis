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

"""Operator dual prompt path (M2): legacy golden parity and transcript mode.

Flag off (``agent.memory.transcript.enabled=false``): the legacy 2-message
build must stay byte-for-byte identical — pinned by SHA-256 goldens recorded
against the pre-split implementation.

Flag on: ``_build_prompt`` renders ``S + F + A + tail`` from the session
transcript ledger; the turn's messages are staged after the tool loop and
committed (with step key and validator result) at the next build.
"""

import base64
import hashlib
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from artemis.agents.operator.operator import OperatorNode
from artemis.agents.operator.prompt_builder import load_operator_prompts
from artemis.agents.operator.prompts import (
    PLAN_HISTORY_STATIC_POINTER,
    PLAN_HISTORY_TEMPLATE_SECTION,
    PromptBuilder,
    TemplatePromptComponent,
    render_transcript_static_system,
)
from artemis.config.agent import MemoryTranscriptConfig
from artemis.context import ArtemisContext
from artemis.memory.step_memory import StepMemoryService
from artemis.memory.transcript import (
    EXECUTION_RESULT_MARKER,
    PLAN_RECITATION_MARKER,
    RESTORED_HISTORY_HEADER,
    TranscriptLedger,
)

# SHA-256 of the legacy system message rendered by TemplatePromptComponent
# with the fixed inputs below. Originally recorded BEFORE the M2 template
# split; re-recorded once in M5 (2026-09-01) after the only intended template
# change since — removing the Short-Term Memory instruction block from
# operator.json (the <short_term_memory> relay was removed end to end per
# pro-context-memory-redesign §3.3). Any other drift here means the flag-off
# path is no longer byte-identical.
GOLDEN_EMPTY_PLAN = "c45aec7d67069dad3a195534dd3c97dadb48454ca186a40eb471d5f9136589fa"
GOLDEN_SENTINEL_PLAN = "670580efd48f237c3257d996a814145fbef4ec0bb467aa4dfa6eb2db4a613826"

SCREENSHOT_B64 = base64.b64encode(b"fake-jpeg-bytes").decode("utf-8")


async def _render_legacy_system(plan_and_history: str) -> str:
    builder = PromptBuilder()
    component = TemplatePromptComponent()
    ctx = SimpleNamespace(execution_setup=None, actuator=None, data_engine=None)
    state = SimpleNamespace(initial_goal="GOLDEN_FIXED_GOAL")
    await component(
        builder,
        state,
        ctx,
        prompts=load_operator_prompts(),
        template_name="main_template",
        plan_and_history=plan_and_history,
    )
    return "".join(builder.system_parts)


@pytest.mark.asyncio
async def test_legacy_prompt_bytes_match_pre_split_golden():
    empty = await _render_legacy_system("No plan or history yet.")
    sentinel = await _render_legacy_system("PLAN_AND_HISTORY_SENTINEL\nline2")
    assert hashlib.sha256(empty.encode("utf-8")).hexdigest() == GOLDEN_EMPTY_PLAN
    assert hashlib.sha256(sentinel.encode("utf-8")).hexdigest() == GOLDEN_SENTINEL_PLAN


def test_main_template_contains_exactly_one_plan_history_section():
    template = load_operator_prompts()["main_template"]
    assert template.count(PLAN_HISTORY_TEMPLATE_SECTION) == 1


def test_transcript_static_system_is_stable_and_carries_no_history():
    ctx = SimpleNamespace(execution_setup=None, actuator=None, data_engine=None)
    state = SimpleNamespace(initial_goal="STATIC_GOAL")
    prompts = load_operator_prompts()

    first = render_transcript_static_system(prompts, ctx, state)
    second = render_transcript_static_system(prompts, ctx, state)
    assert first == second  # byte-stable S region

    assert "STATIC_GOAL" in first
    # The volatile section is replaced by the static pointer...
    assert PLAN_HISTORY_STATIC_POINTER.splitlines()[1] in first
    # ...and no unrendered slot or trailing observation header survives.
    assert "{{" not in first
    assert not first.endswith("# CURRENT OBSERVATION\n")


def _transcript_ctx():
    ctx = MagicMock(spec=ArtemisContext)
    ctx.execution_setup = None
    ctx.actuator = None
    ctx.data_engine = None
    ctx.step_memory = StepMemoryService(ctx=None)
    ctx.transcript_ledger = None
    return ctx


def _transcript_state(**overrides):
    state = MagicMock()
    state.subagent_calls = []
    state.initial_goal = "Transcript goal"
    state.operator_feedback = None
    state.injected_instruction = None
    state.operator_tool_limit_exceeded = False
    state.structured_decisions = None
    state.last_execution_result = None
    state.current_step_id = None
    state.latest_ui_hierarchy = []
    state.operator_raw_data = {
        "screenshot_b64": SCREENSHOT_B64,
        "xml_hierarchy": [],
        "ocr_results": None,
        "width": 1080,
        "height": 2400,
    }
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def _no_action_llm(captured: list):
    mock_llm = MagicMock()
    mock_response = MagicMock()
    mock_response.tool_calls = []
    mock_response.content = "no action this turn"

    async def mock_ainvoke(*args, **kwargs):
        captured.append(list(args[0]))
        return mock_response

    mock_llm.ainvoke = AsyncMock(side_effect=mock_ainvoke)
    mock_llm.bind_tools.return_value = mock_llm
    return mock_llm


@pytest.mark.asyncio
async def test_transcript_mode_two_turns_build_four_regions(tmp_path):
    ctx = _transcript_ctx()
    # Regression data shape (M2 cold-start bug, on-device A/B 2026-09-01):
    # turn 1 runs before any step record exists; by turn 2 the first step HAS
    # been recorded, so `steps` is non-empty while the previous turn is still
    # staged (turn_count == 0). The cold-start check must not read that
    # live-session state as a process restart — seeding would be rejected by
    # the empty-ledger invariant and silently demote every turn to the legacy
    # 2-message path.
    ctx.data_engine = MagicMock()
    ctx.data_engine.base_dir = str(tmp_path)
    ctx.data_engine.global_base_dir = str(tmp_path)
    ctx.data_engine.get_agent_friendly_steps.return_value = []
    captured: list = []
    mock_llm = _no_action_llm(captured)

    with patch("artemis.agents.operator.operator.get_llm", return_value=mock_llm):
        node = OperatorNode(ctx, transcript_config=MemoryTranscriptConfig(enabled=True))

        # Turn 1
        await node(_transcript_state())
        ledger = ctx.transcript_ledger
        assert isinstance(ledger, TranscriptLedger)
        assert ledger.has_static_prefix
        assert ledger.has_staged_turn
        assert ledger.turn_count == 0

        turn1_messages = captured[0]
        assert isinstance(turn1_messages[0], SystemMessage)
        assert isinstance(turn1_messages[-1], HumanMessage)
        tail1_text = str(turn1_messages[-1].content)
        assert "# CURRENT OBSERVATION [T+" in tail1_text
        assert PLAN_RECITATION_MARKER in tail1_text

        # Turn 2: step 1 is now recorded, and the previous turn is committed
        # with its step id and result.
        ctx.data_engine.get_agent_friendly_steps.return_value = [
            {
                "step_id": "step-1",
                "step_number": 1,
                "summary": "Step 1 summary",
                "relative_time": "1.0s",
                "extra_metadata": {"subgoal_hash": "default"},
            }
        ]
        await node(
            _transcript_state(
                current_step_id="step-1",
                structured_decisions='[{"action": "tap"}]',
                last_execution_result={"status": "success"},
            )
        )

        # The staged-but-uncommitted turn 2 state never triggers cold-start
        # seeding (the transcript path stayed live instead of falling back).
        assert not ledger.has_restored_history
        assert ledger.turn_count == 1
        turn2_messages = captured[1]
        # S region is the same object with the same bytes.
        assert turn2_messages[0] is turn1_messages[0]

        active = list(ledger.active_messages)
        # Committed turn-1 observation followed by its validator result.
        assert any(EXECUTION_RESULT_MARKER in str(m.content) for m in active)
        committed_obs = active[0]
        # Depth-1 scrub removed the old plan recitation copy.
        assert PLAN_RECITATION_MARKER not in str(committed_obs.content)
        # The live tail still recites the plan.
        assert PLAN_RECITATION_MARKER in str(turn2_messages[-1].content)
        # Message layout is S + A + tail.
        assert turn2_messages[-1] is not turn1_messages[-1]
        assert re.search(r"T\+\d{2,}:\d{2}", str(turn2_messages[-1].content))


@pytest.mark.asyncio
async def test_transcript_actionless_turn_commits_without_validator_message():
    ctx = _transcript_ctx()
    captured: list = []
    mock_llm = _no_action_llm(captured)

    with patch("artemis.agents.operator.operator.get_llm", return_value=mock_llm):
        node = OperatorNode(ctx, transcript_config=MemoryTranscriptConfig(enabled=True))
        await node(_transcript_state())
        # Planner-rejected turns clear structured_decisions: no validator ran.
        await node(
            _transcript_state(
                current_step_id="step-1",
                structured_decisions="",
                last_execution_result={"status": "success"},
            )
        )

    ledger = ctx.transcript_ledger
    assert ledger.turn_count == 1
    assert not any(
        EXECUTION_RESULT_MARKER in str(m.content) for m in ledger.active_messages
    )


@pytest.mark.asyncio
async def test_transcript_cold_start_builds_restored_history_block(tmp_path):
    ctx = _transcript_ctx()
    ctx.data_engine = MagicMock()
    ctx.data_engine.base_dir = str(tmp_path)
    ctx.data_engine.global_base_dir = str(tmp_path)
    ctx.data_engine.get_agent_friendly_steps.return_value = [
        {
            "step_id": f"step_{i}",
            "step_number": i,
            "summary": f"Step {i} summary",
            "relative_time": f"{i}.0s",
            "extra_metadata": {"subgoal_hash": "default"},
        }
        for i in range(1, 4)
    ]

    captured: list = []
    mock_llm = _no_action_llm(captured)

    with patch("artemis.agents.operator.operator.get_llm", return_value=mock_llm):
        node = OperatorNode(ctx, transcript_config=MemoryTranscriptConfig(enabled=True))
        await node(_transcript_state())

    ledger = ctx.transcript_ledger
    assert ledger.has_restored_history

    messages = captured[0]
    restored_text = str(messages[1].content)
    assert RESTORED_HISTORY_HEADER in restored_text
    assert "--- Execution History ---" in restored_text
    assert "Step 1 summary" in restored_text
    # The live tail still follows normally and A stays appendable.
    assert "# CURRENT OBSERVATION [T+" in str(messages[-1].content)
    assert ledger.has_staged_turn


@pytest.mark.asyncio
async def test_flag_off_keeps_legacy_two_message_build_and_no_ledger():
    ctx = _transcript_ctx()
    captured: list = []
    mock_llm = _no_action_llm(captured)

    with patch("artemis.agents.operator.operator.get_llm", return_value=mock_llm):
        node = OperatorNode(ctx, transcript_config=MemoryTranscriptConfig(enabled=False))
        await node(_transcript_state())

    assert len(captured[0]) == 2
    assert isinstance(captured[0][0], SystemMessage)
    assert isinstance(captured[0][1], HumanMessage)
    assert ctx.transcript_ledger is None
