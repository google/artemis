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
from langchain_core.messages import AIMessage
from artemis.agents.validator.failure_analyzer import ValidationErrorCategory
from artemis.agents.validator.validator import ValidatorNode
from artemis.context import ArtemisContext
import pytest


class DummyState:
    def __init__(
        self,
        structured_decisions,
        validator_messages=[],
        current_step_id=None,
        latest_screenshot=None,
    ):
        self.structured_decisions = structured_decisions
        self.validator_messages = validator_messages
        self.current_step_id = current_step_id
        self.latest_screenshot = latest_screenshot

    async def asanitize_update(self, ctx, update, agent):
        return update


@pytest.fixture
def mock_context(tmp_path):
    ctx = Mock(spec=ArtemisContext)
    ctx.llm_config = Mock()
    ctx.data_engine = Mock()
    ctx.data_engine.base_dir = tmp_path
    ctx.data_engine.get_relative_time.return_value = 1.0
    return ctx


@pytest.fixture
def temp_screenshot(tmp_path):
    p = tmp_path / "screenshot.png"
    p.write_bytes(b"dummy_data")
    return str(p)


@pytest.fixture
def mock_mcp():
    with (
        patch("mcp.client.stdio.stdio_client") as mock_stdio,
        patch("artemis.agents.validator.validator.ClientSession") as mock_session_cls,
    ):
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__.return_value = (Mock(), Mock())
        mock_stdio.return_value = mock_ctx

        mock_session = AsyncMock()
        mock_session.initialize = AsyncMock()
        mock_session_cls.return_value = mock_session

        yield mock_session


@pytest.mark.asyncio
async def test_validator_success(mock_mcp, mock_context, temp_screenshot):
    """Test that ValidatorNode executes actions successfully."""
    # Mock MCP calls: screenshot, tap, screenshot
    mock_mcp.call_tool.side_effect = [
        Mock(content=[Mock(text="ZHVtbXlfZGF0YQ==")]),  # pre-screenshot
        Mock(content=[Mock(text="Success")]),  # tap action
        Mock(content=[Mock(text="ZHVtbXlfZGF0YQ==")]),  # post-screenshot
    ]

    decisions = json.dumps([{"action": "tap", "coordinates": [105, 205]}])
    state = DummyState(structured_decisions=decisions, latest_screenshot=temp_screenshot)

    with patch("artemis.utils.image_diff.check_ui_change", return_value=True):
        node = ValidatorNode(mock_context)
        result = await node(state)

    assert "last_execution_result" in result
    report = result["last_execution_result"]
    assert "execution" in report
    assert len(report["execution"]) == 1
    assert report["execution"][0]["action"] == "tap"
    assert "attempts" not in report["execution"][0]


@pytest.mark.asyncio
async def test_validator_failure_analysis(mock_mcp, mock_context, temp_screenshot):
    """Test that ValidatorNode triggers failure analysis on error and handles
    cannot_fix via tool.
    """
    tap_count = 0

    def mock_call_tool(name, args):
        nonlocal tap_count
        if name == "take_screenshot":
            return Mock(content=[Mock(text="ZHVtbXlfZGF0YQ==")])
        if name == "tap":
            tap_count += 1
            return Mock(content=[Mock(text="Error: Element not found")])
        return Mock(content=[Mock(text="Success")])

    mock_mcp.call_tool.side_effect = mock_call_tool

    decisions = json.dumps([{"action": "tap", "coordinates": [105, 205]}])
    state = DummyState(structured_decisions=decisions, latest_screenshot=temp_screenshot)

    node = ValidatorNode(mock_context)

    with (
        patch("artemis.agents.validator.failure_analyzer.FailureAnalyzer.analyze") as mock_analyze,
        patch("artemis.utils.image_diff.check_ui_change", return_value=False),
    ):
        mock_analyze.return_value = {
            "status": "cannot_fix",
            "analysis": "Analysis: Coordinates are likely wrong.",
        }

        result = await node(state)

    assert "last_execution_result" in result
    report = result["last_execution_result"]
    assert "execution" in report
    assert len(report["execution"]) == 1
    assert report["execution"][0]["action"] == "tap"
    assert report["execution"][0]["attempts"] == [
        "Error: Element not found",
        "Error: Element not found",
    ]
    assert report["execution"][0]["repair"] == "Analysis: Coordinates are likely wrong."


@pytest.mark.asyncio
async def test_validator_repair_success(mock_mcp, mock_context, temp_screenshot):
    """Test that ValidatorNode terminates successfully and clears remaining actions
    after successful repair.
    """
    tap_count = 0

    def mock_call_tool(name, args):
        nonlocal tap_count
        if name == "take_screenshot":
            return Mock(content=[Mock(text="ZHVtbXlfZGF0YQ==")])
        if name == "tap":
            tap_count += 1
            if tap_count <= 2:  # First tap fails
                return Mock(content=[Mock(text="Error: Element not found")])
            return Mock(content=[Mock(text="Success")])
        return Mock(content=[Mock(text="Success")])

    mock_mcp.call_tool.side_effect = mock_call_tool

    # Two actions planned initially
    decisions = json.dumps(
        [
            {"action": "tap", "coordinates": [105, 205]},
            {"action": "tap", "coordinates": [205, 305]},
        ]
    )
    state = DummyState(structured_decisions=decisions, latest_screenshot=temp_screenshot)

    node = ValidatorNode(mock_context)

    with (
        patch("artemis.agents.validator.failure_analyzer.FailureAnalyzer.analyze") as mock_analyze,
        patch("artemis.utils.image_diff.check_ui_change", return_value=False),
    ):
        mock_analyze.return_value = {
            "status": "fixed",
            "analysis": "Fixed by updating actions",
        }

        result = await node(state)

    assert "last_execution_result" in result
    report = result["last_execution_result"]
    assert "execution" in report
    assert len(report["execution"]) == 1  # Only the first failed action is in report

    assert report["execution"][0]["coordinates"] == [105, 205]
    assert report["execution"][0]["attempts"] == [
        "Error: Element not found",
        "Error: Element not found",
    ]
    assert report["execution"][0]["repair"] == "Fixed by updating actions"


@pytest.mark.asyncio
async def test_validator_wait_for_delay(mock_mcp, mock_context, temp_screenshot):
    """Test that wait_for_delay executes successfully."""

    def mock_call(name, args):
        return Mock(content=[Mock(text="ZHVtbXlfZGF0YQ==")])

    mock_mcp.call_tool.side_effect = mock_call

    decisions = json.dumps([{"action": "wait_for_delay", "time_in_ms": 10}])
    state = DummyState(structured_decisions=decisions, latest_screenshot=temp_screenshot)

    with patch("artemis.utils.image_diff.check_ui_change", return_value=False):
        node = ValidatorNode(mock_context)
        result = await node(state)

    assert "last_execution_result" in result
    report = result["last_execution_result"]
    assert "execution" in report
    assert len(report["execution"]) == 1
    assert report["execution"][0]["action"] == "wait_for_delay"


@pytest.mark.asyncio
async def test_validator_focus_and_clear_text_no_ui_change(mock_mcp, mock_context, temp_screenshot):
    """Test that focus_and_clear_text executes successfully even if no UI change is detected."""

    def mock_call(name, args):
        if name == "focus_and_clear_text":
            return Mock(content=[Mock(text="Success")])
        return Mock(content=[Mock(text="ZHVtbXlfZGF0YQ==")])

    mock_mcp.call_tool.side_effect = mock_call

    decisions = json.dumps([{"action": "focus_and_clear_text", "coordinates": [400, 500]}])
    state = DummyState(structured_decisions=decisions, latest_screenshot=temp_screenshot)

    with patch("artemis.utils.image_diff.check_ui_change", return_value=False):
        node = ValidatorNode(mock_context)
        result = await node(state)

    assert "last_execution_result" in result
    report = result["last_execution_result"]
    assert "execution" in report
    assert len(report["execution"]) == 1
    assert report["execution"][0]["action"] == "focus_and_clear_text"


@pytest.mark.asyncio
async def test_validator_silent_failure_treated_as_success(mock_mcp, mock_context, temp_screenshot):
    """Test that silent failure (exec succeeds but no UI change) is treated as
    success and does not trigger FailureAnalyzer.
    """

    def mock_call_tool(name, args):
        if name == "take_screenshot":
            return Mock(content=[Mock(text="ZHVtbXlfZGF0YQ==")])
        if name == "tap":
            return Mock(content=[Mock(text="Success")])
        return Mock(content=[Mock(text="Success")])

    mock_mcp.call_tool.side_effect = mock_call_tool

    decisions = json.dumps([{"action": "tap", "coordinates": [105, 205]}])
    state = DummyState(structured_decisions=decisions, latest_screenshot=temp_screenshot)

    node = ValidatorNode(mock_context)

    with (
        patch("artemis.agents.validator.validator.VALIDATOR_POLL_TIMEOUT", 0.1),
        patch("artemis.agents.validator.validator.VALIDATOR_POLL_INTERVAL", 0.01),
        patch("artemis.agents.validator.failure_analyzer.FailureAnalyzer.analyze") as mock_analyze,
        patch("artemis.utils.image_diff.check_ui_change", return_value=False),  # No UI change
    ):
        result = await node(state)

    # Assert FailureAnalyzer was NOT called
    mock_analyze.assert_not_called()

    # Assert the action is considered executed successfully
    assert "last_execution_result" in result
    report = result["last_execution_result"]
    assert "execution" in report
    assert len(report["execution"]) == 1
    assert report["execution"][0]["action"] == "tap"
    assert "attempts" not in report["execution"][0]


@pytest.mark.asyncio
async def test_validator_records_skipped_actions_on_cannot_fix(
    mock_mcp, mock_context, temp_screenshot
):
    """Test that ValidatorNode marks unexecuted actions as Skipped when repair fails."""

    def mock_call_tool(name, args):
        if name == "take_screenshot":
            return Mock(content=[Mock(text="ZHVtbXlfZGF0YQ==")])
        return Mock(content=[Mock(text="Error: Connection lost")])

    mock_mcp.call_tool.side_effect = mock_call_tool

    # Two actions planned
    decisions = json.dumps(
        [
            {"action": "tap", "coordinates": [100, 200]},
            {"action": "press_key", "keycode": "ENTER"},
        ]
    )
    state = DummyState(structured_decisions=decisions, latest_screenshot=temp_screenshot)

    node = ValidatorNode(mock_context)

    with (
        patch("artemis.agents.validator.failure_analyzer.FailureAnalyzer.analyze") as mock_analyze,
        patch("artemis.utils.image_diff.check_ui_change", return_value=False),
    ):
        mock_analyze.return_value = {
            "status": "cannot_fix",
            "analysis": "Cannot fix: System crashed.",
        }

        result = await node(state)

    assert "last_execution_result" in result
    report = result["last_execution_result"]
    assert "execution" in report
    assert len(report["execution"]) == 2

    # First action failed
    assert report["execution"][0]["action"] == "tap"
    assert report["execution"][0]["attempts"] == [
        "Error: Connection lost",
        "Error: Connection lost",
    ]
    assert report["execution"][0]["repair"] == "Cannot fix: System crashed."

    # Second action was skipped
    assert report["execution"][1]["action"] == "press_key"
    assert report["execution"][1]["attempts"] == ["Skipped"]


@pytest.mark.asyncio
async def test_validator_pre_execution_validation_and_repair(
    mock_mcp, mock_context, temp_screenshot
):
    """Test that pre-execution validation fails when element changes, and is
    repaired by FailureAnalyzer.
    """
    mock_live_elements = [
        {
            "text": "Sign Up",
            "bounds": "[100,200][300,300]",
            "resource-id": "btn_signup",
        }
    ]

    def mock_call_tool(name, args):
        if name == "take_screenshot":
            return Mock(content=[Mock(text="ZHVtbXlfZGF0YQ==")])
        if name == "get_ui_hierarchy":
            return Mock(content=[Mock(text=json.dumps(mock_live_elements))])
        if name == "tap":
            return Mock(content=[Mock(text="Success")])
        return Mock(content=[Mock(text="Success")])

    mock_mcp.call_tool.side_effect = mock_call_tool

    decisions = json.dumps(
        [
            {
                "action": "tap",
                "coordinates": [200, 250],
                "target_text": "Login",
                "target_bounds": [100, 200, 300, 300],
                "target_resource_id": "btn_login",
            }
        ]
    )

    state = DummyState(structured_decisions=decisions, latest_screenshot=temp_screenshot)
    node = ValidatorNode(mock_context)

    with (
        patch("artemis.agents.validator.failure_analyzer.FailureAnalyzer.analyze") as mock_analyze,
        patch("artemis.utils.image_diff.check_ui_change", return_value=True),
        patch.object(
            ValidatorNode,
            "_validate_action_precondition_pixel",
            new_callable=AsyncMock,
        ) as mock_pixel,
    ):
        mock_pixel.return_value = (
            False,
            ValidationErrorCategory.PIXEL_TARGET_DISAPPEARED,
            "Pixel check failed",
        )
        mock_analyze.return_value = {
            "status": "fixed",
            "analysis": ("Pre-exec failure matched and resolved by switching to Sign Up button."),
        }

        result = await node(state)

    mock_analyze.assert_called_once()
    args, kwargs = mock_analyze.call_args
    assert "Pre-execution validation failed" in args[2]
    assert "Login" in args[2]
    assert kwargs.get("error_category") == ValidationErrorCategory.TARGET_OCCUPIED

    assert "last_execution_result" in result
    report = result["last_execution_result"]
    assert len(report["execution"]) == 1
    assert report["execution"][0]["action"] == "tap"
    assert "Pre-execution validation failed" in report["execution"][0]["attempts"][0]
    assert (
        report["execution"][0]["repair"]
        == "Pre-exec failure matched and resolved by switching to Sign Up"
        " button."
    )


@pytest.mark.asyncio
async def test_validator_pre_execution_validation_self_healing(
    mock_mcp, mock_context, temp_screenshot
):
    """Test that pre-execution validation succeeds and self-heals when element shifts slightly."""
    mock_live_elements = [
        {
            "text": "Login",
            "bounds": "[100,220][300,320]",
            "resource-id": "btn_login",
        }
    ]

    tapped_coordinates = None

    def mock_call_tool(name, args):
        nonlocal tapped_coordinates
        if name == "take_screenshot":
            return Mock(content=[Mock(text="ZHVtbXlfZGF0YQ==")])
        if name == "get_ui_hierarchy":
            return Mock(content=[Mock(text=json.dumps(mock_live_elements))])
        if name == "tap":
            tapped_coordinates = args.get("coordinates")
            return Mock(content=[Mock(text="Success")])
        return Mock(content=[Mock(text="Success")])

    mock_mcp.call_tool.side_effect = mock_call_tool

    decisions = json.dumps(
        [
            {
                "action": "tap",
                "coordinates": [200, 250],
                "target_text": "Login",
                "target_bounds": [100, 200, 300, 300],
                "target_resource_id": "btn_login",
            }
        ]
    )

    state = DummyState(structured_decisions=decisions, latest_screenshot=temp_screenshot)
    node = ValidatorNode(mock_context)

    with patch("artemis.utils.image_diff.check_ui_change", return_value=True):
        await node(state)

    # Center of [100, 220][300, 320] is [200, 270]
    assert tapped_coordinates == [200, 270]


@pytest.mark.asyncio
async def test_validator_pre_execution_validation_anonymous_occupant(
    mock_mcp, mock_context, temp_screenshot
):
    """Test that pre-execution validation correctly flags TARGET_OCCUPIED
    when coordinates are blocked by an anonymous clickable view.
    """
    # The live screen contains an anonymous clickable view covering
    # target coordinates [200, 250]
    mock_live_elements = [
        {
            "text": "",
            "bounds": "[150,200][250,300]",
            "clickable": True,
            "resource-id": "",
        }
    ]

    def mock_call_tool(name, args):
        if name == "take_screenshot":
            return Mock(content=[Mock(text="ZHVtbXlfZGF0YQ==")])
        if name == "get_ui_hierarchy":
            return Mock(content=[Mock(text=json.dumps(mock_live_elements))])
        return Mock(content=[Mock(text="Success")])

    mock_mcp.call_tool.side_effect = mock_call_tool

    decisions = json.dumps(
        [
            {
                "action": "tap",
                "coordinates": [200, 250],
                "target_text": "Login",
                "target_bounds": [100, 200, 300, 300],
                "target_resource_id": "btn_login",
            }
        ]
    )

    state = DummyState(structured_decisions=decisions, latest_screenshot=temp_screenshot)
    node = ValidatorNode(mock_context)

    with (
        patch("artemis.agents.validator.failure_analyzer.FailureAnalyzer.analyze") as mock_analyze,
        patch("artemis.utils.image_diff.check_ui_change", return_value=True),
        patch.object(
            ValidatorNode,
            "_validate_action_precondition_pixel",
            new_callable=AsyncMock,
        ) as mock_pixel,
    ):
        mock_pixel.return_value = (
            False,
            ValidationErrorCategory.PIXEL_TARGET_DISAPPEARED,
            "Pixel check failed",
        )
        mock_analyze.return_value = {
            "status": "fixed",
            "analysis": "Pre-exec failure bypassed.",
        }

        await node(state)

    # Assert that the validator correctly classified this as TARGET_OCCUPIED
    # (due to the clickable anonymous view blocking the click)
    mock_analyze.assert_called_once()
    args, kwargs = mock_analyze.call_args
    assert kwargs.get("error_category") == ValidationErrorCategory.TARGET_OCCUPIED
    assert "occupied/intercepted by a different element: interactive anonymous element" in args[2]


@pytest.mark.asyncio
async def test_validator_pixel_validation_success(mock_mcp, mock_context, temp_screenshot):
    """Test that the pixel safety net succeeds when Gemini reports target is present."""
    mock_mcp.call_tool.side_effect = [
        Mock(content=[Mock(text="ZHVtbXlfZGF0YQ==")]),  # live screenshot for pixel check
        Mock(content=[Mock(text="Success")]),  # tap action
        Mock(content=[Mock(text="ZHVtbXlfZGF0YQ==")]),  # post-screenshot
    ]

    decisions = json.dumps([{"action": "tap", "coordinates": [100, 200]}])
    state = DummyState(structured_decisions=decisions, latest_screenshot=temp_screenshot)

    mock_agent_cfg = Mock()
    mock_agent_cfg.provider = "google"
    mock_agent_cfg.model = "gemini-3.5-flash-lite"
    mock_context.llm_config.get_agent.return_value = mock_agent_cfg

    mock_response = Mock()
    mock_response.text = json.dumps(
        {
            "is_present": True,
            "reason": "Button is clearly visible and clickable.",
            "confidence": 0.95,
        }
    )

    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=AIMessage(content=mock_response.text))

    # Mock crop and draw helpers to return dummy bytes
    with (
        patch(
            "artemis.utils.visualization.crop_and_annotate_target",
            return_value=b"dummy_bytes",
        ),
        patch("artemis.agents.validator.validator.get_llm", return_value=mock_llm),
        patch("artemis.utils.image_diff.check_ui_change", return_value=True),
    ):
        node = ValidatorNode(mock_context)
        result = await node(state)

    assert "last_execution_result" in result
    report = result["last_execution_result"]
    assert len(report["execution"]) == 1
    assert "attempts" not in report["execution"][0]


@pytest.mark.asyncio
async def test_validator_pixel_validation_failure(mock_mcp, mock_context, temp_screenshot):
    """Test that the pixel safety net fails and triggers FailureAnalyzer
    when Gemini reports target is missing.
    """

    def mock_call_tool(name, args):
        if name == "take_screenshot":
            return Mock(content=[Mock(text="ZHVtbXlfZGF0YQ==")])
        return Mock(content=[Mock(text="Success")])

    mock_mcp.call_tool.side_effect = mock_call_tool

    decisions = json.dumps([{"action": "tap", "coordinates": [100, 200]}])
    state = DummyState(structured_decisions=decisions, latest_screenshot=temp_screenshot)

    mock_agent_cfg = Mock()
    mock_agent_cfg.provider = "google"
    mock_agent_cfg.model = "gemini-3.5-flash-lite"
    mock_context.llm_config.get_agent.return_value = mock_agent_cfg

    mock_response = Mock()
    mock_response.text = json.dumps(
        {
            "is_present": False,
            "reason": "The button has completely disappeared from the screen.",
            "confidence": 0.90,
        }
    )

    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=AIMessage(content=mock_response.text))

    with (
        patch(
            "artemis.utils.visualization.crop_and_annotate_target",
            return_value=b"dummy_bytes",
        ),
        patch("artemis.agents.validator.validator.get_llm", return_value=mock_llm),
        patch("artemis.utils.image_diff.check_ui_change", return_value=False),
        patch("artemis.agents.validator.failure_analyzer.FailureAnalyzer.analyze") as mock_analyze,
    ):
        mock_analyze.return_value = {
            "status": "cannot_fix",
            "analysis": "Pixel safety net flagged missing target.",
        }

        node = ValidatorNode(mock_context)
        await node(state)

    mock_analyze.assert_called_once()
    args, kwargs = mock_analyze.call_args
    assert "Pixel-level validation failed" in args[2]
    assert kwargs.get("error_category") == ValidationErrorCategory.PIXEL_TARGET_DISAPPEARED


@pytest.mark.asyncio
@patch("artemis.agents.validator.validator.find_package")
@patch("artemis.utils.app_launch_utils.launch_app_with_retries")
async def test_validator_launch_app_passes_use_fallback_mcp(
    mock_launch_app, mock_find_package, mock_mcp, mock_context, temp_screenshot
):
    """Test that ValidatorNode calls find_package with use_fallback=False
    for launch_app action in MCP mode.
    """
    mock_find_package.return_value = "com.example.app"
    mock_launch_app.return_value = (True, None)

    # Mock MCP calls: screenshot (pre), screenshot (post)
    mock_mcp.call_tool.side_effect = [
        Mock(content=[Mock(text="ZHVtbXlfZGF0YQ==")]),  # pre-screenshot
        Mock(content=[Mock(text="ZHVtbXlfZGF0YQ==")]),  # post-screenshot
    ]

    decisions = json.dumps([{"action": "launch_app", "app_name": "My App"}])
    state = DummyState(structured_decisions=decisions, latest_screenshot=temp_screenshot)

    with patch("artemis.utils.image_diff.check_ui_change", return_value=True):
        node = ValidatorNode(mock_context)
        result = await node(state)

    mock_find_package.assert_called_once_with(mock_context, "My App", use_fallback=False)
    mock_launch_app.assert_called_once_with(mock_context, "com.example.app")
    assert "last_execution_result" in result


@pytest.mark.asyncio
@patch("artemis.agents.validator.validator.find_package")
@patch("artemis.utils.app_launch_utils.launch_app_with_retries")
async def test_validator_launch_app_failure_no_retry(
    mock_launch_app, mock_find_package, mock_mcp, mock_context, temp_screenshot
):
    """Test that ValidatorNode does NOT retry launch_app action on failure."""
    mock_find_package.return_value = "com.example.app"
    mock_launch_app.return_value = (False, "Error: Force close")

    def mock_call_tool(name, args):
        if name == "take_screenshot":
            return Mock(content=[Mock(text="ZHVtbXlfZGF0YQ==")])
        return Mock(content=[Mock(text="Success")])

    mock_mcp.call_tool.side_effect = mock_call_tool

    decisions = json.dumps([{"action": "launch_app", "app_name": "My App"}])
    state = DummyState(structured_decisions=decisions, latest_screenshot=temp_screenshot)

    with (
        patch("artemis.agents.validator.validator.VALIDATOR_POLL_TIMEOUT", 0.05),
        patch("artemis.agents.validator.validator.VALIDATOR_POLL_INTERVAL", 0.01),
        patch("artemis.agents.validator.failure_analyzer.FailureAnalyzer.analyze") as mock_analyze,
        patch("artemis.utils.image_diff.check_ui_change", return_value=False),
    ):
        mock_analyze.return_value = {
            "status": "cannot_fix",
            "analysis": "Cannot launch app.",
        }

        node = ValidatorNode(mock_context)
        result = await node(state)

    # Verify find_package was called only once
    mock_find_package.assert_called_once_with(mock_context, "My App", use_fallback=False)
    # Verify launch_app_with_retries was called only once
    # (no retry by ValidatorNode itself since it relies on launch_app_with_retries)
    mock_launch_app.assert_called_once_with(mock_context, "com.example.app")

    # Verify attempts list has only 1 element
    assert "last_execution_result" in result
    report = result["last_execution_result"]
    assert len(report["execution"]) == 1
    assert report["execution"][0]["action"] == "launch_app"
    assert report["execution"][0]["attempts"] == ["Error: Force close"]


@pytest.mark.asyncio
async def test_validator_pre_execution_validation_disappeared_not_shifted(
    mock_mcp, mock_context, temp_screenshot
):
    """Test that a disappeared small button matching text in a giant container
    is classified as TARGET_DISAPPEARED, not TARGET_SHIFTED.
    """
    # Live hierarchy only contains a giant background/container
    # containing the text "Sign Up"
    mock_live_elements = [
        {
            "text": "Sign Up",
            "bounds": "[0,0][1080,2400]",
            "resource-id": "root_container",
        }
    ]

    def mock_call_tool(name, args):
        if name == "take_screenshot":
            return Mock(content=[Mock(text="ZHVtbXlfZGF0YQ==")])
        if name == "get_ui_hierarchy":
            return Mock(content=[Mock(text=json.dumps(mock_live_elements))])
        return Mock(content=[Mock(text="Success")])

    mock_mcp.call_tool.side_effect = mock_call_tool

    decisions = json.dumps(
        [
            {
                "action": "tap",
                "coordinates": [200, 250],
                "target_text": "Sign Up",
                "target_bounds": [100, 200, 300, 300],
                "target_resource_id": "btn_signup",
            }
        ]
    )

    state = DummyState(structured_decisions=decisions, latest_screenshot=temp_screenshot)
    node = ValidatorNode(mock_context)

    with (
        patch("artemis.agents.validator.failure_analyzer.FailureAnalyzer.analyze") as mock_analyze,
        patch("artemis.utils.image_diff.check_ui_change", return_value=True),
        patch.object(
            ValidatorNode,
            "_validate_action_precondition_pixel",
            new_callable=AsyncMock,
        ) as mock_pixel,
    ):
        mock_pixel.return_value = (
            False,
            ValidationErrorCategory.PIXEL_TARGET_DISAPPEARED,
            "Pixel check failed",
        )
        mock_analyze.return_value = {
            "status": "cannot_fix",
            "analysis": "Sign Up button disappeared.",
        }

        await node(state)

    # Assert that the validator correctly classified this as TARGET_DISAPPEARED
    # (since size mismatch prevents shift matching)
    mock_analyze.assert_called_once()
    args, kwargs = mock_analyze.call_args
    assert kwargs.get("error_category") == ValidationErrorCategory.TARGET_DISAPPEARED


@pytest.mark.asyncio
async def test_validator_pre_execution_validation_ocr_direct_to_pixel(
    mock_mcp, mock_context, temp_screenshot
):
    """Test that OCR-derived elements bypass XML validation and
    directly use pixel-based validation.
    """
    mock_mcp.call_tool.side_effect = [
        Mock(content=[Mock(text="ZHVtbXlfZGF0YQ==")]),  # live screenshot for pixel check
        Mock(content=[Mock(text="Success")]),  # tap action
        Mock(content=[Mock(text="ZHVtbXlfZGF0YQ==")]),  # post-screenshot
    ]

    decisions = json.dumps(
        [
            {
                "action": "tap",
                "coordinates": [200, 250],
                "target_text": "Login",
                "target_bounds": [100, 200, 300, 300],
                "target_class": "android.view.View [OCR]",
            }
        ]
    )

    state = DummyState(structured_decisions=decisions, latest_screenshot=temp_screenshot)
    node = ValidatorNode(mock_context)

    with (
        patch.object(
            ValidatorNode,
            "_validate_action_precondition",
            new_callable=AsyncMock,
        ) as mock_xml,
        patch.object(
            ValidatorNode,
            "_validate_action_precondition_pixel",
            new_callable=AsyncMock,
        ) as mock_pixel,
        patch("artemis.utils.image_diff.check_ui_change", return_value=True),
    ):
        mock_pixel.return_value = (True, ValidationErrorCategory.NONE, "")

        result = await node(state)

    # Assert XML validation was NOT called
    mock_xml.assert_not_called()
    # Assert Pixel validation WAS called
    mock_pixel.assert_called_once()

    assert "last_execution_result" in result
    report = result["last_execution_result"]
    assert report["status"] == "success"


@pytest.mark.asyncio
async def test_validator_pre_execution_xml_failure_not_overridden_when_pixel_bypassed(
    mock_mcp, mock_context, temp_screenshot
):
    """Test that when XML validation fails AND Pixel validation is bypassed
    (PIXEL_BYPASSED), the XML failure is NOT overridden.
    """
    mock_live_elements = [
        {
            "text": "Sign Up",
            "bounds": "[100,200][300,300]",
            "resource-id": "btn_signup",
        }
    ]

    def mock_call_tool(name, args):
        if name == "take_screenshot":
            return Mock(content=[Mock(text="ZHVtbXlfZGF0YQ==")])
        if name == "get_ui_hierarchy":
            return Mock(content=[Mock(text=json.dumps(mock_live_elements))])
        return Mock(content=[Mock(text="Success")])

    mock_mcp.call_tool.side_effect = mock_call_tool

    decisions = json.dumps(
        [
            {
                "action": "tap",
                "coordinates": [200, 250],
                "target_text": "Login",
                "target_bounds": [100, 200, 300, 300],
                "target_resource_id": "btn_login",
            }
        ]
    )

    state = DummyState(structured_decisions=decisions, latest_screenshot=temp_screenshot)
    node = ValidatorNode(mock_context)

    with (
        patch("artemis.agents.validator.failure_analyzer.FailureAnalyzer.analyze") as mock_analyze,
        patch("artemis.utils.image_diff.check_ui_change", return_value=True),
        patch.object(
            ValidatorNode,
            "_validate_action_precondition_pixel",
            new_callable=AsyncMock,
        ) as mock_pixel,
    ):
        # Pixel check was bypassed due to VLM error or low confidence
        mock_pixel.return_value = (
            True,
            ValidationErrorCategory.PIXEL_BYPASSED,
            "",
        )
        mock_analyze.return_value = {
            "status": "cannot_fix",
            "analysis": ("XML validation failed and pixel bypass did not override."),
        }

        await node(state)

    # FailureAnalyzer SHOULD be called because XML validation failed
    # and PIXEL_BYPASSED did not override it
    mock_analyze.assert_called_once()
    args, kwargs = mock_analyze.call_args
    assert "Pre-execution validation failed" in args[2]
