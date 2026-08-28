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

import base64
from unittest.mock import AsyncMock, Mock, patch

from artemis.agents.validator.failure_analyzer import (
    _exec_click,
    _exec_click_sequence,
    _exec_input_text,
    _exec_long_press,
    _exec_manage_app,
    _exec_press_key,
    _exec_swipe,
    _exec_wait_for_delay,
)
from artemis.context import ArtemisContext
import pytest


class DummyState:
    def __init__(self, latest_screenshot=None):
        self.latest_screenshot = latest_screenshot
        self.indexed_points = []
        self.indexed_elements = []


@pytest.fixture
def mock_context(tmp_path):
    ctx = Mock(spec=ArtemisContext)
    ctx.device = Mock()
    ctx.device.device_width = 1080
    ctx.device.device_height = 2400
    ctx.data_engine = Mock()
    ctx.data_engine.base_dir = tmp_path
    ctx.data_engine.get_or_create_image.return_value = "dummy_image_name"
    ctx.data_engine.get_image_path.return_value = tmp_path / "dummy_image_name.jpg"
    return ctx


@pytest.fixture
def mock_controller():
    controller = Mock()
    mock_device_data = Mock()
    mock_device_data.base64 = base64.b64encode(b"mocked_screenshot").decode()
    mock_device_data.elements = []
    mock_device_data.width = 1080
    mock_device_data.height = 2400
    controller.get_screen_data = AsyncMock(return_value=mock_device_data)
    controller.get_ui_elements = AsyncMock(return_value=mock_device_data.elements)
    controller.tap_at = AsyncMock(return_value=Mock(error=None))
    controller.type_text = AsyncMock(return_value=True)
    controller.erase_text = AsyncMock(return_value=True)
    controller.swipe_coords = AsyncMock(return_value=None)
    controller.press_enter = AsyncMock(return_value=True)
    controller.go_back = AsyncMock(return_value=True)
    controller.go_home = AsyncMock(return_value=True)
    controller.press_key = AsyncMock(return_value=True)
    controller.terminate_app = AsyncMock(return_value=True)
    return controller


# ==========================================
# SLIDE 1: click
# ==========================================


@pytest.mark.asyncio
async def test_click_happy_path(mock_context, mock_controller):
    state = DummyState()
    outcome, img_bytes, shot_path, xml_list = await _exec_click(
        target=[500, 600],
        state=state,
        controller=mock_controller,
        ctx=mock_context,
        times=2,
        delay_ms=150,
    )
    assert "Clicked at [500, 600]" in outcome
    assert mock_controller.tap_at.call_count == 1
    mock_controller.tap_at.assert_any_call(540, 1440, times=2, delay_ms=150)


@pytest.mark.asyncio
async def test_click_input_validation(mock_context, mock_controller):
    state = DummyState()
    # Coordinates holding non-integer values
    outcome, _, _, _ = await _exec_click(
        target=["invalid", "invalid"],
        state=state,
        controller=mock_controller,
        ctx=mock_context,
    )
    assert "Error during click:" in outcome


@pytest.mark.asyncio
async def test_click_edge_cases(mock_context, mock_controller):
    state = DummyState()
    # Underflow/overflow coordinates should be clamped to valid boundary
    outcome, _, _, _ = await _exec_click(
        target=[-10, 1050],
        state=state,
        controller=mock_controller,
        ctx=mock_context,
    )
    assert "Clicked at [-10, 1050]" in outcome
    mock_controller.tap_at.assert_called_with(0, 2399, times=1, delay_ms=100)


@pytest.mark.asyncio
async def test_click_graceful_failure(mock_context, mock_controller):
    state = DummyState()
    mock_controller.tap_at.return_value = Mock(error="Click failed connection lost")
    outcome, _, _, _ = await _exec_click(
        target=[500, 600],
        state=state,
        controller=mock_controller,
        ctx=mock_context,
    )
    assert "Error executing click: Click failed connection lost" in outcome


# ==========================================
# SLIDE 2: click_sequence
# ==========================================


@pytest.mark.asyncio
async def test_click_sequence_happy_path(mock_context, mock_controller):
    state = DummyState()
    state.indexed_points = [[540, 1200]]  # Mapped from index 1 (1-indexed)
    outcome, img_bytes, shot_path, xml_list = await _exec_click_sequence(
        sequence=[1, [300, 400]],
        state=state,
        controller=mock_controller,
        ctx=mock_context,
    )
    assert "Sequence clicked successfully" in outcome
    assert mock_controller.tap_at.call_count == 2
    mock_controller.tap_at.assert_any_call(540, 1200)
    mock_controller.tap_at.assert_any_call(324, 960)


@pytest.mark.asyncio
async def test_click_sequence_input_validation(mock_context, mock_controller):
    state = DummyState()
    state.indexed_points = [[540, 1200], [270, 600]]

    # Subsequent elements can also be integers (mixed array of indexes and coordinates)
    outcome, _, _, _ = await _exec_click_sequence(
        sequence=[1, 2, [300, 400]],
        state=state,
        controller=mock_controller,
        ctx=mock_context,
    )
    assert "Sequence clicked successfully" in outcome
    assert mock_controller.tap_at.call_count == 3
    mock_controller.tap_at.assert_any_call(540, 1200)
    mock_controller.tap_at.assert_any_call(270, 600)
    mock_controller.tap_at.assert_any_call(324, 960)

    # Reset indexed_points since _exec_click_sequence overwrites them
    # via _capture_screenshot_and_parse_ui
    state.indexed_points = [[540, 1200], [270, 600]]

    # Index out of bounds
    outcome, _, _, _ = await _exec_click_sequence(
        sequence=[5, [300, 400]],
        state=state,
        controller=mock_controller,
        ctx=mock_context,
    )
    assert "Error during click sequence: Index 5 is out of range" in outcome


@pytest.mark.asyncio
async def test_click_sequence_edge_cases(mock_context, mock_controller):
    state = DummyState()
    # Coordinates in sequence outside range should be clamped
    outcome, _, _, _ = await _exec_click_sequence(
        sequence=[[0, 0], [1200, -50]],
        state=state,
        controller=mock_controller,
        ctx=mock_context,
    )
    assert "Sequence clicked successfully" in outcome
    mock_controller.tap_at.assert_any_call(0, 0)
    mock_controller.tap_at.assert_any_call(1079, 0)


@pytest.mark.asyncio
async def test_click_sequence_graceful_failure(mock_context, mock_controller):
    state = DummyState()
    mock_controller.tap_at.side_effect = [
        Mock(error=None),
        Mock(error="Connection error on step 2"),
    ]
    outcome, _, _, _ = await _exec_click_sequence(
        sequence=[[100, 200], [300, 400]],
        state=state,
        controller=mock_controller,
        ctx=mock_context,
    )
    assert "Error executing click at" in outcome
    assert "Connection error on step 2" in outcome


@pytest.mark.asyncio
async def test_click_sequence_stringified_formats(mock_context, mock_controller):
    state = DummyState()
    state.indexed_points = [[540, 1200]]
    # Test stringified coordinate formats commonly generated by LLMs:
    # 1. string representation of lists: ["[500, 280]", "[884, 362]"]
    outcome, _, _, _ = await _exec_click_sequence(
        sequence=["[500, 280]", "[884, 362]"],
        state=state,
        controller=mock_controller,
        ctx=mock_context,
    )
    assert "Sequence clicked successfully" in outcome
    mock_controller.tap_at.assert_any_call(540, 672)
    mock_controller.tap_at.assert_any_call(954, 868)

    # 2. comma-separated strings: ["500,280", "884,362"]
    outcome, _, _, _ = await _exec_click_sequence(
        sequence=["500,280", "884,362"],
        state=state,
        controller=mock_controller,
        ctx=mock_context,
    )
    assert "Sequence clicked successfully" in outcome

    # 3. whole sequence as a string
    outcome, _, _, _ = await _exec_click_sequence(
        sequence="[[500, 280], [884, 362]]",
        state=state,
        controller=mock_controller,
        ctx=mock_context,
    )
    assert "Sequence clicked successfully" in outcome


# ==========================================
# SLIDE 3: long_press
# ==========================================


@pytest.mark.asyncio
async def test_long_press_happy_path(mock_context, mock_controller):
    state = DummyState()
    outcome, img_bytes, shot_path, xml_list = await _exec_long_press(
        target=[500, 500],
        duration=1500,
        state=state,
        controller=mock_controller,
        ctx=mock_context,
    )
    assert "Long pressed at [500, 500]" in outcome
    mock_controller.tap_at.assert_called_once_with(
        540, 1200, long_press=True, long_press_duration=1500
    )


@pytest.mark.asyncio
async def test_long_press_input_validation(mock_context, mock_controller):
    state = DummyState()
    # Invalid target type
    outcome, _, _, _ = await _exec_long_press(
        target="not-a-list",
        duration=1500,
        state=state,
        controller=mock_controller,
        ctx=mock_context,
    )
    assert "Error during long press:" in outcome


# ==========================================
# SLIDE 4: input_text
# ==========================================


@pytest.mark.asyncio
async def test_input_text_happy_path(mock_context, mock_controller):
    state = DummyState()
    outcome, img_bytes, shot_path, xml_list = await _exec_input_text(
        text="hello",
        target=[500, 500],
        clear_exist=True,
        state=state,
        controller=mock_controller,
        ctx=mock_context,
    )
    assert "Executed typing 'hello'." in outcome
    # Focus goes through ensure_focus_at_coords (keyword-style tap), and typing
    # never re-clears: the clear already happened via erase_text.
    mock_controller.tap_at.assert_called_once_with(x=540, y=1200)
    mock_controller.erase_text.assert_called_once()
    mock_controller.type_text.assert_called_once_with("hello", clear_existing=False)


@pytest.mark.asyncio
async def test_input_text_input_validation(mock_context, mock_controller):
    state = DummyState()
    outcome, _, _, _ = await _exec_input_text(
        text="hello",
        target="invalid",
        clear_exist=True,
        state=state,
        controller=mock_controller,
        ctx=mock_context,
    )
    assert "Error during input text:" in outcome


# ==========================================
# SLIDE 5: swipe
# ==========================================


@pytest.mark.asyncio
async def test_swipe_happy_path_direction(mock_context, mock_controller):
    state = DummyState()
    outcome, img_bytes, shot_path, xml_list = await _exec_swipe(
        action="up",
        duration=400,
        state=state,
        controller=mock_controller,
        ctx=mock_context,
    )
    assert "Swipe completed successfully" in outcome
    mock_controller.swipe_coords.assert_called_once_with(648, 1680, 648, 720, 400)


@pytest.mark.asyncio
async def test_swipe_happy_path_coords(mock_context, mock_controller):
    state = DummyState()
    outcome, img_bytes, shot_path, xml_list = await _exec_swipe(
        action=[100, 900, 100, 100],
        duration=400,
        state=state,
        controller=mock_controller,
        ctx=mock_context,
    )
    assert "Swipe completed successfully" in outcome
    mock_controller.swipe_coords.assert_called_once_with(108, 2160, 108, 240, 400)


@pytest.mark.asyncio
async def test_swipe_input_validation(mock_context, mock_controller):
    state = DummyState()
    outcome, _, _, _ = await _exec_swipe(
        action="invalid_direction",
        duration=400,
        state=state,
        controller=mock_controller,
        ctx=mock_context,
    )
    assert "Error during swipe: Invalid direction:" in outcome


# ==========================================
# SLIDE 6: press_key
# ==========================================


@pytest.mark.asyncio
async def test_press_key_happy_path(mock_context, mock_controller):
    state = DummyState()
    outcome, img_bytes, shot_path, xml_list = await _exec_press_key(
        key="BACK",
        state=state,
        controller=mock_controller,
        ctx=mock_context,
    )
    assert "Executed key press 'BACK'." in outcome
    mock_controller.go_back.assert_called_once()


@pytest.mark.asyncio
async def test_press_key_input_validation(mock_context, mock_controller):
    state = DummyState()
    outcome, _, _, _ = await _exec_press_key(
        key="INVALID_KEY",
        state=state,
        controller=mock_controller,
        ctx=mock_context,
    )
    # Unknown keycodes are forwarded verbatim to the driver (historical adb_server
    # behavior); the mock driver accepts them, so the press reports success.
    assert "Executed key press 'INVALID_KEY'." in outcome
    mock_controller.press_key.assert_called_once_with("INVALID_KEY")


# ==========================================
# SLIDE 7: manage_app
# ==========================================


@pytest.mark.asyncio
async def test_manage_app_happy_path(mock_context, mock_controller):
    state = DummyState()
    with (
        # The device-call bodies moved into the actuator layer, which imports these
        # lazily from their source modules -- patch them at the source.
        patch("artemis.tools.mobile.launch_app.find_package") as mock_find_package,
        patch("artemis.utils.app_launch_utils.launch_app_with_retries") as mock_launch,
    ):
        mock_find_package.return_value = "com.google.android.youtube"
        mock_launch.return_value = (True, "")

        outcome, img_bytes, shot_path, xml_list = await _exec_manage_app(
            action="launch",
            app_name="YouTube",
            state=state,
            controller=mock_controller,
            ctx=mock_context,
        )
        assert "Launched app 'YouTube' (com.google.android.youtube) successfully." in outcome
        mock_find_package.assert_called_once_with(mock_context, "YouTube", use_fallback=False)
        mock_launch.assert_called_once_with(mock_context, "com.google.android.youtube")


@pytest.mark.asyncio
async def test_manage_app_input_validation(mock_context, mock_controller):
    state = DummyState()
    with patch("artemis.tools.mobile.launch_app.find_package") as mock_find_package:
        mock_find_package.return_value = None

        outcome, _, _, _ = await _exec_manage_app(
            action="launch",
            app_name="NonExistentApp",
            state=state,
            controller=mock_controller,
            ctx=mock_context,
        )
        assert "Error finding package for app: NonExistentApp" in outcome


# ==========================================
# SLIDE 8: wait_for_delay
# ==========================================


@pytest.mark.asyncio
async def test_wait_for_delay_happy_path(mock_context, mock_controller):
    state = DummyState()
    outcome, img_bytes, shot_path, xml_list = await _exec_wait_for_delay(
        time_in_ms=100,
        state=state,
        controller=mock_controller,
        ctx=mock_context,
    )
    assert "Waited for 100ms successfully." in outcome
