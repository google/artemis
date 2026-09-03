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

"""ValidatorNode tests over the in-process unified action session.

The Validator no longer spawns a stdio MCP subprocess; it calls a shared
``ActionSession`` whose device actions return structured ``ActionResult`` objects.
``FakeActionSession`` below stands in for it: ``action_handler`` decides each
action's ``(ok, message)``, ``screenshot`` feeds the polling loop, and ``hierarchy``
feeds the safety-net XML validation.
"""

import json
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from langchain_core.messages import AIMessage
import pytest

from artemis.agents.validator.categories import ValidationErrorCategory
from artemis.agents.validator.validator import ValidatorNode
from artemis.context import ArtemisContext
from artemis.mcp.action_types import ActionCode, ActionResult


class DummyState:
    def __init__(
        self,
        structured_decisions,
        current_step_id=None,
        latest_screenshot=None,
        open_incident=None,
    ):
        self.structured_decisions = structured_decisions
        self.current_step_id = current_step_id
        self.latest_screenshot = latest_screenshot
        self.open_incident = open_incident


class FakeActionSession:
    """Test double for artemis.mcp.action_session.ActionSession."""

    def __init__(self):
        self.started = True
        self.calls: list[tuple[str, dict]] = []
        # (name, args) -> (ok, message)
        self.action_handler = lambda name, args: (True, "")
        # str | callable | Exception
        self.screenshot = "ZHVtbXlfZGF0YQ=="  # b64("dummy_data")
        self.hierarchy: list | Exception = []

    async def call(self, name, args):
        self.calls.append((name, args))
        ok, msg = self.action_handler(name, args)
        return ActionResult(
            ok=ok,
            code=ActionCode.OK if ok else ActionCode.DEVICE_ERROR,
            action=name,
            message=msg,
        )

    async def screenshot_b64(self, timeout=None):
        shot = self.screenshot
        if isinstance(shot, Exception):
            raise shot
        return shot() if callable(shot) else shot

    async def ui_hierarchy(self, timeout=None):
        if isinstance(self.hierarchy, Exception):
            raise self.hierarchy
        return self.hierarchy

    async def aclose(self):
        pass

    def calls_for(self, name: str) -> list[dict]:
        return [args for n, args in self.calls if n == name]


@pytest.fixture
def mock_context(tmp_path):
    ctx = Mock(spec=ArtemisContext)
    ctx.llm_config = Mock()
    ctx.data_engine = Mock()
    ctx.data_engine.base_dir = tmp_path
    ctx.data_engine.get_relative_time.return_value = 1.0
    ctx.device = Mock()
    ctx.device.device_width = 1080
    ctx.device.device_height = 2400
    return ctx


@pytest.fixture
def temp_screenshot(tmp_path):
    p = tmp_path / "screenshot.png"
    p.write_bytes(b"dummy_data")
    return str(p)


@pytest.fixture
def mock_mcp():
    fake = FakeActionSession()

    async def _get_session(ctx, actuator=None):
        return fake

    with patch("artemis.agents.validator.validator.get_action_session", _get_session):
        yield fake


@pytest.mark.asyncio
async def test_validator_success(mock_mcp, mock_context, temp_screenshot):
    """Test that ValidatorNode executes actions successfully."""
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
    # The Operator's tap verb was translated to the canonical click tool.
    assert len(mock_mcp.calls_for("click")) == 1


# NOTE: upstream's test_failed_mcp_handshake_exits_contexts_and_disables_child_awake_policy
# guarded the stdio-subprocess handshake teardown, machinery this migration removed
# entirely. Its hazard class (leaked AnyIO cancel scopes) is now pinned by
# tests/unit/mcp/test_action_session.py against the in-process ActionSession.


@pytest.mark.asyncio
async def test_validator_exec_error_opens_incident(mock_mcp, mock_context, temp_screenshot):
    """A vetted single action that the device rejects is retried once, then an
    execution incident is opened for the Operator (no repair agent)."""
    mock_mcp.action_handler = lambda name, args: (
        (False, "Error: Element not found") if name == "click" else (True, "")
    )

    decisions = json.dumps([{"action": "tap", "coordinates": [105, 205]}])
    state = DummyState(structured_decisions=decisions, latest_screenshot=temp_screenshot)

    node = ValidatorNode(mock_context)
    with patch("artemis.utils.image_diff.check_ui_change", return_value=False):
        result = await node(state)

    report = result["last_execution_result"]
    assert report["status"] == "failed"
    assert report["burst"] is False
    assert len(report["execution"]) == 1
    assert report["execution"][0]["attempts"] == [
        "Error: Element not found",
        "Error: Element not found",
    ]
    assert "repair" not in report["execution"][0]

    incident = report["incident"]
    assert incident["kind"] == "exec_error"
    assert incident["category"] == "general"
    assert incident["reason"] == "Error: Element not found"
    assert incident["consecutive_failures"] == 1
    assert incident["burst_size"] == 1
    # The Operator reads the incident: the pixel target [105, 205] is rendered
    # in its own 0-1000 space (1080x2400 frame).
    assert incident["action_description"] == "Tapped element at [97, 85]"
    # The same record rides in graph state for the Operator prompt.
    assert result["open_incident"] == incident


@pytest.mark.asyncio
async def test_validator_burst_skips_safety_net_and_aborts_on_first_failure(
    mock_mcp, mock_context, temp_screenshot
):
    """Two or more actions form a fast-action burst: no precondition gate, a
    single dispatch per member, and the first failure aborts the rest."""
    calls = []

    def handler(name, args):
        calls.append(name)
        if name == "click" and len([c for c in calls if c == "click"]) == 2:
            return False, "Error: tap rejected"
        return True, ""

    mock_mcp.action_handler = handler

    decisions = json.dumps(
        [
            {"action": "tap", "coordinates": [105, 205]},
            {"action": "tap", "coordinates": [205, 305]},
            {"action": "press_key", "keycode": "BACK"},
        ]
    )
    state = DummyState(structured_decisions=decisions, latest_screenshot=temp_screenshot)

    node = ValidatorNode(mock_context)
    with (
        patch(
            "artemis.agents.validator.execution_loop._run_precondition_gate",
            new_callable=AsyncMock,
        ) as mock_gate,
        patch("artemis.utils.image_diff.check_ui_change", return_value=False),
    ):
        result = await node(state)

    mock_gate.assert_not_called()

    report = result["last_execution_result"]
    assert report["burst"] is True
    assert report["status"] == "failed"
    assert len(report["execution"]) == 3
    # First member executed cleanly.
    assert "attempts" not in report["execution"][0]
    # Second member failed after exactly one dispatch (bursts never retry).
    assert report["execution"][1]["attempts"] == ["Error: tap rejected"]
    # Third member never fired.
    assert report["execution"][2]["attempts"] == ["Skipped (burst aborted)"]
    assert len(mock_mcp.calls_for("click")) == 2
    assert mock_mcp.calls_for("press_key") == []

    incident = report["incident"]
    assert incident["kind"] == "exec_error"
    assert incident["action_index"] == 1
    assert incident["burst_size"] == 3
    assert result["open_incident"] == incident


@pytest.mark.asyncio
async def test_validator_burst_success_executes_every_member(
    mock_mcp, mock_context, temp_screenshot
):
    decisions = json.dumps(
        [
            {"action": "tap", "coordinates": [105, 205]},
            {"action": "wait_for_delay", "time_in_ms": 1},
            {"action": "tap", "coordinates": [205, 305]},
        ]
    )
    state = DummyState(structured_decisions=decisions, latest_screenshot=temp_screenshot)

    node = ValidatorNode(mock_context)
    with patch("artemis.utils.image_diff.check_ui_change", return_value=True):
        result = await node(state)

    report = result["last_execution_result"]
    assert report["status"] == "success"
    assert report["burst"] is True
    assert report["incident"] is None
    assert result["open_incident"] is None
    assert result["last_closed_incident"] is None
    assert len(mock_mcp.calls_for("click")) == 2


@pytest.mark.asyncio
async def test_validator_success_closes_open_incident(mock_mcp, mock_context, temp_screenshot):
    """A successful turn closes whatever incident was still open."""
    previous = {
        "kind": "safety_net",
        "category": "target_disappeared",
        "reason": "gone",
        "action": {"action": "tap"},
        "action_description": "Tapped element",
        "consecutive_failures": 2,
    }
    decisions = json.dumps([{"action": "tap", "coordinates": [105, 205]}])
    state = DummyState(
        structured_decisions=decisions,
        latest_screenshot=temp_screenshot,
        open_incident=previous,
    )

    node = ValidatorNode(mock_context)
    with patch("artemis.utils.image_diff.check_ui_change", return_value=True):
        result = await node(state)

    assert result["last_execution_result"]["status"] == "success"
    assert result["open_incident"] is None
    # The closed record is handed over once so the Operator settles the intent.
    assert result["last_closed_incident"]["kind"] == "safety_net"
    assert "closed_at_step" in result["last_closed_incident"]


@pytest.mark.asyncio
async def test_validator_consecutive_failures_escalate_incident(
    mock_mcp, mock_context, temp_screenshot
):
    """Failing again while an incident is open continues its failure count."""
    previous = {
        "kind": "exec_error",
        "category": "general",
        "reason": "Error: Element not found",
        "action": {"action": "tap"},
        "action_description": "Tapped element",
        "consecutive_failures": 2,
    }
    mock_mcp.action_handler = lambda name, args: (False, "Error: Element not found")
    decisions = json.dumps([{"action": "tap", "coordinates": [105, 205]}])
    state = DummyState(
        structured_decisions=decisions,
        latest_screenshot=temp_screenshot,
        open_incident=previous,
    )

    node = ValidatorNode(mock_context)
    with patch("artemis.utils.image_diff.check_ui_change", return_value=False):
        result = await node(state)

    assert result["open_incident"]["consecutive_failures"] == 3


@pytest.mark.asyncio
async def test_validator_wait_for_delay(mock_mcp, mock_context, temp_screenshot):
    """Test that wait_for_delay executes successfully."""
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
    # wait_for_delay is a pure client-side sleep, never a session call.
    assert mock_mcp.calls_for("wait_for_delay") == []


@pytest.mark.asyncio
async def test_validator_focus_and_clear_text_no_ui_change(mock_mcp, mock_context, temp_screenshot):
    """Test that focus_and_clear_text executes successfully even if no UI change is detected."""
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
    assert len(mock_mcp.calls_for("focus_and_clear_text")) == 1


@pytest.mark.asyncio
async def test_validator_silent_failure_treated_as_success(mock_mcp, mock_context, temp_screenshot):
    """Test that silent failure (exec succeeds but no UI change) is treated as
    success and does not open an execution incident.
    """
    decisions = json.dumps([{"action": "tap", "coordinates": [105, 205]}])
    state = DummyState(structured_decisions=decisions, latest_screenshot=temp_screenshot)

    node = ValidatorNode(mock_context)

    with (
        patch("artemis.agents.validator.validator.VALIDATOR_POLL_TIMEOUT", 0.1),
        patch("artemis.agents.validator.validator.VALIDATOR_POLL_INTERVAL", 0.01),
        patch("artemis.utils.image_diff.check_ui_change", return_value=False),  # No UI change
    ):
        result = await node(state)

    # No incident is opened for a silent (no-UI-change) success.
    assert result["open_incident"] is None

    # Assert the action is considered executed successfully
    assert "last_execution_result" in result
    report = result["last_execution_result"]
    assert "execution" in report
    assert len(report["execution"]) == 1
    assert report["execution"][0]["action"] == "tap"
    assert "attempts" not in report["execution"][0]


@pytest.mark.asyncio
async def test_validator_burst_first_member_failure_marks_rest_skipped(
    mock_mcp, mock_context, temp_screenshot
):
    mock_mcp.action_handler = lambda name, args: (False, "Error: Connection lost")

    decisions = json.dumps(
        [
            {"action": "tap", "coordinates": [100, 200]},
            {"action": "press_key", "keycode": "ENTER"},
        ]
    )
    state = DummyState(structured_decisions=decisions, latest_screenshot=temp_screenshot)

    node = ValidatorNode(mock_context)
    with patch("artemis.utils.image_diff.check_ui_change", return_value=False):
        result = await node(state)

    report = result["last_execution_result"]
    assert len(report["execution"]) == 2
    assert report["execution"][0]["action"] == "tap"
    # A burst member is dispatched exactly once.
    assert report["execution"][0]["attempts"] == ["Error: Connection lost"]
    assert "repair" not in report["execution"][0]
    assert report["execution"][1]["action"] == "press_key"
    assert report["execution"][1]["attempts"] == ["Skipped (burst aborted)"]
    assert report["incident"]["action_index"] == 0
    assert report["incident"]["burst_size"] == 2


@pytest.mark.asyncio
async def test_validator_pre_execution_validation_opens_incident(
    mock_mcp, mock_context, temp_screenshot
):
    """Test that pre-execution validation fails when the element changes, and the
    interception is handed to the Operator as an execution incident.
    """
    mock_mcp.hierarchy = [
        {
            "text": "Sign Up",
            "bounds": "[100,200][300,300]",
            "resource-id": "btn_signup",
        }
    ]

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
        result = await node(state)

    incident = result["last_execution_result"]["incident"]
    assert "Pre-execution validation failed" in incident["reason"]
    assert "Login" in incident["reason"]
    assert incident["category"] == ValidationErrorCategory.TARGET_OCCUPIED.value

    assert "last_execution_result" in result
    report = result["last_execution_result"]
    assert len(report["execution"]) == 1
    assert report["execution"][0]["action"] == "tap"
    assert "Pre-execution validation failed" in report["execution"][0]["attempts"][0]
    assert report["incident"]["kind"] == "safety_net"
    assert report["status"] == "failed"


@pytest.mark.asyncio
async def test_validator_pre_execution_validation_self_healing(
    mock_mcp, mock_context, temp_screenshot
):
    """Test that pre-execution validation succeeds and self-heals when element shifts slightly."""
    mock_mcp.hierarchy = [
        {
            "text": "Login",
            "bounds": "[100,220][300,320]",
            "resource-id": "btn_login",
        }
    ]

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
        result = await node(state)

    # Center of [100, 220][300, 320] is pixel [200, 270]; the healed coordinates
    # travel to the canonical click tool normalized to 0-1000 on a 1080x2400 screen:
    # round(200 * 1000 / 1080) = 185, round(270 * 1000 / 2400) = 112.
    clicks = mock_mcp.calls_for("click")
    assert len(clicks) == 1
    assert clicks[0]["target"] == [185, 112]


@pytest.mark.asyncio
async def test_validator_pre_execution_validation_anonymous_occupant(
    mock_mcp, mock_context, temp_screenshot
):
    """Test that pre-execution validation correctly flags TARGET_OCCUPIED
    when coordinates are blocked by an anonymous clickable view.
    """
    # The live screen contains an anonymous clickable view covering
    # target coordinates [200, 250]
    mock_mcp.hierarchy = [
        {
            "text": "",
            "bounds": "[150,200][250,300]",
            "clickable": True,
            "resource-id": "",
        }
    ]

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
        result = await node(state)

    # Assert that the validator correctly classified this as TARGET_OCCUPIED
    # (due to the clickable anonymous view blocking the click)
    incident = result["last_execution_result"]["incident"]
    assert incident["category"] == ValidationErrorCategory.TARGET_OCCUPIED.value
    assert (
        "occupied/intercepted by a different element: interactive anonymous element"
        in incident["reason"]
    )


@pytest.mark.asyncio
async def test_validator_pixel_validation_success(mock_mcp, mock_context, temp_screenshot):
    """Test that the pixel safety net succeeds when Gemini reports target is present."""
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
    """Test that the pixel safety net fails and opens an execution incident
    when Gemini reports target is missing.
    """
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
    ):
        node = ValidatorNode(mock_context)
        result = await node(state)

    incident = result["last_execution_result"]["incident"]
    assert "Pixel-level validation failed" in incident["reason"]
    assert incident["category"] == ValidationErrorCategory.PIXEL_TARGET_DISAPPEARED.value


@pytest.mark.asyncio
async def test_validator_launch_app_routes_through_manage_app(
    mock_mcp, mock_context, temp_screenshot
):
    """Test that a launch_app action item routes to the canonical manage_app tool.

    Package resolution (find_package with use_fallback=False) now happens inside the
    actuator's manage_app implementation and is covered by the failure-analyzer tool
    tests; the Validator's contract is the translation and single dispatch.
    """
    decisions = json.dumps([{"action": "launch_app", "app_name": "My App"}])
    state = DummyState(structured_decisions=decisions, latest_screenshot=temp_screenshot)

    with patch("artemis.utils.image_diff.check_ui_change", return_value=True):
        node = ValidatorNode(mock_context)
        result = await node(state)

    manage_calls = mock_mcp.calls_for("manage_app")
    assert manage_calls == [{"action": "launch", "app_name": "My App"}]
    assert "last_execution_result" in result


@pytest.mark.asyncio
async def test_validator_launch_app_failure_no_retry(mock_mcp, mock_context, temp_screenshot):
    """Test that ValidatorNode does NOT retry launch_app action on failure."""
    mock_mcp.action_handler = lambda name, args: (
        (False, "Error: Force close") if name == "manage_app" else (True, "")
    )

    decisions = json.dumps([{"action": "launch_app", "app_name": "My App"}])
    state = DummyState(structured_decisions=decisions, latest_screenshot=temp_screenshot)

    with (
        patch("artemis.agents.validator.validator.VALIDATOR_POLL_TIMEOUT", 0.05),
        patch("artemis.agents.validator.validator.VALIDATOR_POLL_INTERVAL", 0.01),
        patch("artemis.utils.image_diff.check_ui_change", return_value=False),
    ):
        node = ValidatorNode(mock_context)
        result = await node(state)

    # launch_app allows a single attempt -- no validator-level retry.
    assert len(mock_mcp.calls_for("manage_app")) == 1

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
    mock_mcp.hierarchy = [
        {
            "text": "Sign Up",
            "bounds": "[0,0][1080,2400]",
            "resource-id": "root_container",
        }
    ]

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
        result = await node(state)

    # Assert that the validator correctly classified this as TARGET_DISAPPEARED
    # (since size mismatch prevents shift matching)
    incident = result["last_execution_result"]["incident"]
    assert incident["category"] == ValidationErrorCategory.TARGET_DISAPPEARED.value


@pytest.mark.asyncio
async def test_validator_pre_execution_validation_ocr_direct_to_pixel(
    mock_mcp, mock_context, temp_screenshot
):
    """Test that OCR-derived elements bypass XML validation and
    directly use pixel-based validation.
    """
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
    mock_mcp.hierarchy = [
        {
            "text": "Sign Up",
            "bounds": "[100,200][300,300]",
            "resource-id": "btn_signup",
        }
    ]

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
        result = await node(state)

    # An incident SHOULD be opened because XML validation failed
    # and PIXEL_BYPASSED did not override it
    incident = result["last_execution_result"]["incident"]
    assert "Pre-execution validation failed" in incident["reason"]


@pytest.mark.asyncio
async def test_validator_failure_screenshot_mcp_error_handled_safely(
    mock_mcp, mock_context, temp_screenshot
):
    """Test that when take_screenshot fails on the session, the ValidatorNode does
    not crash -- the failure screenshot is simply absent.
    """
    mock_mcp.screenshot = RuntimeError("Error: ADB connection lost")
    mock_mcp.action_handler = lambda name, args: (
        (False, "Error: Target not found") if name == "click" else (True, "")
    )

    decisions = json.dumps([{"action": "tap", "coordinates": [105, 205]}])
    state = DummyState(structured_decisions=decisions, latest_screenshot=temp_screenshot)
    node = ValidatorNode(mock_context)

    with (
        patch("artemis.utils.image_diff.check_ui_change", return_value=False),
    ):
        result = await node(state)

    assert "last_execution_result" in result
    report = result["last_execution_result"]
    assert report["status"] == "failed"
    assert len(report["execution"]) == 1
    assert report["execution"][0]["action"] == "tap"
