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

from unittest.mock import AsyncMock, Mock, patch
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command
from artemis.agents.diagnoser.diagnoser import Diagnoser
from artemis.constants import VALIDATOR_MESSAGES_KEY
from artemis.context import ArtemisContext
from artemis.graph.state import State
import pytest


@pytest.fixture
def mock_context():
    ctx = Mock(spec=ArtemisContext)
    ctx.llm_config = Mock()
    ctx.data_engine = Mock()
    ctx.data_engine.base_dir = "/tmp"
    ctx.data_engine.get_agent_friendly_steps.return_value = []

    # Mock execution_setup
    mock_setup = Mock()
    mock_setup.video_recording_tools_enabled = True
    ctx.execution_setup = mock_setup

    return ctx


@pytest.fixture
def mock_state():
    state = Mock(spec=State)
    state.initial_goal = "Test goal"
    state.latest_screenshot = None
    state.latest_ui_hierarchy = []
    return state


@patch("artemis.agents.diagnoser.diagnoser.get_llm")
@patch("artemis.agents.diagnoser.diagnoser.get_video_analyzer_tool")
@patch("artemis.agents.diagnoser.diagnoser.get_analyze_logs_tool")
@pytest.mark.asyncio
async def test_diagnostic_agent_simple_call(
    mock_get_log_tool,
    mock_get_video_tool,
    mock_get_llm,
    mock_context,
    mock_state,
):
    """Test that DiagnosticAgent calls LLM and returns result."""
    mock_llm = Mock()
    mock_llm.bind_tools.return_value = mock_llm

    mock_response = AIMessage(content="I diagnosed the issue: it is a network error.")

    async def mock_astream(*args, **kwargs):
        yield mock_response

    mock_llm.astream.side_effect = mock_astream
    mock_get_llm.return_value = mock_llm

    # Mock tools
    mock_video_tool = Mock()
    mock_video_tool.name = "video_analyzer"
    mock_get_video_tool.return_value = mock_video_tool

    mock_log_tool = Mock()
    mock_log_tool.name = "analyze_logs"
    mock_get_log_tool.return_value = mock_log_tool

    agent = Diagnoser(mock_context)
    result = await agent.run("Diagnose why the app crashed", mock_state)

    assert result == "I diagnosed the issue: it is a network error."


@patch("artemis.agents.diagnoser.diagnoser.get_llm")
@patch("artemis.agents.diagnoser.diagnoser.get_video_analyzer_tool")
@patch("artemis.agents.diagnoser.diagnoser.get_analyze_logs_tool")
@pytest.mark.asyncio
async def test_diagnostic_agent_tool_calls(
    mock_get_log_tool,
    mock_get_video_tool,
    mock_get_llm,
    mock_context,
    mock_state,
):
    """Test that DiagnosticAgent handles tool calls."""
    mock_llm = Mock()
    mock_llm.bind_tools.return_value = mock_llm

    # Simulate response with tool calls
    mock_response = AIMessage(
        content="I need to check logs and video.",
        tool_calls=[
            {
                "name": "analyze_logs",
                "args": {"specific_query": "Check for crashes"},
                "id": "call_1",
            },
            {
                "name": "video_analyzer",
                "args": {
                    "time_description": "around 10s",
                    "purpose": "Check UI",
                },
                "id": "call_2",
            },
        ],
    )
    # Second call returns final answer
    mock_final_response = AIMessage(content="Diagnosis complete.")

    responses = [mock_response, mock_final_response]
    call_count = 0

    async def mock_astream(*args, **kwargs):
        nonlocal call_count
        if call_count < len(responses):
            yield responses[call_count]
            call_count += 1

    mock_llm.astream.side_effect = mock_astream
    mock_get_llm.return_value = mock_llm

    # Mock tools
    mock_log_tool = AsyncMock()
    mock_log_tool.name = "analyze_logs"
    mock_log_tool.args = {"specific_query": None, "state": None}
    mock_log_tool.ainvoke.return_value = "Logs show no crash."
    mock_get_log_tool.return_value = mock_log_tool

    mock_video_tool = AsyncMock()
    mock_video_tool.name = "video_analyzer"
    mock_video_tool.args = {
        "time_description": None,
        "purpose": None,
        "state": None,
    }
    mock_command = Command(
        update={
            VALIDATOR_MESSAGES_KEY: [
                ToolMessage(content="Video shows UI loading.", tool_call_id="call_2")
            ]
        }
    )
    mock_video_tool.ainvoke.return_value = mock_command
    mock_get_video_tool.return_value = mock_video_tool

    agent = Diagnoser(mock_context)
    result = await agent.run("Diagnose issue", mock_state)

    assert result == "Diagnosis complete."
    mock_log_tool.ainvoke.assert_called_once_with(
        {"specific_query": "Check for crashes", "state": mock_state}
    )
    mock_video_tool.ainvoke.assert_called_once()


@patch("artemis.agents.diagnoser.diagnoser.get_llm")
@patch("artemis.agents.diagnoser.diagnoser.get_video_analyzer_tool")
@patch("artemis.agents.diagnoser.diagnoser.get_analyze_logs_tool")
@pytest.mark.asyncio
async def test_diagnostic_agent_no_video_tool_when_disabled(
    mock_get_log_tool,
    mock_get_video_tool,
    mock_get_llm,
    mock_context,
    mock_state,
):
    """Test that Diagnoser does not bind video tool when disabled."""
    # Set video recording to False
    mock_context.execution_setup.video_recording_tools_enabled = False

    mock_llm = Mock()
    mock_llm.bind_tools.return_value = mock_llm

    mock_response = AIMessage(content="Diagnosis complete.")

    async def mock_astream(*args, **kwargs):
        yield mock_response

    mock_llm.astream.side_effect = mock_astream
    mock_get_llm.return_value = mock_llm

    # Mock tools
    mock_video_tool = Mock()
    mock_video_tool.name = "video_analyzer"
    mock_get_video_tool.return_value = mock_video_tool

    mock_log_tool = Mock()
    mock_log_tool.name = "analyze_logs"
    mock_get_log_tool.return_value = mock_log_tool

    agent = Diagnoser(mock_context)
    await agent.run("Diagnose issue", mock_state)

    # Verify bind_tools was called
    mock_llm.bind_tools.assert_called_once()
    bound_tools = mock_llm.bind_tools.call_args[1]["tools"]

    # Verify video_analyzer is NOT in bound tools
    tool_names = [t.name for t in bound_tools if hasattr(t, "name")]
    assert "video_analyzer" not in tool_names
    # Verify log tool IS in bound tools (should still be there)
    assert "analyze_logs" in tool_names


@patch("artemis.agents.diagnoser.diagnoser.get_llm")
@patch("artemis.tools.video_tool.VideoAnalyzer")
@pytest.mark.asyncio
async def test_diagnoser_background_job_lifecycle(
    mock_video_analyzer_class, mock_get_llm, mock_state
):
    # Setup context
    ctx = ArtemisContext.model_construct(
        device=Mock(),
        llm_config=Mock(),
        execution_setup=Mock(video_recording_tools_enabled=True),
    )
    ctx.data_engine = None

    # The Mock VideoAnalyzer agent running inside the background job
    mock_agent_instance = AsyncMock()

    async def mock_agent_run(time_desc, purpose):
        return "Video shows step successfully completed.", "success"

    mock_agent_instance.run.side_effect = mock_agent_run
    mock_video_analyzer_class.return_value = mock_agent_instance

    # Prepare LLM responses
    mock_llm = Mock()
    mock_llm.bind_tools.return_value = mock_llm

    # Turn 1: LLM requests tool call
    mock_response_1 = AIMessage(
        content="Checking the video first.",
        tool_calls=[
            {
                "name": "video_analyzer",
                "args": {
                    "time_description": "from 0s to 5s",
                    "purpose": "check UI",
                },
                "id": "call_video_1",
            }
        ],
    )
    # Turn 2: LLM gets the initial job start message, continues, then background job finishes, and result is injected.
    mock_response_2 = AIMessage(content="Final complete diagnosis.")

    responses = [mock_response_1, mock_response_2]
    call_count = 0

    async def mock_astream(*args, **kwargs):
        nonlocal call_count
        if call_count < len(responses):
            yield responses[call_count]
            call_count += 1

    mock_llm.astream.side_effect = mock_astream
    mock_get_llm.return_value = mock_llm

    # Mock asanitize_update on mock_state
    async def mock_asanitize_update(ctx, update, agent):
        return update

    mock_state.asanitize_update = mock_asanitize_update

    # Instantiate the Diagnoser
    agent = Diagnoser(ctx)

    # We want to use the actual video analyzer tool so it triggers the background task
    from artemis.tools.video_tool import get_video_analyzer_tool

    real_video_tool = get_video_analyzer_tool(ctx, role="diagnoser")

    with patch(
        "artemis.tools.log_tool.get_analyze_logs_tool",
        return_value=Mock(name="analyze_logs"),
    ):
        with patch(
            "artemis.tools.video_tool.get_video_analyzer_tool",
            return_value=real_video_tool,
        ):
            result = await agent.run("Diagnose video issue", mock_state)

    assert result == "Final complete diagnosis."
    # Verify job registry
    assert len(ctx.background_jobs) == 1
    job_id = list(ctx.background_jobs.keys())[0]
    job = ctx.background_jobs[job_id]
    assert job["tool_call_id"] == "call_video_1"
    assert job["status"] == "completed"
    assert job["result"] == "Video shows step successfully completed."
    assert job["consumed"] is True
