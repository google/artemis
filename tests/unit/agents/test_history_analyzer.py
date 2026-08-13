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

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from langchain_core.messages import ToolMessage
from artemis.agents.history_analyzer.history_analyzer import HistoryAnalyzer
from artemis.context import ArtemisContext
import pytest


@pytest.mark.asyncio
async def test_history_analyzer_no_history():
    # Mock context with a data engine that has no steps
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = MagicMock()
    mock_ctx.data_engine.get_agent_friendly_steps.return_value = []

    analyzer = HistoryAnalyzer(mock_ctx)
    result = await analyzer.run("What happened?")
    assert result == "No history recorded for this session yet."


@pytest.mark.asyncio
async def test_history_analyzer_simple_query_no_tool_call():
    # Mock context and data engine
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = MagicMock()
    mock_ctx.data_engine.base_dir = "/tmp/fake_traces"

    steps = [
        {
            "step_number": 1,
            "relative_time": "1.2s",
            "summary": "Opened the settings app",
        },
        {
            "step_number": 2,
            "relative_time": "4.5s",
            "summary": "Toggled the wifi switch",
        },
    ]
    mock_ctx.data_engine.get_agent_friendly_steps.return_value = steps

    # Mock LLM to return a direct text response (no tool calls)
    mock_response = MagicMock()
    mock_response.content = "First you opened settings, then you toggled wifi."
    mock_response.tool_calls = []

    mock_llm = MagicMock()
    mock_llm.bind_tools.return_value = mock_llm

    async def mock_astream(*args, **kwargs):
        yield mock_response

    mock_llm.astream.side_effect = mock_astream

    with (
        patch(
            "artemis.agents.history_analyzer.history_analyzer.get_llm",
            return_value=mock_llm,
        ),
        patch("pathlib.Path.exists", return_value=False),  # Force system prompt fallback
    ):
        analyzer = HistoryAnalyzer(mock_ctx)
        result = await analyzer.run("Summarize the session")

        assert result == "First you opened settings, then you toggled wifi."
        assert mock_llm.astream.called


@pytest.mark.asyncio
async def test_history_analyzer_detailed_query_with_tool_call():
    # Mock context and data engine with full details
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = MagicMock()
    mock_ctx.data_engine.base_dir = "/tmp/fake_traces"

    steps = [
        {
            "step_number": 1,
            "relative_time": "1.2s",
            "summary": "Opened the settings app",
            "action_taken": {"action": "tap", "coordinates": [500, 600]},
            "operator_raw_thinking": "Need to open settings to configure wifi",
            "last_execution_result": {"status": "success"},
        },
        {
            "step_number": 2,
            "relative_time": "4.5s",
            "summary": "Toggled the wifi switch",
            "action_taken": {"action": "tap", "coordinates": [100, 200]},
            "operator_raw_thinking": "Toggle the switch to turn wifi on",
            "last_execution_result": {"status": "success"},
        },
    ]
    mock_ctx.data_engine.get_agent_friendly_steps.return_value = steps

    # Turn 1 LLM response: Tool call to get_step_details for step 2
    mock_response_turn_1 = MagicMock()
    mock_response_turn_1.content = ""
    mock_tool_call = {
        "name": "get_step_details",
        "args": {"start_step": 2, "end_step": 2},
        "id": "call_123456",
    }
    mock_response_turn_1.tool_calls = [mock_tool_call]

    # Turn 2 LLM response: Final answer using the details
    mock_response_turn_2 = MagicMock()
    mock_response_turn_2.content = (
        "In step 2, the operator was thinking: 'Toggle the switch to turn wifi on'."
    )
    mock_response_turn_2.tool_calls = []

    mock_llm = MagicMock()
    mock_llm.bind_tools.return_value = mock_llm
    responses = [mock_response_turn_1, mock_response_turn_2]
    call_count = 0

    async def mock_astream(*args, **kwargs):
        nonlocal call_count
        if call_count < len(responses):
            yield responses[call_count]
            call_count += 1

    mock_llm.astream.side_effect = mock_astream

    with (
        patch(
            "artemis.agents.history_analyzer.history_analyzer.get_llm",
            return_value=mock_llm,
        ),
        patch("pathlib.Path.exists", return_value=False),  # Force fallback prompt
    ):
        analyzer = HistoryAnalyzer(mock_ctx)
        result = await analyzer.run("What was the operator thinking in step 2?")

        assert (
            result == "In step 2, the operator was thinking: 'Toggle the switch to turn wifi on'."
        )
        assert mock_llm.astream.call_count == 2

        # Verify the tool was invoked with correct parameters internally
        tool_msg = mock_llm.astream.call_args_list[1][0][0][
            -2
        ]  # ToolMessage is second to last after final response is appended
        assert isinstance(tool_msg, ToolMessage)
        assert tool_msg.tool_call_id == "call_123456"

        # Parse the JSON in the ToolMessage content
        tool_result = json.loads(tool_msg.content)
        assert len(tool_result) == 1
        assert tool_result[0]["step_number"] == 2
        assert tool_result[0]["operator_raw_thinking"] == "Toggle the switch to turn wifi on"
        assert tool_result[0]["action_taken"] == {
            "action": "tap",
            "coordinates": [100, 200],
        }


@pytest.mark.asyncio
async def test_history_analyzer_read_note_tool_call():
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = MagicMock()
    mock_ctx.data_engine.base_dir = "/tmp/fake_traces"

    steps = [{"step_number": 1, "relative_time": "1.0s", "summary": "Initiated"}]
    mock_ctx.data_engine.get_agent_friendly_steps.return_value = steps

    # Turn 1 LLM response: Tool call to list_notes
    mock_response_turn_1 = MagicMock()
    mock_response_turn_1.content = ""
    mock_tool_call_list = {"name": "list_notes", "args": {}, "id": "call_list"}
    mock_response_turn_1.tool_calls = [mock_tool_call_list]

    # Turn 2 LLM response: Tool call to read_note for tactical_plan
    mock_response_turn_2 = MagicMock()
    mock_response_turn_2.content = ""
    mock_tool_call_read = {
        "name": "read_note",
        "args": {"key": "tactical_plan"},
        "id": "call_read",
    }
    mock_response_turn_2.tool_calls = [mock_tool_call_read]

    # Turn 3 LLM response: Final answer
    mock_response_turn_3 = MagicMock()
    mock_response_turn_3.content = "The plan was to toggle wifi."
    mock_response_turn_3.tool_calls = []

    mock_llm = MagicMock()
    mock_llm.bind_tools.return_value = mock_llm
    responses = [
        mock_response_turn_1,
        mock_response_turn_2,
        mock_response_turn_3,
    ]
    call_count = 0

    async def mock_astream(*args, **kwargs):
        nonlocal call_count
        if call_count < len(responses):
            yield responses[call_count]
            call_count += 1

    mock_llm.astream.side_effect = mock_astream

    # Mock the file system read using patch
    fake_plan_content = "Goal: Toggle wifi"

    def mock_exists(path):
        # Let Path("/tmp/fake_traces/notes").exists() and notes/tactical_plan.md exist
        return True

    def mock_read_text(self, encoding="utf-8"):
        return fake_plan_content

    def mock_glob(self, pattern):
        return [Path("/tmp/fake_traces/notes/tactical_plan.md")]

    with (
        patch(
            "artemis.agents.history_analyzer.history_analyzer.get_llm",
            return_value=mock_llm,
        ),
        patch("pathlib.Path.exists", mock_exists),
        patch("pathlib.Path.read_text", mock_read_text),
        patch("pathlib.Path.glob", mock_glob),
    ):
        analyzer = HistoryAnalyzer(mock_ctx)
        result = await analyzer.run("What is the saved tactical plan?")

        assert result == "The plan was to toggle wifi."
        assert mock_llm.astream.call_count == 3

        # Verify list_notes response
        list_msg = mock_llm.astream.call_args_list[1][0][0][
            3
        ]  # Index 3 contains the ToolMessage for list_notes
        assert isinstance(list_msg, ToolMessage)
        assert list_msg.tool_call_id == "call_list"
        assert list_msg.content == "Here are all the notes:\n- tactical_plan (1 lines)"

        # Verify read_note response
        read_msg = mock_llm.astream.call_args_list[2][0][0][
            5
        ]  # Index 5 contains the ToolMessage for read_note
        assert isinstance(read_msg, ToolMessage)
        assert read_msg.tool_call_id == "call_read"
        assert (
            read_msg.content == "Successfully read note 'tactical_plan'. 'tactical_plan' note"
            f" content:\n{fake_plan_content}"
        )


def test_history_analyzer_robust_tools_behavior():
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = MagicMock()
    mock_ctx.data_engine.base_dir = "/tmp/fake_traces"

    steps = [
        {
            "step_number": 1,
            "relative_time": "1.0s",
            "summary": "Action 1",
            "action_taken": {"action": "tap"},
        }
    ]

    analyzer = HistoryAnalyzer(mock_ctx)

    # 1. Test get_step_details with string inputs
    details_tool = analyzer._get_step_details_tool(steps)
    result_details = details_tool.invoke({"start_step": "1", "end_step": "1"})
    parsed_result = json.loads(result_details)
    assert len(parsed_result) == 1
    assert parsed_result[0]["step_number"] == 1

    # 2. Test read_note with .md suffix
    from artemis.tools.scratchpad import get_read_note_tool_pure

    read_tool = get_read_note_tool_pure(analyzer.ctx)

    def mock_exists(path):
        return "tactical_plan.md" in str(path)

    with (
        patch("pathlib.Path.exists", mock_exists),
        patch("pathlib.Path.read_text", return_value="Tactical plan content"),
    ):
        result_read = read_tool.invoke({"key": "tactical_plan.md"})
        assert (
            result_read == "Successfully read note 'tactical_plan.md'. 'tactical_plan.md'"
            " note content:\nTactical plan content"
        )


@pytest.mark.asyncio
async def test_history_analyzer_integration_with_task_tree():
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = MagicMock()
    mock_ctx.data_engine.base_dir = "/tmp/fake_traces"

    steps = [
        {
            "step_id": "step_1",
            "step_number": 1,
            "relative_time": "1.0s",
            "summary": "Step 1",
        },
    ]
    mock_ctx.data_engine.get_agent_friendly_steps.return_value = steps

    mock_response = MagicMock()
    mock_response.content = "Answer"
    mock_response.tool_calls = []

    mock_llm = MagicMock()
    mock_llm.bind_tools.return_value = mock_llm

    async def mock_astream(*args, **kwargs):
        yield mock_response

    mock_llm.astream.side_effect = mock_astream

    fake_plan = """- [/] Active Subgoal
- [ ] Pending Subgoal"""

    mock_path = MagicMock()
    mock_path.exists.return_value = True
    mock_path.read_text.return_value = fake_plan

    # Mock build_plan_and_history
    with (
        patch(
            "artemis.agents.history_analyzer.history_analyzer.get_llm",
            return_value=mock_llm,
        ),
        patch(
            "artemis.agents.history_analyzer.history_analyzer.get_note_file_path",
            return_value=mock_path,
        ),
        patch(
            "artemis.agents.history_analyzer.history_analyzer.build_plan_and_history"
        ) as mock_build,
    ):
        mock_build.return_value = "Mocked operation history"
        analyzer = HistoryAnalyzer(mock_ctx)
        await analyzer.run("query")

        # Verify build_plan_and_history call args
        mock_build.assert_called_once()
        args, kwargs = mock_build.call_args
        assert args[0] == fake_plan
        assert args[1] == steps

        import hashlib

        expected_hash = hashlib.md5(b"Active Subgoal").hexdigest()
        assert args[2] == expected_hash

        assert kwargs.get("last_n_detailed") == 1
        assert kwargs.get("min_summaries") == len(steps)
