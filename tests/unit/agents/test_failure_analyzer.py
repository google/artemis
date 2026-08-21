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

"""Unit tests for Universal Failure Analyzer."""

import base64
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from langchain_core.messages import AIMessage

from artemis.agents.validator.failure_analyzer import (
    FailureAnalyzer,
    PixelTargetDisappearedStrategy,
    TargetDisappearedStrategy,
    ValidationErrorCategory,
    VALIDATOR_TOOLS_DECLARATION,
)
from artemis.context import ArtemisContext


class DummyState:
    def __init__(self, latest_screenshot=None):
        self.latest_screenshot = latest_screenshot
        self.indexed_points = []
        self.indexed_elements = []


@pytest.fixture
def mock_context(tmp_path):
    ctx = Mock(spec=ArtemisContext)
    ctx.llm_config = Mock()
    mock_llm_cfg = Mock()
    mock_llm_cfg.model = "gemini-3.7-pro"
    mock_llm_cfg.temperature = 0.1
    ctx.llm_config.get_agent.return_value = mock_llm_cfg

    ctx.device = Mock()
    ctx.device.device_width = 1080
    ctx.device.device_height = 2400

    ctx.data_engine = Mock()
    ctx.data_engine.base_dir = tmp_path
    ctx.data_engine.get_agent_friendly_steps.return_value = []
    ctx.adb_client = None
    ctx.driver = Mock()
    return ctx


@pytest.mark.asyncio
async def test_failure_analyzer_cannot_fix(mock_context, tmp_path):
    """Test that FailureAnalyzer natively executes and handles 'cannot_fix' report."""
    screenshot_file = tmp_path / "shot.jpg"
    screenshot_file.write_bytes(b"dummy_bytes")

    state = DummyState(latest_screenshot=str(screenshot_file))
    failed_action = {"action": "click", "coordinates": [100, 200]}
    error_msg = "Element not found"

    analyzer = FailureAnalyzer(mock_context)

    mock_llm = MagicMock()
    mock_response = AIMessage(
        content="I see the error.",
        tool_calls=[
            {
                "name": "report_failure_analysis",
                "args": {
                    "status": "cannot_fix",
                    "analysis": "The element was definitely not there.",
                },
                "id": "call_1",
            }
        ],
    )
    mock_bound = MagicMock()
    mock_bound.ainvoke = AsyncMock(return_value=mock_response)
    mock_llm.bind_tools.return_value = mock_bound

    with (
        patch("artemis.agents.validator.failure_analyzer.get_llm", return_value=mock_llm),
        patch(
            "artemis.agents.validator.failure_analyzer.UnifiedMobileController"
        ) as mock_controller_cls,
    ):
        mock_controller = Mock()
        mock_controller_cls.return_value = mock_controller
        mock_device_data = Mock()
        mock_device_data.base64 = base64.b64encode(b"mocked_screenshot").decode()
        mock_device_data.elements = []
        mock_device_data.width = 1080
        mock_device_data.height = 2400
        mock_controller.get_screen_data = AsyncMock(return_value=mock_device_data)
        mock_controller.get_ui_elements = AsyncMock(return_value=mock_device_data.elements)

        result = await analyzer.analyze(
            state,
            failed_action,
            error_msg,
            pre_screenshot=base64.b64encode(b"dummy").decode(),
            post_screenshot=base64.b64encode(b"dummy").decode(),
        )

        assert result["status"] == "cannot_fix"
        assert "not there" in result["analysis"]
        assert result["new_remaining_actions"] == []


def test_report_failure_analysis_tool_declaration():
    """Test that report_failure_analysis tool declaration is correct and matches."""
    report_tool = next(
        (t for t in VALIDATOR_TOOLS_DECLARATION if t.name == "report_failure_analysis"),
        None,
    )
    assert report_tool is not None
    props = report_tool.parameters.get("properties", {})
    assert "analysis" in props
    assert "single paragraph" in props["analysis"]["description"]


@pytest.mark.asyncio
async def test_failure_analyzer_strategy_routing(mock_context):
    """Test that FailureAnalyzer selects appropriate strategies based on error category."""
    analyzer = FailureAnalyzer(mock_context)

    strat_disappeared = analyzer._select_strategy(ValidationErrorCategory.TARGET_DISAPPEARED)
    assert isinstance(strat_disappeared, TargetDisappearedStrategy)
    assert "target_disappeared" in strat_disappeared.get_prompt_template_name()

    strat_pixel = analyzer._select_strategy(ValidationErrorCategory.PIXEL_TARGET_DISAPPEARED)
    assert isinstance(strat_pixel, PixelTargetDisappearedStrategy)
    assert "pixel_target_disappeared" in strat_pixel.get_prompt_template_name()


@pytest.mark.asyncio
async def test_failure_analyzer_click_sequence(mock_context, tmp_path):
    """Test click_sequence execution in FailureAnalyzer."""
    screenshot_file = tmp_path / "shot.jpg"
    screenshot_file.write_bytes(b"dummy_bytes")

    state = DummyState(latest_screenshot=str(screenshot_file))
    analyzer = FailureAnalyzer(mock_context)

    # Turn 1: Call click_sequence tool with direct coordinates
    call_step_1 = AIMessage(
        content="Let me click in sequence.",
        tool_calls=[
            {
                "name": "click_sequence",
                "args": {"sequence": [[100, 200], [300, 400]]},
                "id": "call_seq_1",
            }
        ],
    )
    # Turn 2: Report failure fixed
    call_step_2 = AIMessage(
        content="Repaired successfully.",
        tool_calls=[
            {
                "name": "report_failure_analysis",
                "args": {
                    "status": "fixed",
                    "analysis": "Sequence clicked successfully.",
                },
                "id": "call_report_2",
            }
        ],
    )

    mock_llm = MagicMock()
    mock_bound = MagicMock()
    mock_bound.ainvoke = AsyncMock(side_effect=[call_step_1, call_step_2])
    mock_llm.bind_tools.return_value = mock_bound

    with (
        patch("artemis.agents.validator.failure_analyzer.get_llm", return_value=mock_llm),
        patch(
            "artemis.agents.validator.failure_analyzer.UnifiedMobileController"
        ) as mock_controller_cls,
    ):
        mock_controller = Mock()
        mock_controller_cls.return_value = mock_controller
        mock_device_data = Mock()
        mock_device_data.base64 = base64.b64encode(b"mocked_screenshot").decode()
        mock_device_data.elements = []
        mock_device_data.width = 1080
        mock_device_data.height = 2400
        mock_controller.get_screen_data = AsyncMock(return_value=mock_device_data)
        mock_controller.get_ui_elements = AsyncMock(return_value=mock_device_data.elements)
        mock_controller.tap_at = AsyncMock(return_value=Mock(error=None))

        result = await analyzer.analyze(
            state,
            {"action": "click", "coordinates": [100, 200]},
            "Element not found",
            pre_screenshot=base64.b64encode(b"dummy").decode(),
            post_screenshot=base64.b64encode(b"dummy").decode(),
            error_category=ValidationErrorCategory.TARGET_DISAPPEARED,
        )

        assert result["status"] == "fixed"
        assert mock_controller.tap_at.call_count == 2
