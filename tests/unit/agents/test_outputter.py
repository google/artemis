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

from pydantic import BaseModel
import pytest

from artemis.agents.outputter.outputter import outputter
from artemis.config import LLM, OutputConfig  # noqa: E402
from artemis.context import ArtemisContext  # noqa: E402
from artemis.utils.logger import get_logger  # noqa: E402

logger = get_logger(__name__)


class _CyFunctionDetectorMeta(type):
    def __instancecheck__(self, instance):
        name = type(instance).__name__
        return (
            name
            in (
                "cyfunction",
                "cython_function_or_method",
                "builtin_function_or_method",
            )
            or "cyfunction" in name.lower()
        )


class CyFunctionDetector(metaclass=_CyFunctionDetectorMeta):
    pass


class MockPydanticSchema(BaseModel):
    model_config = {"ignored_types": (CyFunctionDetector,)}
    color: str
    price: float
    currency_symbol: str
    website_url: str


mock_dict = {
    "color": "green",
    "price": 20,
    "currency_symbol": "$",
    "website_url": "http://superwebsite.fr",
}


class DummyState:
    def __init__(self, messages, initial_goal, operator_raw_data=None):
        self.messages = messages
        self.initial_goal = initial_goal
        self.operator_raw_data = operator_raw_data


@pytest.fixture
def mock_context():
    """Create a properly mocked context with all required fields."""
    ctx = Mock(spec=ArtemisContext)
    ctx.llm_config = {
        "planner": LLM(provider="openai", model="gpt-5-nano"),
        "operator": LLM(provider="openai", model="gpt-5-nano"),
        "validator": LLM(provider="openai", model="gpt-5-nano"),
    }
    ctx.device = Mock()
    ctx.data_engine = None
    return ctx


@pytest.fixture
def mock_state():
    """Create a mock state with test data."""
    return DummyState(
        messages=[],
        initial_goal="Find a green product on my website",
    )


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


@patch("artemis.agents.outputter.outputter.get_llm")
@pytest.mark.asyncio
async def test_outputter_with_pydantic_model(mock_get_llm, mock_context, mock_state):
    """Test outputter with Pydantic model output."""
    expected_structured = MockPydanticSchema(
        color="green",
        price=20,
        currency_symbol="$",
        website_url="http://superwebsite.fr",
    )
    setup_mock_llm(
        mock_get_llm,
        react_response_content="Raw details about green product",
        structured_response=expected_structured,
    )

    config = OutputConfig(
        structured_output=MockPydanticSchema,
        output_description=None,
    )

    result = await outputter(ctx=mock_context, output_config=config, graph_output=mock_state)

    assert isinstance(result, dict)
    assert result.get("color") == "green"


@patch("artemis.agents.outputter.outputter.get_llm")
@pytest.mark.asyncio
async def test_outputter_with_dict(mock_get_llm, mock_context, mock_state):
    """Test outputter with dictionary output."""
    expected_dict = {
        "color": "green",
        "price": 20,
        "currency_symbol": "$",
        "website_url": "http://superwebsite.fr",
    }
    setup_mock_llm(
        mock_get_llm,
        react_response_content="Raw details",
        structured_response=expected_dict,
    )

    config = OutputConfig(
        structured_output=mock_dict,
        output_description=None,
    )

    result = await outputter(ctx=mock_context, output_config=config, graph_output=mock_state)

    assert isinstance(result, dict)
    assert result.get("color") == "green"
    assert result.get("price") == 20
    assert result.get("currency_symbol") == "$"
    assert result.get("website_url") == "http://superwebsite.fr"


@patch("artemis.agents.outputter.outputter.get_llm")
@pytest.mark.asyncio
async def test_outputter_with_natural_language_output(mock_get_llm, mock_context, mock_state):
    """Test outputter with natural language description output (returns JSON string)."""
    expected_json = (
        '{"color": "green", "price": 20, "currency_symbol": "$", "website_url":'
        ' "http://superwebsite.fr"}'
    )
    setup_mock_llm(mock_get_llm, react_response_content=expected_json)

    config = OutputConfig(
        structured_output=None,
        output_description=(
            "A JSON object with a color, a price, a currency_symbol and a website_url key"
        ),
    )

    result = await outputter(ctx=mock_context, output_config=config, graph_output=mock_state)

    assert isinstance(result, dict)
    assert result.get("color") == "green"
    assert result.get("price") == 20
    assert result.get("currency_symbol") == "$"
    assert result.get("website_url") == "http://superwebsite.fr"


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
    assert human_content[1]["image_url"]["url"] == "data:image/jpeg;base64,mock_b64_data"


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
