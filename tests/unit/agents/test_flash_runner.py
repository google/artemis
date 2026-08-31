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

from artemis.agents.flash.runner import FlashRunner
from artemis.context import ArtemisContext


@pytest.fixture
def mock_context():
    ctx = Mock(spec=ArtemisContext)
    ctx.llm_config = Mock()
    mock_llm_cfg = Mock()
    mock_llm_cfg.model = "gemini-3.7-flash"
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
