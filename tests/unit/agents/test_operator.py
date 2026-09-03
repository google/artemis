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
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from artemis.agents.operator.operator import OperatorNode
from artemis.config.agent import MemoryTranscriptConfig
from artemis.context import ArtemisContext
import pytest

# These tests exercise the legacy 2-message prompt path. Since M5 the
# transcript path is the default, so flag-off semantics must be requested
# explicitly (never via the config default).
LEGACY_TRANSCRIPT = MemoryTranscriptConfig(enabled=False)


@pytest.mark.asyncio
async def test_operator_node_fast_path():
    # Mock context and state
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.execution_setup = None
    mock_ctx.data_engine = None
    mock_ctx.trace_id = "test-trace"

    mock_state = MagicMock()
    mock_state.subagent_calls = []
    mock_state.initial_goal = "Test goal"

    # Mock controller and data
    mock_controller = MagicMock()
    mock_controller.take_screenshot = AsyncMock(
        return_value="YmFzZTY0X3NjcmVlbnNob3Q="
    )  # "base64_screenshot" in base64

    mock_device_data = MagicMock()
    mock_device_data.elements = [{"text": "Item 1", "bounds": "[0,0][100,100]"}]
    mock_controller.get_screen_data = AsyncMock(return_value=mock_device_data)

    # Mock LLM response
    mock_llm = MagicMock()
    mock_response = MagicMock()
    mock_response.tool_calls = [
        {
            "name": "click",
            "args": {"target": [50, 50], "reasoning": "Test reasoning"},
            "id": "call_123",
        }
    ]

    mock_llm.ainvoke = AsyncMock(return_value=mock_response)
    mock_llm.bind_tools.return_value = mock_llm  # Allow chaining

    # Patch dependencies
    with patch("artemis.agents.operator.operator.get_llm", return_value=mock_llm):
        node = OperatorNode(mock_ctx, transcript_config=LEGACY_TRANSCRIPT)
        node_update = await node(mock_state)

        # Verify state was updated with action
        update_dict = node_update

        assert "structured_decisions" in update_dict
        decisions = json.loads(update_dict["structured_decisions"])
        assert decisions[0]["action"] == "tap"
        assert decisions[0]["coordinates"] == [54, 120]


@pytest.mark.asyncio
async def test_perform_action_validation():
    from artemis.agents.operator.operator import OperatorNode
    from unittest.mock import MagicMock
    from artemis.graph.state import State

    mock_ctx = MagicMock()
    node = OperatorNode(mock_ctx, transcript_config=LEGACY_TRANSCRIPT)

    mock_state = MagicMock(spec=State)
    mock_state.indexed_points = [[200, 300], [400, 500]]
    mock_state.indexed_elements = [
        {
            "center": [200, 300],
            "text": "Item 1",
            "bounds": "[100,100][300,500]",
            "class": "android.widget.Button",
            "resource_id": "btn_1",
        },
        {
            "center": [400, 500],
            "text": "hello",
            "bounds": "[300,400][500,600]",
            "class": "android.widget.EditText",
            "resource_id": "input_1",
        },
    ]

    # Test click with valid target index
    actions, err = node._translate_and_validate_tool(
        {"name": "click", "args": {"target": 1}}, mock_state
    )
    assert err is None
    assert actions == [
        {
            "action": "tap",
            "coordinates": [200, 300],
            "normalized_coordinates": [185, 125],
            "times": 1,
            "delay_ms": 100,
            "target_text": "Item 1",
            "target_bounds": "[100,100][300,500]",
            "target_resource_id": "btn_1",
            "target_class": "android.widget.Button",
            "target_label_source": "index",
        }
    ]

    # Test click with invalid index
    actions, err = node._translate_and_validate_tool(
        {"name": "click", "args": {"target": 5}}, mock_state
    )
    assert "Error" in err

    # Test input_text with clear_exist
    actions, err = node._translate_and_validate_tool(
        {
            "name": "input_text",
            "args": {"text": "hello", "target": 2, "clear_exist": True},
        },
        mock_state,
    )
    assert err is None
    assert len(actions) == 1
    assert actions[0] == {
        "action": "focus_and_input_text",
        "coordinates": [400, 500],
        "normalized_coordinates": [370, 208],
        "text": "hello",
        "clear_before_input": True,
        "target_text": "hello",
        "target_bounds": "[300,400][500,600]",
        "target_resource_id": "input_1",
        "target_class": "android.widget.EditText",
        "target_label_source": "index",
    }

    # Test swipe direction
    actions, err = node._translate_and_validate_tool(
        {"name": "swipe", "args": {"gesture": "up"}}, mock_state
    )
    assert err is None
    assert actions[0]["action"] == "swipe"
    assert len(actions[0]["coordinates"]) == 4
    assert actions[0]["duration"] == 800

    # Test click with float target index (coercion)
    actions, err = node._translate_and_validate_tool(
        {"name": "click", "args": {"target": 2.0}}, mock_state
    )
    assert err is None
    assert actions == [
        {
            "action": "tap",
            "coordinates": [400, 500],
            "normalized_coordinates": [370, 208],
            "times": 1,
            "delay_ms": 100,
            "target_text": "hello",
            "target_bounds": "[300,400][500,600]",
            "target_resource_id": "input_1",
            "target_class": "android.widget.EditText",
            "target_label_source": "index",
        }
    ]

    # Test input_text with missing text argument
    actions, err = node._translate_and_validate_tool(
        {"name": "input_text", "args": {"target": 2}}, mock_state
    )
    assert "Error" in err
    assert "Missing required argument 'text'" in err

    # Test input_text with non-string text argument
    actions, err = node._translate_and_validate_tool(
        {"name": "input_text", "args": {"text": 123, "target": 2}}, mock_state
    )
    assert "Error" in err
    assert "text' must be a string" in err

    # Test swipe with invalid duration
    actions, err = node._translate_and_validate_tool(
        {"name": "swipe", "args": {"gesture": "up", "duration": "fast"}},
        mock_state,
    )
    assert "Error" in err
    assert "duration' must be a number" in err

    # Test swipe with float duration (coercion)
    actions, err = node._translate_and_validate_tool(
        {"name": "swipe", "args": {"gesture": "up", "duration": 500.0}},
        mock_state,
    )
    assert err is None
    assert actions[0]["duration"] == 500

    # Test manage_app with missing app_name
    actions, err = node._translate_and_validate_tool(
        {"name": "manage_app", "args": {"action": "launch"}}, mock_state
    )
    assert "Error" in err
    assert "app_name' must be a valid non-empty string" in err

    # Test manage_app with invalid action
    actions, err = node._translate_and_validate_tool(
        {
            "name": "manage_app",
            "args": {"action": "restart", "app_name": "Settings"},
        },
        mock_state,
    )
    assert "Error" in err
    assert "Unsupported app management action" in err


@pytest.mark.asyncio
async def test_bare_coordinate_click_enriched_by_hit_test():
    """A coordinate click hit tests the pre-action frame for element semantics."""
    from artemis.agents.operator.operator import OperatorNode
    from unittest.mock import MagicMock
    from artemis.graph.state import State

    mock_ctx = MagicMock()
    node = OperatorNode(mock_ctx, transcript_config=LEGACY_TRANSCRIPT)

    mock_state = MagicMock(spec=State)
    mock_state.indexed_elements = [
        {
            "center": [550, 1440],
            "text": "Confirm",
            "bounds": [500, 1400, 600, 1480],
            "class": "android.widget.Button",
            "resource_id": "btn_confirm",
            "is_ocr": False,
        }
    ]

    # Normalized (500, 600) on the default 1080x2400 device -> pixel (540, 1440),
    # which falls inside the "Confirm" button bounds.
    actions, err = node._translate_and_validate_tool(
        {"name": "click", "args": {"target": [500, 600]}}, mock_state
    )
    assert err is None
    assert actions == [
        {
            "action": "tap",
            "coordinates": [540, 1440],
            "normalized_coordinates": [500, 600],
            "times": 1,
            "delay_ms": 100,
            "target_text": "Confirm",
            "target_bounds": [500, 1400, 600, 1480],
            "target_resource_id": "btn_confirm",
            "target_class": "android.widget.Button",
            "target_label_source": "hit_test",
        }
    ]

    # Graceful degradation: no perception data at all -> bare coordinates, "none".
    mock_state.indexed_elements = []
    actions, err = node._translate_and_validate_tool(
        {"name": "click", "args": {"target": [500, 600]}}, mock_state
    )
    assert err is None
    assert actions[0]["target_text"] is None
    assert actions[0]["target_label_source"] == "none"


@pytest.mark.asyncio
async def test_operator_node_multiple_actions():
    from artemis.agents.operator.operator import OperatorNode
    from artemis.context import ArtemisContext
    from unittest.mock import AsyncMock, MagicMock, patch
    import json

    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.execution_setup = None
    mock_ctx.data_engine = None

    mock_state = MagicMock()
    mock_state.subagent_calls = []
    mock_state.initial_goal = "Test goal"

    mock_controller = MagicMock()
    mock_controller.take_screenshot = AsyncMock(return_value="YmFzZTY0X3NjcmVlbnNob3Q=")
    mock_device_data = MagicMock()
    mock_device_data.elements = []
    mock_controller.get_screen_data = AsyncMock(return_value=mock_device_data)

    mock_llm = MagicMock()
    mock_response = MagicMock()
    mock_response.tool_calls = [
        {
            "name": "input_text",
            "args": {"text": "hello", "target": [50, 50]},
            "id": "call_1",
        },
        {"name": "click", "args": {"target": [50, 50]}, "id": "call_2"},
    ]

    mock_llm.ainvoke = AsyncMock(return_value=mock_response)
    mock_llm.bind_tools.return_value = mock_llm

    with patch("artemis.agents.operator.operator.get_llm", return_value=mock_llm):
        node = OperatorNode(mock_ctx, transcript_config=LEGACY_TRANSCRIPT)
        node_update = await node(mock_state)

        update_dict = node_update

        assert "structured_decisions" in update_dict
        decisions = json.loads(update_dict["structured_decisions"])
        assert len(decisions) == 2
        assert decisions[0]["action"] == "focus_and_input_text"
        assert decisions[0]["text"] == "hello"
        assert decisions[0]["coordinates"] == [54, 120]
        assert decisions[0]["clear_before_input"] == True
        assert decisions[1]["action"] == "tap"
        assert decisions[1]["coordinates"] == [54, 120]


@pytest.mark.asyncio
async def test_operator_node_no_record_step():
    from artemis.agents.operator.operator import OperatorNode
    from artemis.context import ArtemisContext
    from unittest.mock import AsyncMock, MagicMock, patch

    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = MagicMock()
    mock_ctx.data_engine.current_session_id = "test-session"

    mock_state = MagicMock()
    mock_state.subagent_calls = []
    mock_state.initial_goal = "Test goal"

    mock_controller = MagicMock()
    mock_controller.take_screenshot = AsyncMock(return_value="YmFzZTY0X3NjcmVlbnNob3Q=")
    mock_device_data = MagicMock()
    mock_device_data.elements = []
    mock_controller.get_screen_data = AsyncMock(return_value=mock_device_data)

    mock_llm = MagicMock()
    mock_response = MagicMock()
    mock_response.tool_calls = []

    mock_llm.ainvoke = AsyncMock(return_value=mock_response)
    mock_llm.bind_tools.return_value = mock_llm

    with patch("artemis.agents.operator.operator.get_llm", return_value=mock_llm):
        node = OperatorNode(mock_ctx, transcript_config=LEGACY_TRANSCRIPT)
        node_update = await node(mock_state)

        mock_ctx.data_engine.record_step.assert_not_called()

        update_dict = node_update
        assert "structured_decisions" in update_dict


@pytest.mark.asyncio
async def test_operator_dynamic_prompt():
    from artemis.agents.operator.operator import OperatorNode, PromptComponent
    from artemis.context import ArtemisContext
    from unittest.mock import AsyncMock, MagicMock, patch

    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = None

    mock_state = MagicMock()
    mock_state.subagent_calls = []
    mock_state.initial_goal = "Test goal"

    class MockComponent(PromptComponent):
        async def __call__(self, builder, state, ctx, **kwargs):
            builder.add_system_text("System part.")
            builder.add_human_content("Human part.")
            builder.set_human_footer("Footer part.")

    mock_controller = MagicMock()
    mock_controller.take_screenshot = AsyncMock(return_value="YmFzZTY0X3NjcmVlbnNob3Q=")
    mock_device_data = MagicMock()
    mock_device_data.elements = []
    mock_controller.get_screen_data = AsyncMock(return_value=mock_device_data)

    mock_llm = MagicMock()
    mock_response = MagicMock()
    mock_response.tool_calls = []

    async def mock_ainvoke(*args, **kwargs):
        messages = args[0]
        assert len(messages) == 2
        assert messages[0].content == "System part."
        assert messages[1].content[0]["text"] == "Human part."
        assert messages[1].content[-1]["text"] == "Footer part."
        return mock_response

    mock_llm.ainvoke.side_effect = mock_ainvoke
    mock_llm.bind_tools.return_value = mock_llm

    with patch("artemis.agents.operator.operator.get_llm", return_value=mock_llm):
        node = OperatorNode(
            mock_ctx, prompt_components=[MockComponent()], transcript_config=LEGACY_TRANSCRIPT
        )
        node_update = await node(mock_state)


@pytest.mark.asyncio
async def test_operator_history_compression():
    from artemis.agents.operator.operator import OperatorNode
    from artemis.context import ArtemisContext
    from unittest.mock import AsyncMock, MagicMock, patch
    import json
    import hashlib

    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = MagicMock()

    subgoal_old_hash = hashlib.md5(b"subgoal_old").hexdigest()
    subgoal_current_hash = hashlib.md5(b"subgoal_current").hexdigest()

    # Create mock steps spanning multiple subgoals
    # We need at least 5 summaries to trigger folding of older subgoals
    mock_steps = [
        {
            "step_id": "step_1",
            "step_number": 1,
            "summary": "Step 1 summary",
            "relative_time": "1.0s",
            "extra_metadata": {"subgoal_hash": subgoal_old_hash},
        },
        {
            "step_id": "step_2",
            "step_number": 2,
            "summary": "Step 2 summary",
            "relative_time": "2.0s",
            "extra_metadata": {"subgoal_hash": subgoal_current_hash},
        },
        {
            "step_id": "step_3",
            "step_number": 3,
            "summary": "Step 3 summary",
            "relative_time": "3.0s",
            "extra_metadata": {"subgoal_hash": subgoal_current_hash},
        },
        {
            "step_id": "step_4",
            "step_number": 4,
            "summary": "Step 4 summary",
            "relative_time": "4.0s",
            "extra_metadata": {"subgoal_hash": subgoal_current_hash},
        },
        {
            "step_id": "step_5",
            "step_number": 5,
            "summary": "Step 5 summary",
            "relative_time": "5.0s",
            "extra_metadata": {"subgoal_hash": subgoal_current_hash},
        },
        {
            "step_id": "step_6",
            "step_number": 6,
            "summary": "Step 6 summary",
            "relative_time": "6.0s",
            "extra_metadata": {"subgoal_hash": subgoal_current_hash},
        },
        {
            "step_id": "step_7",
            "step_number": 7,
            "summary": "Step 7 summary",
            "relative_time": "7.0s",
            "extra_metadata": {"subgoal_hash": subgoal_current_hash},
            "action_taken": {"action": "tap"},
        },
    ]
    mock_ctx.data_engine.get_agent_friendly_steps.return_value = mock_steps

    mock_state = MagicMock()
    mock_state.subagent_calls = []
    mock_state.initial_goal = "Test goal"

    mock_controller = MagicMock()
    mock_controller.take_screenshot = AsyncMock(return_value="YmFzZTY0X3NjcmVlbnNob3Q=")
    mock_device_data = MagicMock()
    mock_device_data.elements = []
    mock_controller.get_screen_data = AsyncMock(return_value=mock_device_data)

    mock_llm = MagicMock()
    mock_response = MagicMock()
    mock_response.tool_calls = []

    # Smarter file reading mock
    def mock_read_text(path_self, encoding=None):
        path_str = str(path_self)
        if "operator.json" in path_str:
            return json.dumps(
                {
                    "main_template": (
                        "Goals: {{ initial_goal }}\nPlan:\n{{ plan_and_history"
                        " }}\n# CURRENT OBSERVATION"
                    ),
                }
            )
        elif "task_plan.md" in path_str:
            return """- [x] subgoal_old
- [/] subgoal_current"""
        return ""

    async def mock_ainvoke(*args, **kwargs):
        messages = args[0]
        full_text = "".join([str(m.content) for m in messages])

        # Check that old subgoal line is present
        assert "- [x] subgoal_old" in full_text
        # Check that step 1 (old) is NOT present (folded)
        assert "Step 1 summary" not in full_text

        # Check that current subgoal line is present
        assert "- [/] subgoal_current" in full_text
        # Check that current subgoal steps are present as summaries
        assert "Step 2 summary" in full_text
        assert "Step 6 summary" not in full_text
        assert "Step 6" in full_text
        # Check that last step has full details
        assert "Step 7" in full_text
        return mock_response

    mock_llm.ainvoke.side_effect = mock_ainvoke
    mock_llm.bind_tools.return_value = mock_llm

    with (
        patch("artemis.agents.operator.operator.get_llm", return_value=mock_llm),
        patch(
            "artemis.agents.operator.operator.OperatorNode._get_active_subgoal_hash",
            return_value=subgoal_current_hash,
        ),
        patch("pathlib.Path.exists", return_value=True),
        patch("pathlib.Path.read_text", mock_read_text),
    ):
        node = OperatorNode(mock_ctx, last_n_detailed=5, transcript_config=LEGACY_TRANSCRIPT)
        node_update = await node(mock_state)


@pytest.mark.asyncio
async def test_operator_tracks_subagent_calls():
    from artemis.agents.operator.operator import OperatorNode
    from artemis.context import ArtemisContext
    from unittest.mock import AsyncMock, MagicMock, patch

    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.execution_setup = None
    mock_ctx.data_engine = None

    mock_state = MagicMock()
    mock_state.subagent_calls = ["ask_explorer"]
    mock_state.initial_goal = "Test goal"

    mock_controller = MagicMock()
    mock_controller.take_screenshot = AsyncMock(return_value="YmFzZTY0X3NjcmVlbnNob3Q=")
    mock_device_data = MagicMock()
    mock_device_data.elements = []
    mock_controller.get_screen_data = AsyncMock(return_value=mock_device_data)

    mock_llm = MagicMock()
    mock_response = MagicMock()
    mock_response.tool_calls = [
        {"name": "ask_diagnoser", "args": {"query": "test"}, "id": "call_1"}
    ]

    mock_response_2 = MagicMock()
    mock_response_2.tool_calls = [{"name": "click", "args": {"target": [10, 20]}, "id": "call_2"}]

    responses = [mock_response, mock_response_2]
    call_count = 0

    async def mock_ainvoke(*args, **kwargs):
        nonlocal call_count
        if call_count < len(responses):
            res = responses[call_count]
            call_count += 1
            return res

    mock_llm.ainvoke.side_effect = mock_ainvoke
    mock_llm.bind_tools.return_value = mock_llm

    mock_diagnoser_tool = MagicMock()
    mock_diagnoser_tool.name = "ask_diagnoser"
    mock_diagnoser_tool.args = {"state": None}
    mock_diagnoser_tool.ainvoke = AsyncMock(return_value="Diagnosis result")

    with (
        patch("artemis.agents.operator.operator.get_llm", return_value=mock_llm),
        patch(
            "artemis.agents.operator.operator.trace_langchain_tool",
            side_effect=lambda t, ctx: t,
        ),
    ):
        node = OperatorNode(
            mock_ctx, tools=[mock_diagnoser_tool], transcript_config=LEGACY_TRANSCRIPT
        )
        node_update = await node(mock_state)

        update_dict = node_update

        assert "subagent_calls" in update_dict
        assert update_dict["subagent_calls"] == [
            "ask_explorer",
            "ask_diagnoser",
        ]


@pytest.mark.asyncio
async def test_operator_defer_action_for_gathering():
    from artemis.agents.operator.operator import OperatorNode
    from artemis.context import ArtemisContext
    from unittest.mock import AsyncMock, MagicMock, patch
    import json

    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.execution_setup = None
    mock_ctx.data_engine = None

    mock_state = MagicMock()
    mock_state.subagent_calls = []
    mock_state.initial_goal = "Test goal"

    mock_controller = MagicMock()
    mock_controller.take_screenshot = AsyncMock(return_value="YmFzZTY0X3NjcmVlbnNob3Q=")
    mock_device_data = MagicMock()
    mock_device_data.elements = []
    mock_controller.get_screen_data = AsyncMock(return_value=mock_device_data)

    mock_llm = MagicMock()

    # First response calls both ask_diagnoser (gathering) and click (action)
    mock_response_1 = MagicMock()
    mock_response_1.tool_calls = [
        {
            "name": "ask_diagnoser",
            "args": {"query": "check something"},
            "id": "call_diagnose",
        },
        {"name": "click", "args": {"target": [50, 50]}, "id": "call_click"},
    ]

    # Second response only calls click
    mock_response_2 = MagicMock()
    mock_response_2.tool_calls = [
        {
            "name": "click",
            "args": {"target": [50, 50]},
            "id": "call_click_final",
        }
    ]

    responses = [mock_response_1, mock_response_2]
    call_count = 0

    async def mock_ainvoke(*args, **kwargs):
        nonlocal call_count
        if call_count < len(responses):
            res = responses[call_count]
            call_count += 1
            return res

    mock_llm.ainvoke.side_effect = mock_ainvoke
    mock_llm.bind_tools.return_value = mock_llm

    from langgraph.prebuilt import InjectedState
    from typing import Annotated

    mock_diagnoser_tool = MagicMock()
    mock_diagnoser_tool.name = "ask_diagnoser"
    mock_diagnoser_tool.args = {"state": None}

    mock_call_receiver = Mock()

    async def dummy_diagnoser_func(query: str, state: Annotated[MagicMock, InjectedState]):
        mock_call_receiver(query=query, state=state)
        return "Diagnostics OK"

    mock_diagnoser_tool.coroutine = dummy_diagnoser_func
    mock_diagnoser_tool.func = None

    with (
        patch("artemis.agents.operator.operator.get_llm", return_value=mock_llm),
        patch(
            "artemis.agents.operator.operator.trace_langchain_tool",
            side_effect=lambda t, ctx: t,
        ),
    ):
        node = OperatorNode(
            mock_ctx, tools=[mock_diagnoser_tool], transcript_config=LEGACY_TRANSCRIPT
        )
        node_update = await node(mock_state)

        # Subagent tool should have been executed
        mock_call_receiver.assert_called_once_with(query="check something", state=mock_state)

        # Verify final state has the action execution from the second response
        update_dict = node_update

        assert "structured_decisions" in update_dict
        decisions = json.loads(update_dict["structured_decisions"])
        assert len(decisions) == 1
        assert decisions[0]["action"] == "tap"
        assert decisions[0]["coordinates"] == [54, 120]


@pytest.mark.asyncio
async def test_wait_actions_translation():
    from artemis.agents.operator.operator import OperatorNode
    from unittest.mock import MagicMock
    from artemis.graph.state import State

    mock_ctx = MagicMock()
    node = OperatorNode(mock_ctx, transcript_config=LEGACY_TRANSCRIPT)
    mock_state = MagicMock(spec=State)

    # 1. Test wait_for_delay translation
    actions, err = node._translate_and_validate_tool(
        {"name": "wait_for_delay", "args": {"time_in_ms": 2500}}, mock_state
    )
    assert err is None
    assert actions == [{"action": "wait_for_delay", "time_in_ms": 2500}]

    # Test wait_for_delay invalid type coercion
    actions, err = node._translate_and_validate_tool(
        {"name": "wait_for_delay", "args": {"time_in_ms": "3000"}}, mock_state
    )
    assert err is None
    assert actions == [{"action": "wait_for_delay", "time_in_ms": 3000}]


@pytest.mark.asyncio
async def test_long_press_action_translation():
    from artemis.agents.operator.operator import OperatorNode
    from unittest.mock import MagicMock
    from artemis.graph.state import State

    mock_ctx = MagicMock()
    node = OperatorNode(mock_ctx, transcript_config=LEGACY_TRANSCRIPT)
    mock_state = MagicMock(spec=State)
    mock_state.indexed_points = [[200, 300], [400, 500]]
    mock_state.indexed_elements = [
        {
            "center": [200, 300],
            "text": "Item 1",
            "bounds": "[100,100][300,500]",
            "class": "android.widget.Button",
            "resource_id": "btn_1",
        },
        {
            "center": [400, 500],
            "text": "hello",
            "bounds": "[300,400][500,600]",
            "class": "android.widget.EditText",
            "resource_id": "input_1",
        },
    ]

    # 1. Test long_press with element index target
    actions, err = node._translate_and_validate_tool(
        {"name": "long_press", "args": {"target": 1, "duration": 1500}},
        mock_state,
    )
    assert err is None
    assert actions == [
        {
            "action": "long_press_on",
            "coordinates": [200, 300],
            "normalized_coordinates": [185, 125],
            "duration": 1500,
            "target_text": "Item 1",
            "target_bounds": "[100,100][300,500]",
            "target_resource_id": "btn_1",
            "target_class": "android.widget.Button",
            "target_label_source": "index",
        }
    ]

    # 2. Test long_press with custom coordinates and default duration
    actions, err = node._translate_and_validate_tool(
        {"name": "long_press", "args": {"target": [500, 600]}}, mock_state
    )
    assert err is None
    # Since coordinates are in 0-1000 scale, they should be converted using device width (default 1080) and height (default 2400)
    # x = 500 * 1080 / 1000 = 540
    # y = 600 * 2400 / 1000 = 1440
    assert actions == [
        {
            "action": "long_press_on",
            "coordinates": [540, 1440],
            "normalized_coordinates": [500, 600],
            "duration": 1000,
            "target_text": None,
            "target_bounds": None,
            "target_resource_id": None,
            "target_class": None,
            "target_label_source": "none",
        }
    ]

    # 3. Test long_press with invalid index
    actions, err = node._translate_and_validate_tool(
        {"name": "long_press", "args": {"target": 5}}, mock_state
    )
    assert "Error" in err


@pytest.mark.asyncio
async def test_operator_no_defer_for_note_updating():
    from artemis.agents.operator.operator import OperatorNode
    from artemis.context import ArtemisContext
    from unittest.mock import AsyncMock, MagicMock, patch
    import json

    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.execution_setup = None
    mock_ctx.data_engine = None

    mock_state = MagicMock()
    mock_state.subagent_calls = []
    mock_state.initial_goal = "Test goal"

    mock_controller = MagicMock()
    mock_controller.take_screenshot = AsyncMock(return_value="YmFzZTY0X3NjcmVlbnNob3Q=")
    mock_device_data = MagicMock()
    mock_device_data.elements = []
    mock_controller.get_screen_data = AsyncMock(return_value=mock_device_data)

    mock_llm = MagicMock()

    # LLM calls both update_note (writing, non-deferring) and click (action) in 1st turn
    mock_response_1 = MagicMock()
    mock_response_1.tool_calls = [
        {
            "name": "update_note",
            "args": {"key": "progress", "content": "step 1"},
            "id": "call_save",
        },
        {"name": "click", "args": {"target": [50, 50]}, "id": "call_click"},
    ]

    # We only expect 1 LLM call (astream should only yield once)
    responses = [mock_response_1]
    call_count = 0

    async def mock_ainvoke(*args, **kwargs):
        nonlocal call_count
        if call_count < len(responses):
            res = responses[call_count]
            call_count += 1
            return res

    mock_llm.ainvoke.side_effect = mock_ainvoke
    mock_llm.bind_tools.return_value = mock_llm

    # Mock the update_note tool
    mock_update_note_tool = MagicMock()
    mock_update_note_tool.name = "update_note"
    mock_update_note_tool.args = {}

    mock_call_receiver = MagicMock()

    async def dummy_update_note(key: str, content: str, **kwargs):
        mock_call_receiver(key=key, content=content)
        return "Note updated"

    mock_update_note_tool.coroutine = dummy_update_note
    mock_update_note_tool.func = None

    with (
        patch("artemis.agents.operator.operator.get_llm", return_value=mock_llm),
        patch(
            "artemis.agents.operator.operator.trace_langchain_tool",
            side_effect=lambda t, ctx: t,
        ),
    ):
        node = OperatorNode(
            mock_ctx, tools=[mock_update_note_tool], transcript_config=LEGACY_TRANSCRIPT
        )
        node_update = await node(mock_state)

        # The update_note tool should have been executed immediately
        mock_call_receiver.assert_called_once_with(key="progress", content="step 1")

        # The click action should NOT be deferred, so it should be in structured_decisions
        update_dict = node_update

        assert "structured_decisions" in update_dict
        decisions = json.loads(update_dict["structured_decisions"])
        assert len(decisions) == 1
        assert decisions[0]["action"] == "tap"
        assert decisions[0]["coordinates"] == [54, 120]


@pytest.mark.asyncio
async def test_operator_defer_for_note_reading():
    from artemis.agents.operator.operator import OperatorNode
    from artemis.context import ArtemisContext
    from unittest.mock import AsyncMock, MagicMock, patch
    import json

    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.execution_setup = None
    mock_ctx.data_engine = None

    mock_state = MagicMock()
    mock_state.subagent_calls = []
    mock_state.initial_goal = "Test goal"

    mock_controller = MagicMock()
    mock_controller.take_screenshot = AsyncMock(return_value="YmFzZTY0X3NjcmVlbnNob3Q=")
    mock_device_data = MagicMock()
    mock_device_data.elements = []
    mock_controller.get_screen_data = AsyncMock(return_value=mock_device_data)

    mock_llm = MagicMock()

    # 1st turn: LLM calls both read_note (reading, deferring) and click (action)
    mock_response_1 = MagicMock()
    mock_response_1.tool_calls = [
        {"name": "read_note", "args": {"key": "progress"}, "id": "call_read"},
        {"name": "click", "args": {"target": [50, 50]}, "id": "call_click"},
    ]

    # 2nd turn: LLM calls only click after receiving note content
    mock_response_2 = MagicMock()
    mock_response_2.tool_calls = [
        {
            "name": "click",
            "args": {"target": [50, 50]},
            "id": "call_click_final",
        }
    ]

    responses = [mock_response_1, mock_response_2]
    call_count = 0

    async def mock_ainvoke(*args, **kwargs):
        nonlocal call_count
        if call_count < len(responses):
            res = responses[call_count]
            call_count += 1
            return res

    mock_llm.ainvoke.side_effect = mock_ainvoke
    mock_llm.bind_tools.return_value = mock_llm

    # Mock the read_note tool
    mock_read_note_tool = MagicMock()
    mock_read_note_tool.name = "read_note"
    mock_read_note_tool.args = {}

    mock_call_receiver = MagicMock()

    async def dummy_read_note(key: str, **kwargs):
        mock_call_receiver(key=key)
        return "Note content"

    mock_read_note_tool.coroutine = dummy_read_note
    mock_read_note_tool.func = None

    with (
        patch("artemis.agents.operator.operator.get_llm", return_value=mock_llm),
        patch(
            "artemis.agents.operator.operator.trace_langchain_tool",
            side_effect=lambda t, ctx: t,
        ),
    ):
        node = OperatorNode(
            mock_ctx, tools=[mock_read_note_tool], transcript_config=LEGACY_TRANSCRIPT
        )
        node_update = await node(mock_state)

        # The read_note tool should have been executed
        mock_call_receiver.assert_called_once_with(key="progress")

        # The click action from the first response should have been deferred,
        # so we should have needed the second response to get the final click.
        # Total call_count should be 2.
        assert call_count == 2

        update_dict = node_update

        assert "structured_decisions" in update_dict
        decisions = json.loads(update_dict["structured_decisions"])
        assert len(decisions) == 1
        assert decisions[0]["action"] == "tap"
        assert decisions[0]["coordinates"] == [54, 120]


@pytest.mark.asyncio
async def test_operator_no_defer_for_note_appending():
    from artemis.agents.operator.operator import OperatorNode
    from artemis.context import ArtemisContext
    from unittest.mock import AsyncMock, MagicMock, patch
    import json

    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.execution_setup = None
    mock_ctx.data_engine = None

    mock_state = MagicMock()
    mock_state.subagent_calls = []
    mock_state.initial_goal = "Test goal"

    mock_controller = MagicMock()
    mock_controller.take_screenshot = AsyncMock(return_value="YmFzZTY0X3NjcmVlbnNob3Q=")
    mock_device_data = MagicMock()
    mock_device_data.elements = []
    mock_controller.get_screen_data = AsyncMock(return_value=mock_device_data)

    mock_llm = MagicMock()

    # LLM calls both append_note (writing, non-deferring) and click (action) in 1st turn
    mock_response_1 = MagicMock()
    mock_response_1.tool_calls = [
        {
            "name": "append_note",
            "args": {"key": "progress", "content": "step 1"},
            "id": "call_append",
        },
        {"name": "click", "args": {"target": [50, 50]}, "id": "call_click"},
    ]

    responses = [mock_response_1]
    call_count = 0

    async def mock_ainvoke(*args, **kwargs):
        nonlocal call_count
        if call_count < len(responses):
            res = responses[call_count]
            call_count += 1
            return res

    mock_llm.ainvoke.side_effect = mock_ainvoke
    mock_llm.bind_tools.return_value = mock_llm

    # Mock the append_note tool
    mock_append_note_tool = MagicMock()
    mock_append_note_tool.name = "append_note"
    mock_append_note_tool.args = {}

    mock_call_receiver = MagicMock()

    async def dummy_append_note(key: str, content: str, **kwargs):
        mock_call_receiver(key=key, content=content)
        return "Note appended"

    mock_append_note_tool.coroutine = dummy_append_note
    mock_append_note_tool.func = None

    with (
        patch("artemis.agents.operator.operator.get_llm", return_value=mock_llm),
        patch(
            "artemis.agents.operator.operator.trace_langchain_tool",
            side_effect=lambda t, ctx: t,
        ),
    ):
        node = OperatorNode(
            mock_ctx, tools=[mock_append_note_tool], transcript_config=LEGACY_TRANSCRIPT
        )
        node_update = await node(mock_state)

        # The append_note tool should have been executed immediately
        mock_call_receiver.assert_called_once_with(key="progress", content="step 1")

        # The click action should NOT be deferred, so it should be in structured_decisions
        update_dict = node_update

        assert "structured_decisions" in update_dict
        decisions = json.loads(update_dict["structured_decisions"])
        assert len(decisions) == 1
        assert decisions[0]["action"] == "tap"
        assert decisions[0]["coordinates"] == [54, 120]


@pytest.mark.asyncio
async def test_operator_background_tasks_prompt_injection():
    from artemis.agents.operator.operator import OperatorNode
    from artemis.context import ArtemisContext
    from artemis.tools.command_tool import BackgroundTask, get_adb_task_registry
    from unittest.mock import AsyncMock, MagicMock, patch

    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.execution_setup = None
    mock_ctx.data_engine = None

    mock_state = MagicMock()
    mock_state.subagent_calls = []
    mock_state.initial_goal = "Test goal"

    mock_controller = MagicMock()
    mock_controller.take_screenshot = AsyncMock(return_value="YmFzZTY0X3NjcmVlbnNob3Q=")
    mock_device_data = MagicMock()
    mock_device_data.elements = []
    mock_controller.get_screen_data = AsyncMock(return_value=mock_device_data)

    mock_llm = MagicMock()
    mock_response = MagicMock()
    mock_response.tool_calls = []

    mock_llm.ainvoke = AsyncMock(return_value=mock_response)
    mock_llm.bind_tools.return_value = mock_llm

    # Setup a mock background task
    mock_proc = MagicMock()
    bg_task = BackgroundTask(
        task_id="task_99999",
        command="ping google.com",
        process=mock_proc,
        terminal_id="term_xyz",
        cwd="/data/local/tmp",
    )

    adb_registry = get_adb_task_registry(mock_ctx)
    adb_registry.background["task_99999"] = bg_task

    try:
        captured_messages = []

        async def mock_invoke_llm_loop(*args, **kwargs):
            active_background_tasks = [
                {
                    "task_id": "task_99999",
                    "command": "ping google.com",
                    "cwd": "/data/local/tmp",
                    "terminal_id": "term_xyz",
                    "output_line_count": 0,
                }
            ]
            messages = await node._build_prompt(
                state=mock_state,
                latest_screenshot_b64="YmFzZTY0X3NjcmVlbnNob3Q=",
                fused_xml=[],
                minimal_list="",
                current_step_num=1,
                steps=[],
                task_plan="",
                active_background_tasks=active_background_tasks,
            )
            captured_messages.extend(messages)
            return [], None, None, False

        with (
            patch("artemis.agents.operator.operator.get_llm", return_value=mock_llm),
            patch(
                "artemis.agents.operator.operator.OperatorNode._invoke_llm_loop",
                side_effect=mock_invoke_llm_loop,
            ),
        ):
            node = OperatorNode(mock_ctx, transcript_config=LEGACY_TRANSCRIPT)
            node_update = await node(mock_state)

            # Assert that the captured messages (prompts) contain the background tasks section
            full_content = ""
            for m in captured_messages:
                if isinstance(m.content, list):
                    for part in m.content:
                        if isinstance(part, dict) and "text" in part:
                            full_content += part["text"] + "\n"
                elif isinstance(m.content, str):
                    full_content += m.content + "\n"

            assert "--- Active Background ADB Tasks ---" in full_content
            assert "task_99999" in full_content
            assert "ping google.com" in full_content
            assert "term_xyz" in full_content
            assert "Accumulated Output: 0 lines of logs" in full_content
    finally:
        adb_registry.background.pop("task_99999", None)


@pytest.mark.asyncio
async def test_operator_task_plan_warning_prompt_injection():
    from artemis.agents.operator.operator import OperatorNode
    from artemis.context import ArtemisContext
    from unittest.mock import AsyncMock, MagicMock, patch

    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.execution_setup = None
    mock_ctx.data_engine = None

    mock_state = MagicMock()
    mock_state.subagent_calls = []
    mock_state.initial_goal = "Test goal"

    mock_controller = MagicMock()
    mock_controller.take_screenshot = AsyncMock(return_value="YmFzZTY0X3NjcmVlbnNob3Q=")
    mock_device_data = MagicMock()
    mock_device_data.elements = []
    mock_controller.get_screen_data = AsyncMock(return_value=mock_device_data)

    mock_llm = MagicMock()
    mock_response = MagicMock()
    mock_response.tool_calls = []

    mock_llm.ainvoke = AsyncMock(return_value=mock_response)
    mock_llm.bind_tools.return_value = mock_llm

    # Case 1: task_plan updated in the last step (using the "key" argument)
    steps_updated = [
        {
            "step_id": "step_1",
            "step_number": 1,
            "tool_calls": [{"name": "click", "args": {"target": 1}}],
        },
        {
            "step_id": "step_2",
            "step_number": 2,
            "tool_calls": [
                {
                    "name": "update_note",
                    "args": {"key": "task_plan", "target": "a", "replacement": "b"},
                }
            ],
        },
    ]

    captured_messages_1 = []

    async def mock_invoke_llm_loop_1(*args, **kwargs):
        messages = await node._build_prompt(
            state=mock_state,
            latest_screenshot_b64="YmFzZTY0X3NjcmVlbnNob3Q=",
            fused_xml=[],
            minimal_list="",
            current_step_num=3,
            steps=steps_updated,
            task_plan="",
        )
        captured_messages_1.extend(messages)
        return [], None, None, False

    with (
        patch("artemis.agents.operator.operator.get_llm", return_value=mock_llm),
        patch(
            "artemis.agents.operator.operator.OperatorNode._invoke_llm_loop",
            side_effect=mock_invoke_llm_loop_1,
        ),
    ):
        node = OperatorNode(mock_ctx, transcript_config=LEGACY_TRANSCRIPT)
        node_update = await node(mock_state)

        full_content_1 = ""
        for m in captured_messages_1:
            if isinstance(m.content, list):
                for part in m.content:
                    if isinstance(part, dict) and "text" in part:
                        full_content_1 += part["text"] + "\n"
            elif isinstance(m.content, str):
                full_content_1 += m.content + "\n"

        assert "Reminder: your last turn executed a Turn-Ending Action" not in full_content_1

    # Case 2: an action turn that did NOT update task_plan
    steps_not_updated = [
        {
            "step_id": "step_1",
            "step_number": 1,
            "tool_calls": [{"name": "click", "args": {"target": 1}}],
        },
        {
            "step_id": "step_2",
            "step_number": 2,
            "tool_calls": [{"name": "click", "args": {"target": 2}}],
            "action_taken": {"action": "click", "target": 2},
        },
    ]

    captured_messages_2 = []

    async def mock_invoke_llm_loop_2(*args, **kwargs):
        messages = await node._build_prompt(
            state=mock_state,
            latest_screenshot_b64="YmFzZTY0X3NjcmVlbnNob3Q=",
            fused_xml=[],
            minimal_list="",
            current_step_num=3,
            steps=steps_not_updated,
            task_plan="",
        )
        captured_messages_2.extend(messages)
        return [], None, None, False

    with (
        patch("artemis.agents.operator.operator.get_llm", return_value=mock_llm),
        patch(
            "artemis.agents.operator.operator.OperatorNode._invoke_llm_loop",
            side_effect=mock_invoke_llm_loop_2,
        ),
    ):
        node = OperatorNode(mock_ctx, transcript_config=LEGACY_TRANSCRIPT)
        node_update = await node(mock_state)

        full_content_2 = ""
        for m in captured_messages_2:
            if isinstance(m.content, list):
                for part in m.content:
                    if isinstance(part, dict) and "text" in part:
                        full_content_2 += part["text"] + "\n"
            elif isinstance(m.content, str):
                full_content_2 += m.content + "\n"

        assert "Reminder: your last turn executed a Turn-Ending Action" in full_content_2


@pytest.mark.asyncio
async def test_operator_tool_limit_exceeded_warning():
    from artemis.agents.operator.operator import OperatorNode
    from artemis.context import ArtemisContext
    from unittest.mock import AsyncMock, MagicMock, patch

    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.execution_setup = None
    mock_ctx.data_engine = None

    mock_state = MagicMock()
    mock_state.subagent_calls = []
    # Set the flag to True to simulate tool call limit exceeded in previous turn
    mock_state.operator_tool_limit_exceeded = True
    mock_state.initial_goal = "Test goal"

    mock_controller = MagicMock()
    mock_controller.take_screenshot = AsyncMock(return_value="YmFzZTY0X3NjcmVlbnNob3Q=")
    mock_device_data = MagicMock()
    mock_device_data.elements = []
    mock_controller.get_screen_data = AsyncMock(return_value=mock_device_data)

    mock_llm = MagicMock()
    mock_response = MagicMock()
    mock_response.tool_calls = []

    captured_messages = []

    async def mock_invoke_llm_loop(*args, **kwargs):
        messages = await node._build_prompt(
            state=mock_state,
            latest_screenshot_b64="YmFzZTY0X3NjcmVlbnNob3Q=",
            fused_xml=[],
            minimal_list="",
            current_step_num=1,
            steps=[],
            task_plan="",
        )
        captured_messages.extend(messages)
        # Return 4 elements: action_result, raw_thinking, native_thinking, tool_limit_exceeded
        return [], None, None, False

    with (
        patch("artemis.agents.operator.operator.get_llm", return_value=mock_llm),
        patch(
            "artemis.agents.operator.operator.OperatorNode._invoke_llm_loop",
            side_effect=mock_invoke_llm_loop,
        ),
    ):
        node = OperatorNode(mock_ctx, transcript_config=LEGACY_TRANSCRIPT)
        node_update = await node(mock_state)

        full_content = ""
        for m in captured_messages:
            if isinstance(m.content, list):
                for part in m.content:
                    if isinstance(part, dict) and "text" in part:
                        full_content += part["text"] + "\n"
            elif isinstance(m.content, str):
                full_content += m.content + "\n"

        assert (
            "your last turn used up its tool-call budget without a Turn-Ending Action"
            in full_content
        )

        assert node_update.get("operator_tool_limit_exceeded") is False


@pytest.mark.asyncio
async def test_operator_fallback_function_call_parsing():
    from artemis.agents.operator.operator import OperatorNode
    from artemis.context import ArtemisContext
    from unittest.mock import AsyncMock, MagicMock, patch
    from langchain_core.messages import AIMessage
    import json

    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.execution_setup = None
    mock_ctx.data_engine = None

    mock_state = MagicMock()
    mock_state.subagent_calls = []
    mock_state.initial_goal = "Test goal"

    mock_controller = MagicMock()
    mock_controller.take_screenshot = AsyncMock(return_value="YmFzZTY0X3NjcmVlbnNob3Q=")
    mock_device_data = MagicMock()
    mock_device_data.elements = []
    mock_controller.get_screen_data = AsyncMock(return_value=mock_device_data)

    # Mock response with empty tool_calls but populated additional_kwargs['function_call']
    mock_response = AIMessage(
        content="I intend to click the button.",
        additional_kwargs={
            "function_call": {
                "name": "click",
                "arguments": '{"target": [123, 456]}',
            }
        },
        tool_calls=[],
    )

    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=mock_response)
    mock_llm.bind_tools.return_value = mock_llm

    with patch("artemis.agents.operator.operator.get_llm", return_value=mock_llm):
        node = OperatorNode(mock_ctx, transcript_config=LEGACY_TRANSCRIPT)
        node_update = await node(mock_state)

        # Verify state was updated with parsed click action
        update_dict = node_update

        assert "structured_decisions" in update_dict
        decisions = json.loads(update_dict["structured_decisions"])
        assert len(decisions) == 1
        assert decisions[0]["action"] == "tap"
        assert decisions[0]["coordinates"] == [132, 1094]


@pytest.mark.asyncio
async def test_operator_swipe_translation():
    from artemis.agents.operator.operator import OperatorNode
    from artemis.context import ArtemisContext
    from unittest.mock import MagicMock

    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_state = MagicMock()
    mock_state.operator_raw_data = {"width": 1080, "height": 2400}
    node = OperatorNode(mock_ctx, transcript_config=LEGACY_TRANSCRIPT)

    # 1. Directional swipe
    actions, err = node._translate_and_validate_tool(
        {"name": "swipe", "args": {"direction": "up"}}, mock_state
    )
    assert err is None
    assert len(actions) == 1
    assert actions[0]["action"] == "swipe"
    assert actions[0]["coordinates"] == [648, 1680, 648, 720]

    # 2. Start & End coordinate swipe (drag)
    actions, err = node._translate_and_validate_tool(
        {
            "name": "swipe",
            "args": {"start": [100, 200], "end": [500, 600], "duration": 1500},
        },
        mock_state,
    )
    assert err is None
    assert len(actions) == 1
    assert actions[0]["action"] == "swipe"
    assert actions[0]["coordinates"] == [108, 480, 540, 1440]
    assert actions[0]["duration"] == 1500

    # 3. Coordinates alias
    actions, err = node._translate_and_validate_tool(
        {
            "name": "swipe",
            "args": {"coordinates": [200, 300, 400, 500]},
        },
        mock_state,
    )
    assert err is None
    assert len(actions) == 1
    assert actions[0]["action"] == "swipe"
    assert actions[0]["coordinates"] == [216, 720, 432, 1200]

    # 4. Backward compatible gesture parameter
    actions, err = node._translate_and_validate_tool(
        {
            "name": "swipe",
            "args": {"gesture": "down"},
        },
        mock_state,
    )
    assert err is None
    assert len(actions) == 1
    assert actions[0]["action"] == "swipe"
    assert actions[0]["coordinates"] == [648, 720, 648, 1680]
