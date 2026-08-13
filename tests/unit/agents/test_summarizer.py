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
from unittest.mock import MagicMock, Mock, patch
from artemis.agents.summarizer.summarizer import SummarizerNode
from artemis.context import ArtemisContext
import pytest


class DummyState:
    def __init__(
        self,
        structured_decisions=None,
        operator_raw_thinking=None,
        operator_native_thinking=None,
        last_execution_result=None,
        current_step_id=None,
    ):
        self.structured_decisions = structured_decisions
        self.operator_raw_thinking = operator_raw_thinking
        self.operator_native_thinking = operator_native_thinking
        self.last_execution_result = last_execution_result
        self.current_step_id = current_step_id


@pytest.fixture
def mock_context():
    ctx = Mock(spec=ArtemisContext)
    ctx.device = None
    ctx.data_engine = Mock()
    ctx.background_tasks = []
    return ctx


@pytest.mark.asyncio
async def test_summarizer_success(mock_context):
    """Test that SummarizerNode successfully generates summary from raw thinking and execution result."""
    decisions = json.dumps([{"action": "tap", "coordinates": [500, 600]}])
    thinking = "The operator clicked the search button."
    result = {
        "status": "success",
        "executed_actions": [{"action": "tap", "coordinates": [500, 600]}],
    }

    state = DummyState(
        structured_decisions=decisions,
        operator_raw_thinking=thinking,
        last_execution_result=result,
        current_step_id="12345678-1234-5678-1234-567812345678",
    )

    # Mock LLM response
    mock_response = Mock()
    mock_response.content = "Step completed successfully."
    mock_llm = MagicMock()

    async def mock_astream(*args, **kwargs):
        yield mock_response

    mock_llm.astream.side_effect = mock_astream

    with (
        patch(
            "artemis.agents.summarizer.summarizer.get_llm", return_value=mock_llm
        ) as mock_get_llm,
        patch("asyncio.create_task") as mock_create_task,
    ):
        # Run the node call
        node = SummarizerNode(mock_context)
        await node(state)

        # Verify task was registered in context
        assert len(mock_context.background_tasks) == 1
        assert mock_context.background_tasks[0] == mock_create_task.return_value

        # Verify background task was spawned
        assert mock_create_task.called
        background_coroutine = mock_create_task.call_args[0][0]

        # Await the background task synchronously for test verification
        await background_coroutine

    # Verify LLM was invoked with proper structured messages
    assert mock_llm.astream.called
    invoked_messages = mock_llm.astream.call_args[0][0]

    system_msg = invoked_messages[0].content
    human_msg = invoked_messages[1].content

    assert "Summarizer Agent" in system_msg
    assert "Decisions made" in human_msg
    assert "The operator clicked the search button." in human_msg

    # Verify Data Engine updated the step summary
    from uuid import UUID

    mock_context.data_engine.update_step_summary.assert_called_once_with(
        UUID("12345678-1234-5678-1234-567812345678"),
        "Step completed successfully.",
    )


@pytest.mark.asyncio
async def test_summarizer_with_native_thinking(mock_context):
    """Test that SummarizerNode successfully generates summary from both explicit and native thoughts."""
    decisions = json.dumps([{"action": "tap", "coordinates": [500, 600]}])
    raw_thinking = "Explicit monologue."
    native_thinking = "Silent core reasoning."
    result = {"status": "success"}

    state = DummyState(
        structured_decisions=decisions,
        operator_raw_thinking=raw_thinking,
        operator_native_thinking=native_thinking,
        last_execution_result=result,
        current_step_id="12345678-1234-5678-1234-567812345678",
    )

    mock_response = Mock()
    mock_response.content = "Summary result."
    mock_llm = MagicMock()

    async def mock_astream(*args, **kwargs):
        yield mock_response

    mock_llm.astream.side_effect = mock_astream

    with (
        patch(
            "artemis.agents.summarizer.summarizer.get_llm", return_value=mock_llm
        ) as mock_get_llm,
        patch("asyncio.create_task") as mock_create_task,
    ):
        node = SummarizerNode(mock_context)
        await node(state)

        # Verify task was registered in context
        assert len(mock_context.background_tasks) == 1
        assert mock_context.background_tasks[0] == mock_create_task.return_value

        background_coroutine = mock_create_task.call_args[0][0]
        await background_coroutine

    assert mock_llm.astream.called
    invoked_messages = mock_llm.astream.call_args[0][0]
    human_msg = invoked_messages[1].content

    assert "Operator explicit thoughts:" in human_msg
    assert "Explicit monologue." in human_msg
    assert "Operator native thoughts:" in human_msg
    assert "Silent core reasoning." in human_msg


@pytest.mark.asyncio
async def test_summarizer_with_chronological_trace(mock_context):
    """Test that SummarizerNode generates a prompt containing the chronological step trace when available."""
    step_id = "12345678-1234-5678-1234-567812345678"
    state = DummyState(
        current_step_id=step_id,
    )

    mock_step_data = {
        "step_id": step_id,
        "step_number": 1,
        "relative_time": "5.0s",
        "action_taken": [{"action": "click", "target_text": "Search"}],
        "last_execution_result": {"status": "success"},
        "interleaved_events": [
            {"type": "thought", "content": "Let's click search."},
        ],
    }

    mock_context.data_engine.get_agent_friendly_steps.return_value = [mock_step_data]

    # Mock LLM response
    mock_response = Mock()
    mock_response.content = "Summary using chronological trace."
    mock_llm = MagicMock()

    async def mock_astream(*args, **kwargs):
        yield mock_response

    mock_llm.astream.side_effect = mock_astream

    with (
        patch("artemis.agents.summarizer.summarizer.get_llm", return_value=mock_llm),
        patch("asyncio.create_task") as mock_create_task,
    ):
        node = SummarizerNode(mock_context)
        await node(state)

        # Verify task was registered in context
        assert len(mock_context.background_tasks) == 1
        assert mock_context.background_tasks[0] == mock_create_task.return_value

        background_coroutine = mock_create_task.call_args[0][0]
        await background_coroutine

    assert mock_llm.astream.called
    invoked_messages = mock_llm.astream.call_args[0][0]
    human_msg = invoked_messages[1].content

    assert "--- Execution History ---" in human_msg
    assert "Let's click search." in human_msg
