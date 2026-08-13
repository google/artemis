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
from adbutils import AdbClient
from langchain_core.messages import AIMessage, ToolMessage
from artemis.agents.diagnoser.diagnoser import Diagnoser
from artemis.context import ArtemisContext
from artemis.graph.state import State
import pytest


@pytest.fixture(scope="session", autouse=True)
def ensure_device_connected():
    try:
        client = AdbClient()
        if not client.device_list():
            pytest.skip("No active Android device/emulator found. Skipping diagnostic tool tests.")
    except Exception as e:
        pytest.skip(f"ADB Client connection failed: {e}. Skipping tests.")


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
@patch("artemis.tools.video_tool.get_video_analyzer_tool")
@patch("artemis.tools.log_tool.get_analyze_logs_tool")
@patch("artemis.agents.diagnoser.diagnoser.get_ui_hierarchy_tool")
@pytest.mark.asyncio
async def test_maestro_uninstall_timeout(
    mock_get_ui_tool,
    mock_get_log_tool,
    mock_get_video_tool,
    mock_get_llm,
    mock_context,
    mock_state,
):
    """Test Maestro Uninstall Timeout / Blocker Failure."""
    # Mock LLM calls
    mock_llm = Mock()
    mock_llm.bind_tools.return_value = mock_llm

    mock_response_1 = AIMessage(
        content="I need to check the UI hierarchy.",
        tool_calls=[
            {
                "name": "get_ui_hierarchy",
                "args": {},
                "id": "call_hierarchy",
            }
        ],
    )
    mock_response_2 = AIMessage(
        content=(
            "Diagnoser identifies the blocker failure and recommends restarting"
            " ADB/uninstalling conflicting tools manually."
        )
    )

    responses = [mock_response_1, mock_response_2]
    call_count = 0

    async def mock_astream(*args, **kwargs):
        nonlocal call_count
        if call_count < len(responses):
            yield responses[call_count]
            call_count += 1

    mock_llm.astream.side_effect = mock_astream
    mock_get_llm.return_value = mock_llm

    # Mock tool
    mock_ui_tool = AsyncMock()
    mock_ui_tool.name = "get_ui_hierarchy"
    mock_ui_tool.args = {}
    mock_ui_tool.ainvoke.return_value = "Error: pm list packages timed out"
    mock_get_ui_tool.return_value = mock_ui_tool

    # Run agent
    agent = Diagnoser(mock_context)
    result = await agent.run("Diagnose why the app crashed", mock_state)

    assert "recommends restarting ADB" in result
    assert "uninstalling conflicting tools" in result

    # Assert get_ui_hierarchy was called
    mock_ui_tool.ainvoke.assert_called_once()

    # Assert that the ToolMessage was sent back to LLM
    assert len(mock_llm.astream.call_args_list) == 2
    sent_messages = mock_llm.astream.call_args_list[1][0][0]
    tool_message = sent_messages[-2]
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.tool_call_id == "call_hierarchy"
    assert tool_message.content == "Error: pm list packages timed out"
    assert tool_message.status == "error"


@patch("artemis.agents.diagnoser.diagnoser.get_llm")
@patch("artemis.tools.video_tool.get_video_analyzer_tool")
@patch("artemis.tools.log_tool.get_analyze_logs_tool")
@patch("artemis.agents.diagnoser.diagnoser.get_ui_hierarchy_tool")
@pytest.mark.asyncio
async def test_uiautomator2_connection_lost(
    mock_get_ui_tool,
    mock_get_log_tool,
    mock_get_video_tool,
    mock_get_llm,
    mock_context,
    mock_state,
):
    """Test UIAutomator2 Daemon Connection Lost."""
    # Mock LLM
    mock_llm = Mock()
    mock_llm.bind_tools.return_value = mock_llm

    mock_response_1 = AIMessage(
        content="Let me check the UI hierarchy.",
        tool_calls=[
            {
                "name": "get_ui_hierarchy",
                "args": {},
                "id": "call_hierarchy_lost",
            }
        ],
    )
    mock_response_2 = AIMessage(content="Diagnoser reports connection loss with UIAutomator2.")

    responses = [mock_response_1, mock_response_2]
    call_count = 0

    async def mock_astream(*args, **kwargs):
        nonlocal call_count
        if call_count < len(responses):
            yield responses[call_count]
            call_count += 1

    mock_llm.astream.side_effect = mock_astream
    mock_get_llm.return_value = mock_llm

    # Mock tool to throw ConnectionError
    mock_ui_tool = AsyncMock()
    mock_ui_tool.name = "get_ui_hierarchy"
    mock_ui_tool.args = {}
    mock_ui_tool.ainvoke.side_effect = ConnectionError(
        "UIAutomator2 connection lost, reconnecting..."
    )
    mock_get_ui_tool.return_value = mock_ui_tool

    # Run agent
    agent = Diagnoser(mock_context)
    result = await agent.run("Diagnose crash", mock_state)

    assert "reports connection loss" in result

    # Assert that tool error is wrapped and returned in the ToolMessage
    assert len(mock_llm.astream.call_args_list) == 2
    sent_messages = mock_llm.astream.call_args_list[1][0][0]
    tool_message = sent_messages[-2]
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.tool_call_id == "call_hierarchy_lost"
    assert "UIAutomator2 connection lost, reconnecting..." in tool_message.content
    assert tool_message.status == "error"

    # Verify that the trace span recorded the failure
    record_trace_calls = mock_context.data_engine.record_trace.call_args_list
    tool_trace_calls = [c for c in record_trace_calls if c[1].get("name") == "get_ui_hierarchy"]
    failed_trace = next((c for c in tool_trace_calls if c[1].get("status") == "failed"), None)
    assert failed_trace is not None
    assert "UIAutomator2 connection lost, reconnecting..." in failed_trace[1]["payload"]["error"]


@patch("artemis.agents.diagnoser.diagnoser.get_llm")
@patch("artemis.tools.video_tool.get_video_analyzer_tool")
@patch("artemis.tools.log_tool.get_analyze_logs_tool")
@patch("artemis.agents.diagnoser.diagnoser.get_ui_hierarchy_tool")
@pytest.mark.asyncio
async def test_ui_hierarchy_dump_stall(
    mock_get_ui_tool,
    mock_get_log_tool,
    mock_get_video_tool,
    mock_get_llm,
    mock_context,
    mock_state,
):
    """Test UI Hierarchy Dump Stall (UI Thread Frozen)."""
    # Mock LLM
    mock_llm = Mock()
    mock_llm.bind_tools.return_value = mock_llm

    mock_response_1 = AIMessage(
        content="Let me fetch the UI hierarchy to see what's on screen.",
        tool_calls=[
            {
                "name": "get_ui_hierarchy",
                "args": {},
                "id": "call_hierarchy_stall",
            }
        ],
    )
    mock_response_2 = AIMessage(
        content=(
            "Diagnoser identifies frozen UI and suggests rendering-safe"
            " recovery actions (like ADB wake commands or a wait/retry cycle)."
        )
    )

    responses = [mock_response_1, mock_response_2]
    call_count = 0

    async def mock_astream(*args, **kwargs):
        nonlocal call_count
        if call_count < len(responses):
            yield responses[call_count]
            call_count += 1

    mock_llm.astream.side_effect = mock_astream
    mock_get_llm.return_value = mock_llm

    # Mock tool
    mock_ui_tool = AsyncMock()
    mock_ui_tool.name = "get_ui_hierarchy"
    mock_ui_tool.args = {}
    mock_ui_tool.ainvoke.return_value = (
        "Error retrieving UI hierarchy: dump_hierarchy timed out after 3 seconds"
    )
    mock_get_ui_tool.return_value = mock_ui_tool

    # Run agent
    agent = Diagnoser(mock_context)
    result = await agent.run("Diagnose stall", mock_state)

    assert "rendering-safe recovery actions" in result

    # Assert ToolMessage was passed back with correct error details
    assert len(mock_llm.astream.call_args_list) == 2
    sent_messages = mock_llm.astream.call_args_list[1][0][0]
    tool_message = sent_messages[-2]
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.tool_call_id == "call_hierarchy_stall"
    assert "dump_hierarchy timed out after 3 seconds" in tool_message.content
    assert tool_message.status == "error"


@patch("artemis.agents.diagnoser.diagnoser.get_llm")
@patch("artemis.tools.video_tool.get_video_analyzer_tool")
@patch("artemis.tools.log_tool.get_analyze_logs_tool")
@patch("artemis.agents.diagnoser.diagnoser.get_ui_hierarchy_tool")
@pytest.mark.asyncio
async def test_device_offline(
    mock_get_ui_tool,
    mock_get_log_tool,
    mock_get_video_tool,
    mock_get_llm,
    mock_context,
    mock_state,
):
    """Test Device Offline (ADB Transport Lost)."""
    # Mock LLM
    mock_llm = Mock()
    mock_llm.bind_tools.return_value = mock_llm

    mock_response_1 = AIMessage(
        content="Let's grab the UI hierarchy.",
        tool_calls=[
            {
                "name": "get_ui_hierarchy",
                "args": {},
                "id": "call_hierarchy_offline",
            }
        ],
    )
    mock_response_2 = AIMessage(
        content=("The device is offline. Diagnoser flags the offline device state.")
    )

    responses = [mock_response_1, mock_response_2]
    call_count = 0

    async def mock_astream(*args, **kwargs):
        nonlocal call_count
        if call_count < len(responses):
            yield responses[call_count]
            call_count += 1

    mock_llm.astream.side_effect = mock_astream
    mock_get_llm.return_value = mock_llm

    # Mock tool
    mock_ui_tool = AsyncMock()
    mock_ui_tool.name = "get_ui_hierarchy"
    mock_ui_tool.args = {}
    mock_ui_tool.ainvoke.side_effect = RuntimeError("device offline")
    mock_get_ui_tool.return_value = mock_ui_tool

    # Run agent
    agent = Diagnoser(mock_context)
    result = await agent.run("Diagnose offline", mock_state)

    assert "flags the offline device state" in result

    # Assert ToolMessage was passed back with correct error details
    assert len(mock_llm.astream.call_args_list) == 2
    sent_messages = mock_llm.astream.call_args_list[1][0][0]
    tool_message = sent_messages[-2]
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.tool_call_id == "call_hierarchy_offline"
    assert "device offline" in tool_message.content
    assert tool_message.status == "error"
