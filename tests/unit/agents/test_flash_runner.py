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

"""Unit tests for Universal FlashRunner."""

from unittest.mock import Mock, patch
import pytest
from langchain_core.messages import HumanMessage, ToolMessage

from artemis.agents.explorer.constants import ASK_EXPLORER_DESCRIPTION
from artemis.agents.flash.runner import FlashRunner
from artemis.agents.validator.tool_declarations import ASK_EXPLORER_TOOL
from artemis.context import ArtemisContext


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


def test_flash_runner_tools_initialization(mock_context):
    """Verify FlashRunner gathers correct active tools."""
    with patch("artemis.controllers.unified_controller.get_driver"):
        runner = FlashRunner(mock_context, goal="Test Open Settings")
        tools = runner._get_tools()

        tool_names = [t.name for t in tools]
        assert "click" in tool_names
        assert "click_sequence" in tool_names
        assert "swipe" in tool_names
        assert "input_text" in tool_names
        assert "ask_explorer" in tool_names
        assert "report_task_status" in tool_names
        # Validator's report_failure_analysis should NOT be in FlashRunner
        assert "report_failure_analysis" not in tool_names


def test_flash_runner_executor_follows_the_flash_explorer_tier(mock_context):
    """The executor is tagged as the Flash profile so ask_explorer follows flash_mode."""
    with (
        patch("artemis.controllers.unified_controller.get_driver"),
        patch("artemis.agents.flash.runner.McpActionExecutor") as executor_cls,
    ):
        runner = FlashRunner(mock_context, goal="Test Open Settings")

    executor_cls.assert_called_once()
    assert executor_cls.call_args.kwargs["agent_name"] == "flash"
    assert runner.executor is executor_cls.return_value


def test_ask_explorer_declaration_is_tier_agnostic(mock_context):
    """Flash binds the shared ask_explorer contract: query + context_feedback, no tier."""
    with patch("artemis.controllers.unified_controller.get_driver"):
        runner = FlashRunner(mock_context, goal="Test Open Settings")
        declaration = next(t for t in runner._get_tools() if t.name == "ask_explorer")

    assert declaration is ASK_EXPLORER_TOOL
    assert declaration.description == ASK_EXPLORER_DESCRIPTION
    assert set(declaration.parameters["properties"]) == {"query", "context_feedback"}
    assert declaration.parameters["required"] == ["query"]


def test_flash_runner_screenshot_pruning(mock_context):
    """Verify that earlier screenshot blocks are pruned, keeping only the latest."""
    with patch("artemis.controllers.unified_controller.get_driver"):
        runner = FlashRunner(mock_context, goal="Test Pruning")

        messages = [
            HumanMessage(
                content=[
                    {"type": "text", "text": "Turn 1 goal"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64,SCREENSHOT_1"},
                    },
                ]
            ),
            ToolMessage(
                tool_call_id="tc1",
                name="click",
                content=[
                    {"type": "text", "text": "Clicked button"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64,SCREENSHOT_2"},
                    },
                ],
            ),
            ToolMessage(
                tool_call_id="tc2",
                name="swipe",
                content=[
                    {"type": "text", "text": "Swiped up"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64,SCREENSHOT_3"},
                    },
                ],
            ),
        ]

        runner._prune_intermediate_screenshots(messages)

        # Check Turn 1: image should be removed
        assert len(messages[0].content) == 1
        assert messages[0].content[0]["type"] == "text"

        # Check Turn 2 (Tool 1): image should be removed
        assert len(messages[1].content) == 1
        assert messages[1].content[0]["type"] == "text"

        # Check Turn 3 (Tool 2 - Latest): image MUST be preserved
        assert len(messages[2].content) == 2
        assert messages[2].content[0]["type"] == "text"
        assert messages[2].content[1]["type"] == "image_url"
        assert messages[2].content[1]["image_url"]["url"] == "data:image/jpeg;base64,SCREENSHOT_3"


# --- Recorded action shape (coordinate space + ledger rendering) ---------------------


def _record(runner, name, args):
    from types import SimpleNamespace

    runner.ctx.data_engine = Mock()
    runner.ctx.data_engine.current_step_id = None
    runner.ctx.data_engine.record_step.return_value = "step-1"
    exec_result = SimpleNamespace(
        status="success", text_summary=f"{name} executed", metadata={}, ui_elements_text="ui"
    )
    step_id = runner._record_action_step(
        name, args, exec_result, "thought", {}, b"pre", ["xml"], b"post"
    )
    assert step_id == "step-1"
    return runner.ctx.data_engine.record_step.call_args.kwargs["action_taken"]


def test_record_action_step_stamps_the_normalized_coordinate_space(mock_context):
    """Flash stores the model's own 0–1000 target verbatim, marked as
    normalized, so the agent-friendly view never re-normalizes it."""
    from artemis.utils.coordinates import (
        COORDINATE_SPACE_KEY,
        COORDINATE_SPACE_NORMALIZED,
        normalize_any_structure,
    )

    with patch("artemis.controllers.unified_controller.get_driver"):
        runner = FlashRunner(mock_context, goal="g")
    recorded = _record(runner, "click", {"target": [320, 399]})
    assert recorded["coordinates"] == [320, 399]
    assert recorded["normalized_coordinates"] == [320, 399]
    assert recorded[COORDINATE_SPACE_KEY] == COORDINATE_SPACE_NORMALIZED
    assert normalize_any_structure(recorded, 1080, 2400)["coordinates"] == [320, 399]


def test_flash_records_render_in_the_ledger_with_their_arguments(mock_context):
    """The band-③ phrase reads the tool arguments Flash keeps under ``args``:
    app launches name the package, key presses the key, inputs the text,
    direction swipes the direction — never ``'None'``."""
    from artemis.utils.task_tree import format_actions_clean

    with patch("artemis.controllers.unified_controller.get_driver"):
        runner = FlashRunner(mock_context, goal="g")

    launch = _record(runner, "manage_app", {"action": "launch", "app_name": "com.android.settings"})
    assert format_actions_clean(launch) == "Launched app 'com.android.settings'"
    stop = _record(runner, "manage_app", {"action": "stop", "app_name": "com.android.settings"})
    assert format_actions_clean(stop) == "Stopped app 'com.android.settings'"
    direct = _record(runner, "launch_app", {"app_name": "com.android.settings"})
    assert format_actions_clean(direct) == "Launched app 'com.android.settings'"

    key = _record(runner, "press_key", {"key": "BACK"})
    assert format_actions_clean(key) == "Pressed key 'BACK'"

    typed = _record(runner, "input_text", {"target": [500, 600], "text": "hello"})
    assert format_actions_clean(typed) == "Inputted 'hello' into field at [500, 600]"

    swipe_dir = _record(runner, "swipe", {"direction": "up"})
    assert format_actions_clean(swipe_dir) == "Swiped up"
    swipe_fa = _record(runner, "swipe", {"action": "down"})
    assert format_actions_clean(swipe_fa) == "Swiped down"
    swipe_coords = _record(runner, "swipe", {"coordinates": [556, 289, 556, 124]})
    assert format_actions_clean(swipe_coords) == "Swiped from [556, 289] to [556, 124]"
