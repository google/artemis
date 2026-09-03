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

"""FlashRunner over the session transcript ledger (Pro-identical history).

Covers: the ``video_analyzer`` availability gate (same as the Pro graph) and
its prompt segment, the unbounded-by-default turn limit, the Pro-shaped
observation tail (``# CURRENT OBSERVATION [T+mm:ss]`` header, screenshot,
``--- Visible UI Elements ---`` list), and the end-to-end loop: committed
turns carry text-only tool messages plus an ``--- Action Execution Result
(T+mm:ss) ---`` message, every recorded step of a multi-action turn is
registered on the committed turn, the final-turn tool restriction only
applies to bounded loops, and a no-tool-call turn is nudged, not terminated.
"""

import base64
import re
from unittest.mock import AsyncMock, Mock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from artemis.agents.flash.runner import FlashRunner
from artemis.agents.validator.tool_declarations import ToolExecutionResult
from artemis.context import ArtemisContext
from artemis.graph.state import State
from artemis.memory.transcript import (
    EXECUTION_RESULT_MARKER,
    PRO_UI_LIST_MARKER,
    TranscriptLedger,
)

OBSERVATION_HEADER_RE = re.compile(r"^# CURRENT OBSERVATION \[T\+\d{2,}:\d{2}\]$")
RESULT_RE = re.compile(
    rf"^{re.escape(EXECUTION_RESULT_MARKER)} \(T\+\d{{2,}}:\d{{2}}\) ---\nStatus: success$"
)


@pytest.fixture
def mock_context():
    ctx = Mock(spec=ArtemisContext)
    ctx.llm_config = Mock()
    mock_llm_cfg = Mock()
    mock_llm_cfg.model = "gemini-2.5-flash"
    mock_llm_cfg.temperature = 0.1
    ctx.llm_config.get_agent.return_value = mock_llm_cfg
    ctx.device = Mock()
    ctx.device.device_width = 1080
    ctx.device.device_height = 2400
    ctx.data_engine = None
    ctx.adb_client = None
    ctx.driver = Mock()
    return ctx


# ---------------------------------------------------------------------------
# video_analyzer availability (same gate as the Pro graph)
# ---------------------------------------------------------------------------


def test_video_analyzer_bound_only_when_recording_tools_enabled(mock_context):
    with patch("artemis.controllers.unified_controller.get_driver"):
        mock_context.execution_setup = Mock()
        mock_context.execution_setup.video_recording_tools_enabled = True
        names = [t.name for t in FlashRunner(mock_context, goal="g")._get_tools()]
        assert "video_analyzer" in names
        assert names[-1] == "report_task_status"

        mock_context.execution_setup.video_recording_tools_enabled = False
        names = [t.name for t in FlashRunner(mock_context, goal="g")._get_tools()]
        assert "video_analyzer" not in names

        # A Mock attribute (truthy but not True) must not enable the tool.
        mock_context.execution_setup = Mock()
        names = [t.name for t in FlashRunner(mock_context, goal="g")._get_tools()]
        assert "video_analyzer" not in names


HISTORY_TOOLS = {"search_history", "replay_steps", "get_step_screenshot"}


def test_history_tools_bound_only_with_a_data_engine_session(mock_context):
    with patch("artemis.controllers.unified_controller.get_driver"):
        # No DataEngine: nothing to read, the tools stay out (as in Pro).
        names = [t.name for t in FlashRunner(mock_context, goal="g")._get_tools()]
        assert not (HISTORY_TOOLS & set(names))
        prompt = FlashRunner(mock_context, goal="g")._render_system_prompt(
            FlashRunner(mock_context, goal="g")._get_tools()
        )
        assert "search_history" not in prompt and "replay_steps" not in prompt

        mock_context.data_engine = Mock()
        with patch("artemis.tools.history._recall_config", return_value=None):
            runner = FlashRunner(mock_context, goal="g")
            tools = runner._get_tools()
        names = [t.name for t in tools]
        assert HISTORY_TOOLS <= set(names)
        assert names[-1] == "report_task_status"

        # Same declarations as the LangChain tools: derived from one args schema.
        search = next(t for t in tools if t.name == "search_history")
        assert set(search.parameters["properties"]) == {"query", "step_range", "max_results"}
        assert search.parameters["required"] == []
        replay = next(t for t in tools if t.name == "replay_steps")
        assert set(replay.parameters["properties"]) == {"start_step", "end_step"}
        assert replay.parameters["required"] == ["start_step"]
        shot = next(t for t in tools if t.name == "get_step_screenshot")
        assert shot.parameters["properties"]["which"]["enum"] == ["pre", "post", "overlay"]

        prompt = runner._render_system_prompt(tools)
        assert "`search_history`" in prompt
        assert "`replay_steps`" in prompt
        assert "`get_step_screenshot`" in prompt


def test_search_history_alone_follows_the_recall_config_gate(mock_context):
    from types import SimpleNamespace

    with patch("artemis.controllers.unified_controller.get_driver"):
        mock_context.data_engine = Mock()
        with patch(
            "artemis.tools.history._recall_config",
            return_value=SimpleNamespace(enabled=False),
        ):
            names = [t.name for t in FlashRunner(mock_context, goal="g")._get_tools()]
        assert "search_history" not in names
        assert {"replay_steps", "get_step_screenshot"} <= set(names)


def test_video_analyzer_prompt_segment_follows_availability(mock_context):
    with patch("artemis.controllers.unified_controller.get_driver"):
        runner = FlashRunner(mock_context, goal="g")
        mock_context.execution_setup = Mock()
        mock_context.execution_setup.video_recording_tools_enabled = True
        with_video = runner._render_system_prompt(runner._get_tools())
        assert "`video_analyzer`" in with_video

        mock_context.execution_setup.video_recording_tools_enabled = False
        without_video = runner._render_system_prompt(runner._get_tools())
        assert "video_analyzer" not in without_video
        # Session-clock / history teaching is unconditional.
        assert "T+mm:ss" in without_video
        assert "Action Execution Result" in without_video


# ---------------------------------------------------------------------------
# Turn limit: unbounded by default, explicit caps honoured
# ---------------------------------------------------------------------------


def test_turn_limit_semantics(mock_context):
    with patch("artemis.controllers.unified_controller.get_driver"):
        assert FlashRunner(mock_context, goal="g", max_turns=0).turn_limit is None
        assert FlashRunner(mock_context, goal="g", max_turns=7).turn_limit == 7
        with patch("artemis.agents.flash.runner.load_agent_config", side_effect=RuntimeError):
            assert FlashRunner(mock_context, goal="g").turn_limit is None


# ---------------------------------------------------------------------------
# Observation tail: Pro shape with the session-relative header
# ---------------------------------------------------------------------------


def test_observation_tail_has_pro_shape(mock_context):
    with patch("artemis.controllers.unified_controller.get_driver"):
        runner = FlashRunner(mock_context, goal="Open Settings")
        ledger = TranscriptLedger()

        tail = runner._build_tail(ledger, 1, b"IMG", "[1] Settings")
        texts = [b["text"] for b in tail.content if b["type"] == "text"]
        assert texts[0] == "Your objective is: Open Settings"
        assert OBSERVATION_HEADER_RE.match(texts[1])
        assert texts[2] == "--- Current Screenshot ---"
        assert texts[3] == f"{PRO_UI_LIST_MARKER}\n[1] Settings"
        assert sum(1 for b in tail.content if b["type"] == "image_url") == 1

        later = runner._build_tail(
            ledger, 5, None, None, injected="[INJECTED] stop", notices=["nudge"], is_final=True
        )
        texts = [b["text"] for b in later.content]
        assert OBSERVATION_HEADER_RE.match(texts[0])
        assert "Your objective is" not in "".join(texts)
        assert texts[1:] == [
            "nudge",
            "[INJECTED] stop",
            "[WARNING] This is your final turn; only 'report_task_status' is available.",
        ]


# ---------------------------------------------------------------------------
# End-to-end loop over the ledger (mocked model / executor)
# ---------------------------------------------------------------------------


def _exec_result(tc_id, name, post_bytes, ui="[1] next"):
    return ToolExecutionResult(
        tool_call_id=tc_id,
        tool_name=name,
        status="success",
        text_summary=f"{name} executed",
        screenshot_bytes=post_bytes,
        ui_elements_text=ui,
    )


def _make_runner(mock_context, responses, max_turns=None):
    runner = FlashRunner(mock_context, goal="Open Settings", max_turns=max_turns)
    runner.summarizer = None
    runner._init_llm = Mock(return_value=Mock())
    # Snapshot each invocation: the runner appends to the same message list
    # after the call, so the recorded call args would otherwise drift.
    runner.calls = []
    pending = list(responses)

    async def _invoke(llm, tools, messages):
        runner.calls.append({"tools": list(tools), "messages": list(messages)})
        return pending.pop(0)

    runner._invoke_model = AsyncMock(side_effect=_invoke)
    runner.executor.execute = AsyncMock(
        side_effect=lambda name, args, tc_id, state: _exec_result(
            tc_id, name, f"IMG-{tc_id}".encode()
        )
    )
    return runner


def _report(status="completed", tc_id="tc-report", **extra):
    return AIMessage(
        content="Done.",
        tool_calls=[
            {"name": "report_task_status", "args": {"status": status, **extra}, "id": tc_id}
        ],
    )


_INITIAL_OBSERVATION = ("shot0.png", b"IMG0", "[1] Settings")


@pytest.mark.asyncio
async def test_run_builds_prompt_from_ledger_with_session_offsets(mock_context):
    """Turn 2 must see: the static system prefix, the committed turn 1 (its
    tail, the AI message, text-only tool messages and an execution-result
    message with a ``T+`` offset) and a fresh observation tail carrying the
    last action's post screenshot — the Pro transcript shape, with no cap."""
    responses = [
        AIMessage(
            content="I will tap then wait.",
            tool_calls=[
                {"name": "click", "args": {"target": [500, 600]}, "id": "tc1"},
                {"name": "wait_for_delay", "args": {"seconds": 1}, "id": "tc2"},
            ],
        ),
        _report(explanation="ok"),
    ]
    with (
        patch("artemis.controllers.unified_controller.get_driver"),
        patch(
            "artemis.agents.flash.runner.capture_screenshot_and_parse_ui",
            AsyncMock(return_value=_INITIAL_OBSERVATION),
        ),
    ):
        runner = _make_runner(mock_context, responses, max_turns=0)
        assert runner.turn_limit is None
        result = await runner.run(State(initial_goal="Open Settings"))

    assert result == {"status": "completed", "explanation": "ok"}
    assert runner._invoke_model.await_count == 2

    turn2_messages = runner.calls[1]["messages"]
    assert isinstance(turn2_messages[0], SystemMessage)
    assert "T+mm:ss" in turn2_messages[0].content

    tail1 = turn2_messages[1]
    assert isinstance(tail1, HumanMessage)
    tail1_texts = [b["text"] for b in tail1.content if b["type"] == "text"]
    assert tail1_texts[0] == "Your objective is: Open Settings"
    assert OBSERVATION_HEADER_RE.match(tail1_texts[1])

    assert isinstance(turn2_messages[2], AIMessage)
    tool_msgs = [m for m in turn2_messages if isinstance(m, ToolMessage)]
    assert [m.content for m in tool_msgs] == ["click executed", "wait_for_delay executed"]

    result_msg = turn2_messages[5]
    assert isinstance(result_msg, HumanMessage)
    assert RESULT_RE.match(result_msg.content[0]["text"])

    tail2 = turn2_messages[6]
    tail2_texts = [b["text"] for b in tail2.content if b["type"] == "text"]
    assert OBSERVATION_HEADER_RE.match(tail2_texts[0])
    assert "Your objective is" not in "".join(tail2_texts)
    images = [b for b in tail2.content if b["type"] == "image_url"]
    assert len(images) == 1
    assert images[0]["image_url"]["url"].endswith(base64.b64encode(b"IMG-tc2").decode())
    assert tail2_texts[1] == "--- Current Screenshot ---"
    assert tail2_texts[2] == f"{PRO_UI_LIST_MARKER}\n[1] next"
    assert len(turn2_messages) == 7

    # Both actions of the multi-action turn were registered on the committed turn.
    ledger = mock_context.transcript_ledger
    assert ledger.unchunked_turns()[0]["step_keys"] == ["tc1", "tc2"]


@pytest.mark.asyncio
async def test_run_final_turn_restricts_tools_and_fails_without_report(mock_context):
    responses = [AIMessage(content="I give up.", tool_calls=[])]
    with (
        patch("artemis.controllers.unified_controller.get_driver"),
        patch(
            "artemis.agents.flash.runner.capture_screenshot_and_parse_ui",
            AsyncMock(return_value=_INITIAL_OBSERVATION),
        ),
    ):
        runner = _make_runner(mock_context, responses, max_turns=1)
        result = await runner.run(State(initial_goal="Open Settings"))

    assert result == {"status": "failed", "explanation": "I give up."}
    tools = runner.calls[0]["tools"]
    assert [t.name for t in tools] == ["report_task_status"]
    tail = runner.calls[0]["messages"][-1]
    assert any("final turn" in b.get("text", "") for b in tail.content)


@pytest.mark.asyncio
async def test_run_no_tool_call_turn_is_nudged_not_terminated(mock_context):
    responses = [AIMessage(content="thinking only", tool_calls=[]), _report()]
    with (
        patch("artemis.controllers.unified_controller.get_driver"),
        patch(
            "artemis.agents.flash.runner.capture_screenshot_and_parse_ui",
            AsyncMock(return_value=_INITIAL_OBSERVATION),
        ),
    ):
        runner = _make_runner(mock_context, responses, max_turns=0)
        result = await runner.run(State(initial_goal="Open Settings"))

    assert result == {"status": "completed"}
    turn2_messages = runner.calls[1]["messages"]
    # Unbounded loop: no final-turn restriction was ever applied.
    tools = runner.calls[1]["tools"]
    assert "click" in [t.name for t in tools]
    tail2_texts = [b["text"] for b in turn2_messages[-1].content if b["type"] == "text"]
    assert any("did not call any tools" in t for t in tail2_texts)
    # A helper-only / empty turn commits without an execution-result message.
    assert not any(
        isinstance(m, HumanMessage)
        and any(EXECUTION_RESULT_MARKER in b.get("text", "") for b in m.content)
        for m in turn2_messages
    )
