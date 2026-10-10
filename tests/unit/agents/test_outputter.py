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

import asyncio
from unittest.mock import AsyncMock, Mock, patch

from pydantic import BaseModel
import pytest

from artemis.agents.outputter.outputter import outputter
from artemis.config import LLM, OutputConfig  # noqa: E402
from artemis.context import ArtemisContext  # noqa: E402
from artemis.core.tool_failure import ToolFailure  # noqa: E402
from third_party.mobile_use import testing as agent_fixtures
from third_party.mobile_use.utils.logger import get_logger  # noqa: E402

logger = get_logger(__name__)

mock_context = agent_fixtures.mock_context
mock_state = agent_fixtures.mock_state


def setup_mock_llm(mock_get_llm, react_response_content="Paris", structured_response=None):
    mock_llm = Mock()
    mock_llm_fallback = Mock()

    # Mock bind_tools
    mock_llm_with_tools = Mock()
    mock_llm_with_tools.ainvoke = AsyncMock()
    mock_react_response = Mock()
    mock_react_response.content = react_response_content
    mock_react_response.tool_calls = []
    mock_llm_with_tools.ainvoke.return_value = mock_react_response

    mock_llm.bind_tools.return_value = mock_llm_with_tools
    mock_llm_fallback.bind_tools.return_value = mock_llm_with_tools

    # Mock with_structured_output
    mock_structured_llm = Mock()
    mock_structured_llm.ainvoke = AsyncMock()
    if structured_response is not None:
        mock_structured_llm.ainvoke.return_value = structured_response
    mock_llm.with_structured_output.return_value = mock_structured_llm
    mock_llm_fallback.with_structured_output.return_value = mock_structured_llm

    def get_llm_side_effect(ctx, name, is_utils=False, use_fallback=False):
        if use_fallback:
            return mock_llm_fallback
        return mock_llm

    mock_get_llm.side_effect = get_llm_side_effect

    return mock_llm, mock_llm_with_tools, mock_structured_llm


class _Verdict(BaseModel):
    achieved: bool
    evidence: str


# A Pydantic model or a JSON schema dict; the result is a plain dict in both cases.
_FORMAT_CASES = [
    pytest.param(_Verdict, _Verdict(achieved=True, evidence="Order placed"), id="pydantic_model"),
    pytest.param(
        _Verdict.model_json_schema(),
        {"achieved": True, "evidence": "Order placed"},
        id="json_schema",
    ),
]


@patch("artemis.agents.outputter.outputter.get_llm")
@pytest.mark.asyncio
@pytest.mark.parametrize("schema, formatted_reply", _FORMAT_CASES)
async def test_outputter_applies_schema_in_separate_formatting_pass(
    mock_get_llm, schema, formatted_reply, mock_context, mock_state
):
    """The ReAct answer is re-shaped by a second, schema-bound call that sees the goal."""
    react_answer = "The confirmation screen reads 'Order placed'."
    mock_llm, _, mock_structured_llm = setup_mock_llm(
        mock_get_llm, react_response_content=react_answer, structured_response=formatted_reply
    )

    result = await outputter(
        ctx=mock_context,
        output_config=OutputConfig(structured_output=schema),
        graph_output=mock_state,
    )

    assert result == {"achieved": True, "evidence": "Order placed"}
    mock_llm.with_structured_output.assert_called_once_with(schema)
    format_request = mock_structured_llm.ainvoke.call_args.args[0][-1].content
    assert mock_state.initial_goal in format_request
    assert react_answer in format_request


@patch("artemis.agents.outputter.outputter.get_llm")
@pytest.mark.asyncio
async def test_outputter_with_history_and_image(mock_get_llm, mock_context):
    """Test outputter with explicit history and image."""
    expected_json = '{"answer": "Paris"}'
    _, mock_llm_with_tools, _ = setup_mock_llm(mock_get_llm, react_response_content=expected_json)

    config = OutputConfig(
        structured_output=None,
        output_description="The capital of France",
    )

    class StateWithRawData:
        def __init__(self, messages, initial_goal, operator_raw_data):
            self.messages = messages
            self.initial_goal = initial_goal
            self.operator_raw_data = operator_raw_data

    state = StateWithRawData(
        messages=[],
        initial_goal="What is the capital of France?",
        operator_raw_data={"screenshot_b64": "mock_b64_data"},
    )

    await outputter(
        ctx=mock_context,
        output_config=config,
        graph_output=state,
        plan_and_history="Plan: Find capital\nStep 1: Opened browser",
    )

    call_args = mock_llm_with_tools.ainvoke.call_args
    assert call_args is not None
    messages = call_args[0][0]

    assert (
        "Your sole objective is to verify whether the user's initial goal was"
        " achieved" in messages[0].content
    )

    human_content = messages[1].content
    assert isinstance(human_content, list)
    assert human_content[0]["type"] == "text"
    assert "Step 1: Opened browser" in human_content[0]["text"]
    assert "Plan: Find capital" in human_content[0]["text"]
    assert human_content[1]["type"] == "image_url"
    assert human_content[1]["image_url"]["url"] == "data:image/png;base64,mock_b64_data"


@patch("artemis.agents.outputter.outputter.get_llm")
@pytest.mark.asyncio
async def test_outputter_builds_concise_history(mock_get_llm):
    """Test that the outputter dynamically builds clean Summary ➡️ Action history."""
    expected_json = '{"answer": "Success"}'
    _, mock_llm_with_tools, _ = setup_mock_llm(mock_get_llm, react_response_content=expected_json)

    config = OutputConfig(
        structured_output=None,
        output_description="Test output",
    )

    ctx = Mock(spec=ArtemisContext)
    ctx.llm_config = {
        "planner": LLM(provider="openai", model="gpt-5-nano"),
        "operator": LLM(provider="openai", model="gpt-5-nano"),
        "validator": LLM(provider="openai", model="gpt-5-nano"),
    }
    ctx.device = Mock()

    mock_data_engine = Mock()
    steps = [
        {
            "step_id": "step_1",
            "step_number": 1,
            "relative_time": "2.5s",
            "summary": "Tapped search button",
            "action_taken": [{"action": "click", "target_text": "Search"}],
            "last_execution_result": {"status": "success"},
        }
    ]
    mock_data_engine.get_agent_friendly_steps.return_value = steps
    # The transcript flag defaults on, so the outputter's compiled view
    # consults the chunk store; no persisted chunks keeps the classic path.
    mock_data_engine.get_history_chunks.return_value = []
    mock_data_engine.base_dir = "/tmp/mock_session"
    ctx.data_engine = mock_data_engine

    class StateWithRawData:
        def __init__(self, messages, initial_goal, operator_raw_data):
            self.messages = messages
            self.initial_goal = initial_goal
            self.operator_raw_data = operator_raw_data

    state = StateWithRawData(
        messages=[],
        initial_goal="Search for something",
        operator_raw_data=None,
    )

    # Test Case 1: Fallback path (no plan file)
    from pathlib import Path

    original_exists = Path.exists

    def mock_exists_fallback(self, *args, **kwargs):
        if self.name == "task_plan.md":
            return False
        return original_exists(self, *args, **kwargs)

    with patch("pathlib.Path.exists", mock_exists_fallback):
        await outputter(
            ctx=ctx,
            output_config=config,
            graph_output=state,
        )

    call_args = mock_llm_with_tools.ainvoke.call_args
    assert call_args is not None
    messages = call_args[0][0]
    human_content_fallback = messages[1].content[0]["text"]

    assert "--- Execution History ---" in human_content_fallback
    assert "- *Step 1 (Start: 2.5s): Tapped search button*" in human_content_fallback
    assert "*Action*: Tapped 'Search' at None" in human_content_fallback

    # Reset mock call history
    mock_llm_with_tools.ainvoke.reset_mock()

    # Test Case 2: Standard path (plan file exists)
    def mock_exists_standard(self, *args, **kwargs):
        if self.name == "task_plan.md":
            return True
        return original_exists(self, *args, **kwargs)

    original_read_text = Path.read_text

    def mock_read_text(self, *args, **kwargs):
        if self.name == "task_plan.md":
            return "- [ ] Goal 1\n  - [/] Subgoal 1.1"
        return original_read_text(self, *args, **kwargs)

    with (
        patch("pathlib.Path.exists", mock_exists_standard),
        patch("pathlib.Path.read_text", mock_read_text),
    ):
        await outputter(
            ctx=ctx,
            output_config=config,
            graph_output=state,
        )

    call_args = mock_llm_with_tools.ainvoke.call_args
    assert call_args is not None
    messages = call_args[0][0]
    human_content_standard = messages[1].content[0]["text"]

    assert "--- Execution History ---" in human_content_standard
    assert "- *Step 1 (Start: 2.5s): Tapped search button*" in human_content_standard
    assert "*Action*: Tapped 'Search' at None" in human_content_standard


@patch("artemis.tools.video_tool.VideoAnalyzer")
@patch("artemis.agents.outputter.outputter.get_llm")
@pytest.mark.asyncio
async def test_outputter_executes_video_analyzer_tool(
    mock_get_llm, mock_video_analyzer, mock_context, mock_state
):
    """Test that the outputter ReAct loop can successfully invoke the video_analyzer tool."""
    # Setup mock VideoAnalyzer
    mock_agent_instance = Mock()
    mock_agent_instance.run = AsyncMock(
        return_value=("The user played a video for 5 seconds.", "success")
    )
    mock_video_analyzer.return_value = mock_agent_instance

    # Setup mock LLM with two turns
    mock_llm = Mock()
    mock_llm_fallback = Mock()

    mock_llm_with_tools = Mock()
    mock_llm_with_tools.ainvoke = AsyncMock()

    # Turn 1: LLM calls video_analyzer tool
    tool_call = {
        "name": "video_analyzer",
        "args": {
            "time_description": "from 5s to 10s",
            "purpose": "verify video played",
        },
        "id": "call_123",
    }
    msg_turn1 = Mock()
    msg_turn1.content = ""
    msg_turn1.tool_calls = [tool_call]

    # Turn 2: LLM returns final answer
    msg_turn2 = Mock()
    msg_turn2.content = "Video played successfully."
    msg_turn2.tool_calls = []

    mock_llm_with_tools.ainvoke.side_effect = [msg_turn1, msg_turn2]

    mock_llm.bind_tools.return_value = mock_llm_with_tools
    mock_llm_fallback.bind_tools.return_value = mock_llm_with_tools

    def get_llm_side_effect(ctx, name, is_utils=False, use_fallback=False):
        if use_fallback:
            return mock_llm_fallback
        return mock_llm

    mock_get_llm.side_effect = get_llm_side_effect

    config = OutputConfig(
        structured_output=None,
        output_description="Verify video",
    )

    result = await outputter(ctx=mock_context, output_config=config, graph_output=mock_state)

    # Assert VideoAnalyzer was called correctly
    mock_video_analyzer.assert_called_once_with(mock_context)
    mock_agent_instance.run.assert_called_once_with("from 5s to 10s", "verify video played")

    # Assert LLM's ainvoke was called twice
    assert mock_llm_with_tools.ainvoke.call_count == 2

    # Assert tool message was appended (messages list is mutated in place,
    # so it eventually has 5 elements)
    sent_messages = mock_llm_with_tools.ainvoke.call_args_list[1][0][0]
    assert len(sent_messages) == 5

    # Verify the sequence of messages
    assert sent_messages[2] == msg_turn1

    tool_message = sent_messages[3]
    from langchain_core.messages import ToolMessage

    assert isinstance(tool_message, ToolMessage)
    assert tool_message.content == "The user played a video for 5 seconds."
    assert tool_message.status == "success"

    assert sent_messages[4] == msg_turn2

    # Assert final result
    assert result == "Video played successfully."


@patch("artemis.agents.outputter.outputter.get_llm")
@pytest.mark.asyncio
async def test_outputter_executes_save_note_tool(mock_get_llm, mock_context, mock_state):
    """Test that the outputter ReAct loop can successfully invoke the save_note tool."""
    # Setup mock LLM with two turns
    mock_llm = Mock()
    mock_llm_fallback = Mock()

    mock_llm_with_tools = Mock()
    mock_llm_with_tools.ainvoke = AsyncMock()

    # Turn 1: LLM calls save_note tool
    tool_call = {
        "name": "save_note",
        "args": {"key": "verification_code", "content": "123456"},
        "id": "call_save_123",
    }
    msg_turn1 = Mock()
    msg_turn1.content = ""
    msg_turn1.tool_calls = [tool_call]

    # Turn 2: LLM returns final answer
    msg_turn2 = Mock()
    msg_turn2.content = "Verification code is 123456."
    msg_turn2.tool_calls = []

    mock_llm_with_tools.ainvoke.side_effect = [msg_turn1, msg_turn2]

    mock_llm.bind_tools.return_value = mock_llm_with_tools
    mock_llm_fallback.bind_tools.return_value = mock_llm_with_tools

    def get_llm_side_effect(ctx, name, is_utils=False, use_fallback=False):
        if use_fallback:
            return mock_llm_fallback
        return mock_llm

    mock_get_llm.side_effect = get_llm_side_effect

    config = OutputConfig(
        structured_output=None,
        output_description="Save verification code to note",
    )

    # Setup mock data_engine on context to support saving notes
    mock_data_engine = Mock()
    mock_data_engine.base_dir = "/tmp/mock_session"
    mock_context.data_engine = mock_data_engine

    # Patch save_note_content utility to avoid writing to disk
    with patch("artemis.tools.scratchpad.save_note_content") as mock_save_content:
        result = await outputter(ctx=mock_context, output_config=config, graph_output=mock_state)
        mock_save_content.assert_called_once_with(
            "/tmp/mock_session", "verification_code", "123456"
        )

    # Assert final result
    assert result == "Verification code is 123456."


# --- tool results: status is structural, never sniffed from the words -----------------


# A helper tool reports failure structurally (``ToolFailure``); free-form text
# that merely starts with "Error" is an ordinary answer.
_STATUS_CASES = [
    pytest.param(ToolFailure("Error: note 'progress' not found"), "error", id="tool_failure"),
    pytest.param("Error 404 was typed into the search box", "success", id="plain_error_text"),
]


def _text_tool(name: str, result):  # noqa: D103
    from langchain_core.tools import StructuredTool

    async def _run(key: str) -> str:
        return result

    return StructuredTool.from_function(coroutine=_run, name=name, description=name)


@patch("artemis.agents.outputter.outputter.get_read_note_tool_pure")
@patch("artemis.agents.outputter.outputter.get_llm")
@pytest.mark.asyncio
@pytest.mark.parametrize("result, expected_status", _STATUS_CASES)
async def test_outputter_tool_message_status_is_structural(
    mock_get_llm, mock_get_read_note, result, expected_status, mock_context, mock_state
):
    from langchain_core.messages import AIMessage, ToolMessage

    mock_get_read_note.return_value = _text_tool("read_note", result)
    _, mock_llm_with_tools, _ = setup_mock_llm(mock_get_llm)
    tool_turn = AIMessage(
        content="", tool_calls=[{"name": "read_note", "args": {"key": "progress"}, "id": "c1"}]
    )
    final_turn = AIMessage(content="The answer.", tool_calls=[])
    mock_llm_with_tools.ainvoke.side_effect = [tool_turn, final_turn]

    config = OutputConfig(structured_output=None, output_description=None)
    answer = await outputter(ctx=mock_context, output_config=config, graph_output=mock_state)

    assert answer == "The answer."
    second_turn_messages = mock_llm_with_tools.ainvoke.call_args_list[1].args[0]
    tool_msgs = [m for m in second_turn_messages if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_call_id == "c1"
    assert tool_msgs[0].status == expected_status
    assert tool_msgs[0].content == str(result)


# Runs that end without a normal answer.


def _tool_call(name: str, call_id: str, **args):  # noqa: D103
    return {"name": name, "args": args, "id": call_id}


def _recording_tool(name: str, result, events: list, barrier=None):  # noqa: D103
    from langchain_core.tools import StructuredTool

    async def _run(**_kwargs):
        events.append(f"start:{name}")
        if barrier is not None:
            await asyncio.wait_for(barrier.wait(), timeout=2)
        await asyncio.sleep(0)
        events.append(f"end:{name}")
        return result

    return StructuredTool.from_function(coroutine=_run, name=name, description=name)


@patch("artemis.agents.outputter.outputter.get_llm")
@pytest.mark.asyncio
async def test_outputter_returns_text_for_content_block_replies(
    mock_get_llm, mock_context, mock_state
):
    """Gemini 3+ replies arrive as content blocks; the caller gets their text."""
    from langchain_core.messages import AIMessage

    _, bound, _ = setup_mock_llm(mock_get_llm)
    bound.ainvoke.return_value = AIMessage(
        content=[
            {"type": "thinking", "thinking": "internal"},
            {"type": "text", "text": "The order was placed."},
        ]
    )

    result = await outputter(
        ctx=mock_context, output_config=OutputConfig(), graph_output=mock_state
    )

    assert result == "The order was placed."


@patch("artemis.agents.outputter.outputter.get_llm")
@pytest.mark.asyncio
async def test_outputter_nudges_once_after_empty_reply(mock_get_llm, mock_context, mock_state):
    from langchain_core.messages import AIMessage, HumanMessage

    _, bound, _ = setup_mock_llm(mock_get_llm)
    bound.ainvoke.side_effect = [AIMessage(content=""), AIMessage(content="Done.")]

    result = await outputter(
        ctx=mock_context, output_config=OutputConfig(), graph_output=mock_state
    )

    assert result == "Done."
    assert bound.ainvoke.call_count == 2
    sent = bound.ainvoke.call_args_list[1].args[0]
    assert isinstance(sent[2], HumanMessage)  # the nudge, not the empty AI turn
    assert not any(isinstance(m, AIMessage) and not m.content for m in sent)


@patch("artemis.agents.outputter.outputter.get_llm")
@pytest.mark.asyncio
async def test_outputter_nudges_only_once(mock_get_llm, mock_context, mock_state):
    from langchain_core.messages import AIMessage

    _, bound, _ = setup_mock_llm(mock_get_llm)
    bound.ainvoke.side_effect = [AIMessage(content=""), AIMessage(content=""), AssertionError()]

    result = await outputter(
        ctx=mock_context, output_config=OutputConfig(), graph_output=mock_state
    )

    assert bound.ainvoke.call_count == 2
    assert result.startswith("Error: Outputter failed")


@patch("artemis.agents.outputter.outputter.MAX_TURNS", 2)
@patch("artemis.agents.outputter.outputter.get_read_note_tool_pure")
@patch("artemis.agents.outputter.outputter.get_llm")
@pytest.mark.asyncio
async def test_outputter_final_turn_runs_no_tools_and_returns_text(
    mock_get_llm, mock_get_read_note, mock_context, mock_state
):
    from langchain_core.messages import AIMessage, HumanMessage

    events: list[str] = []
    mock_get_read_note.return_value = _recording_tool("read_note", "note body", events)
    _, bound, _ = setup_mock_llm(mock_get_llm)
    bound.ainvoke.side_effect = [
        AIMessage(content="", tool_calls=[_tool_call("read_note", "c1", key="a")]),
        AIMessage(content="Best answer so far.", tool_calls=[_tool_call("read_note", "c2")]),
    ]

    result = await outputter(
        ctx=mock_context, output_config=OutputConfig(), graph_output=mock_state
    )

    assert result == "Best answer so far."
    assert events == ["start:read_note", "end:read_note"]  # the final turn's call never ran
    final_request = bound.ainvoke.call_args_list[1].args[0]
    assert any(
        isinstance(m, HumanMessage) and "final turn" in str(m.content) for m in final_request
    )


@patch("artemis.agents.outputter.outputter.MAX_TURNS", 2)
@patch("artemis.agents.outputter.outputter.get_llm")
@pytest.mark.asyncio
async def test_outputter_falls_back_to_saved_report(
    mock_get_llm, mock_context, mock_state, tmp_path
):
    """Without a final answer, the report written this run is returned, not an error."""
    from langchain_core.messages import AIMessage

    mock_context.data_engine = Mock(base_dir=str(tmp_path))
    _, bound, _ = setup_mock_llm(mock_get_llm)
    report = "# Report\nStatus: SUCCESS"
    bound.ainvoke.side_effect = [
        AIMessage(
            content="", tool_calls=[_tool_call("save_note", "c1", key="output", content=report)]
        ),
        AIMessage(content=""),
    ]

    result = await outputter(
        ctx=mock_context,
        output_config=OutputConfig(),
        graph_output=mock_state,
        plan_and_history="Step 1: placed order",
    )

    assert result == report


@patch("artemis.agents.outputter.outputter.get_llm")
@pytest.mark.asyncio
async def test_outputter_formatting_pass_sees_saved_report(
    mock_get_llm, mock_context, mock_state, tmp_path
):
    from langchain_core.messages import AIMessage

    mock_context.data_engine = Mock(base_dir=str(tmp_path))
    _, bound, structured = setup_mock_llm(
        mock_get_llm, structured_response={"achieved": True, "evidence": "e"}
    )
    bound.ainvoke.side_effect = [
        AIMessage(
            content="", tool_calls=[_tool_call("save_note", "c1", key="output", content="REPORT")]
        ),
        AIMessage(content="Order placed."),
    ]

    await outputter(
        ctx=mock_context,
        output_config=OutputConfig(structured_output=_Verdict),
        graph_output=mock_state,
        plan_and_history="Step 1",
    )

    format_messages = structured.ainvoke.call_args.args[0]
    assert "REPORT" in format_messages[1].content
    assert "Order placed." in format_messages[-1].content
    assert mock_state.initial_goal in format_messages[-1].content


@patch("artemis.agents.outputter.outputter.get_save_note_tool_pure")
@patch("artemis.agents.outputter.outputter.get_history_tools")
@patch("artemis.agents.outputter.outputter.get_llm")
@pytest.mark.asyncio
async def test_outputter_runs_reads_concurrently_and_writes_in_order(
    mock_get_llm, mock_history_tools, mock_get_save_note, mock_context, mock_state
):
    from langchain_core.messages import AIMessage, ToolMessage

    events: list[str] = []
    barrier = asyncio.Barrier(2)  # only passable if both reads are in flight together
    mock_history_tools.return_value = (
        _recording_tool("search_history", "hit", events, barrier),
        _recording_tool("replay_steps", "replay", events, barrier),
        _recording_tool("get_step_screenshot", "no image", events),
    )
    mock_get_save_note.return_value = _recording_tool("save_note", "saved", events)
    _, bound, _ = setup_mock_llm(mock_get_llm)
    calls = [
        _tool_call("search_history", "a"),
        _tool_call("replay_steps", "b"),
        _tool_call("save_note", "c", key="k", content="v"),
        _tool_call("get_step_screenshot", "d"),
    ]
    bound.ainvoke.side_effect = [AIMessage(content="", tool_calls=calls), AIMessage("ok")]

    await outputter(ctx=mock_context, output_config=OutputConfig(), graph_output=mock_state)

    save_start = events.index("start:save_note")
    assert {"end:search_history", "end:replay_steps"} <= set(events[:save_start])
    assert events.index("end:save_note") < events.index("start:get_step_screenshot")
    sent = bound.ainvoke.call_args_list[1].args[0]
    assert [m.tool_call_id for m in sent if isinstance(m, ToolMessage)] == ["a", "b", "c", "d"]


@patch("artemis.agents.outputter.outputter.get_history_tools")
@patch("artemis.agents.outputter.outputter.get_llm")
@pytest.mark.asyncio
async def test_outputter_puts_image_messages_after_all_tool_results(
    mock_get_llm, mock_history_tools, mock_context, mock_state
):
    """Providers that carry images in a HumanMessage need tool results contiguous."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    shot = [
        {"type": "text", "text": "Step 3 screenshot"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
    ]
    events: list[str] = []
    mock_history_tools.return_value = (
        _recording_tool("search_history", "hit at step 3", events),
        _recording_tool("replay_steps", "replay", events),
        _recording_tool("get_step_screenshot", shot, events),
    )
    mock_llm, bound, _ = setup_mock_llm(mock_get_llm)
    mock_llm.endpoint.provider = "openai"
    turn = AIMessage(
        content="",
        tool_calls=[_tool_call("get_step_screenshot", "a"), _tool_call("search_history", "b")],
    )
    bound.ainvoke.side_effect = [turn, AIMessage("done")]

    await outputter(ctx=mock_context, output_config=OutputConfig(), graph_output=mock_state)

    sent = bound.ainvoke.call_args_list[1].args[0]
    after = sent[sent.index(turn) + 1 : -1]
    assert [type(m) for m in after] == [ToolMessage, ToolMessage, HumanMessage]
    assert [m.tool_call_id for m in after[:2]] == ["a", "b"]


_DEGRADE_CASES = [
    pytest.param(
        '{"achieved": true, "evidence": "Order placed"}',
        {"achieved": True, "evidence": "Order placed"},
        id="parsed_locally",
    ),
    pytest.param("The order was placed.", "The order was placed.", id="text_answer"),
]


@patch("artemis.agents.outputter.outputter.get_llm")
@pytest.mark.asyncio
@pytest.mark.parametrize("answer, expected", _DEGRADE_CASES)
async def test_outputter_formatting_failure_degrades(
    mock_get_llm, answer, expected, mock_context, mock_state
):
    _, _, structured = setup_mock_llm(mock_get_llm, react_response_content=answer)
    structured.ainvoke.side_effect = RuntimeError("schema rejected")

    result = await outputter(
        ctx=mock_context,
        output_config=OutputConfig(structured_output=_Verdict),
        graph_output=mock_state,
    )

    assert result == expected


@patch("artemis.agents.outputter.outputter.get_llm")
@pytest.mark.asyncio
async def test_outputter_shows_target_schema_while_investigating(
    mock_get_llm, mock_context, mock_state
):
    _, bound, _ = setup_mock_llm(
        mock_get_llm, structured_response={"achieved": True, "evidence": "e"}
    )

    await outputter(
        ctx=mock_context,
        output_config=OutputConfig(structured_output=_Verdict),
        graph_output=mock_state,
    )

    prompt = bound.ainvoke.call_args.args[0][1].content[0]["text"]
    assert "## Target Output Fields" in prompt
    assert '"achieved"' in prompt and '"evidence"' in prompt
