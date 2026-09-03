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

"""Actuator layer tests: coordinate conversion, message parity, driver dispatch."""

import pytest

from artemis.mcp.action_manifest import DEVICE_ACTIONS
from artemis.mcp.action_types import ActionCode
from artemis.mcp.actuators import Actuator, AdbActuator, MockActuator


@pytest.fixture
def actuator() -> MockActuator:
    return MockActuator(width=1000, height=2000)


def test_mock_actuator_satisfies_protocol(actuator):
    assert isinstance(actuator, Actuator)
    assert isinstance(actuator, AdbActuator)
    assert actuator.capabilities() == DEVICE_ACTIONS
    assert actuator.extensions() == []


@pytest.mark.asyncio
async def test_click_converts_normalized_to_pixels(actuator):
    res = await actuator.click(500, 500)
    assert res.ok and res.code is ActionCode.OK
    # Historical wording is part of the contract: transcripts and traces assert on it.
    assert res.message == "Clicked at [500, 500] (normalized) successfully."
    assert res.normalized_coordinates == [500, 500]
    assert actuator.action_history[-1] == {
        "action": "tap",
        "x": 500,
        "y": 1000,
        "times": 1,
        "duration_ms": 100,
    }


@pytest.mark.asyncio
async def test_click_sequence_taps_each_point(actuator):
    res = await actuator.click_sequence([(100, 100), (900, 900)], delay_ms=1)
    assert res.ok
    assert res.message == (
        "Sequence clicked successfully: Tapped at [100, 200]; Tapped at [900, 1800]"
    )
    taps = [h for h in actuator.action_history if h["action"] == "tap"]
    assert [(t["x"], t["y"]) for t in taps] == [(100, 200), (900, 1800)]


@pytest.mark.asyncio
async def test_long_press_message_and_duration(actuator):
    res = await actuator.long_press(250, 750, duration_ms=1500)
    assert res.ok
    assert res.message == ("Long pressed at [250, 750] (normalized) for 1500ms successfully.")
    assert res.duration_ms == 1500


@pytest.mark.asyncio
async def test_swipe_reports_normalized_endpoints(actuator):
    res = await actuator.swipe((500, 800), (500, 200), 400)
    assert res.ok
    assert res.message == "Swipe completed successfully. Swiped from [500, 800] to [500, 200]."
    assert res.normalized_coordinates == [500, 800, 500, 200]


@pytest.mark.asyncio
async def test_press_key_forwards_unknown_keycode_to_driver(actuator):
    # Arbitrary keycodes (KEYCODE_* names, numeric codes) pass through to the driver,
    # matching the historical adb_server behavior of accepting any Android key event.
    res = await actuator.press_key("KEYCODE_DPAD_DOWN")
    assert res.ok
    assert res.message == "Executed key press 'KEYCODE_DPAD_DOWN'."
    assert actuator.action_history[-1]["action"] == "press_key"


@pytest.mark.asyncio
async def test_press_key_rejects_empty_key(actuator):
    res = await actuator.press_key("")
    assert not res.ok
    assert res.code is ActionCode.INVALID_ARGS


@pytest.mark.asyncio
async def test_input_text_append_moves_cursor_and_preserves_text(actuator, monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _instant_sleep)
    res = await actuator.input_text("hello", (500, 300), clear_exist=False)
    assert res.ok
    assert res.message == "Executed typing 'hello'."
    actions = [h["action"] for h in actuator.action_history]
    # Append semantics: focus tap, cursor-to-end (KEYCODE_MOVE_END), then type without
    # clearing — matching the historical adb_server focus_and_input_text contract.
    assert actions == ["tap", "press_key", "input_text"]
    typed = actuator.action_history[-1]
    assert typed["clear_existing"] is False
    cursor = actuator.action_history[-2]
    assert cursor["key"] == "123"


@pytest.mark.asyncio
async def test_input_text_clear_erases_then_types_without_reclearing(actuator, monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _instant_sleep)
    res = await actuator.input_text("hello", (500, 300), clear_exist=True)
    assert res.ok
    actions = [h["action"] for h in actuator.action_history]
    # The controller's full clear runs as a shell keyevent batch on this driver.
    assert actions == ["tap", "execute_shell", "input_text"]
    assert actuator.action_history[-1]["clear_existing"] is False


async def _instant_sleep(_delay, *args, **kwargs):
    return None


@pytest.mark.asyncio
async def test_wait_for_delay(actuator):
    res = await actuator.wait_for_delay(1)
    assert res.ok
    assert res.message == "Waited for 1ms successfully."


def test_partial_capabilities_are_reported():
    partial = MockActuator(capabilities=frozenset({"click_sequence"}))
    assert partial.capabilities() == frozenset({"click_sequence"})


@pytest.mark.asyncio
async def test_observation_primitives(actuator):
    shot = await actuator.take_screenshot()
    assert isinstance(shot, str) and len(shot) > 0
    data = await actuator.get_screen_data()
    assert data.width == 1000 and data.height == 2000
