# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Native hit points must survive the shared perception-to-action pipeline."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image
import pytest

from artemis.agents.explorer.screen_index import ScreenIndex
from artemis.context import ArtemisContext, DeviceContext, DevicePlatform
from artemis.drivers.ios.hierarchy import parse_hierarchy
from artemis.drivers.ios.xcode_driver import XcodeSimulatorDriver
from artemis.mcp.action_executor import McpActionExecutor
from artemis.mcp.actuators.ios import IosActuator
from artemis.mcp.observation import observe
from artemis.utils.visualization import (
    format_minimal_list_with_elements,
    format_minimal_list_with_points,
)


HIERARCHY = """Application bundle identifier: com.example.Test
Application, pid: 123, label: ' '
 Window, {{0.0, 0.0}, {400.0, 800.0}}, hitPoint: {200.0, 400.0}
  UIButton \"Continue\" {{100.0, 200.0}, {60.0, 30.0}}, hitPoint: {150.0, 220.0}, identifier: 'continueButton', activationBundleId: com.example.Test
"""


@pytest.mark.asyncio
async def test_ios_observation_preserves_hit_point_for_indexed_native_tap(tmp_path, monkeypatch):
    screenshot = tmp_path / "native.png"
    Image.new("RGB", (800, 1600)).save(screenshot)
    hierarchy = tmp_path / "native.txt"
    hierarchy.write_text(HIERARCHY)
    driver = XcodeSimulatorDriver("simulator-id")
    driver._session_key = "session"
    driver._bridge = SimpleNamespace(
        call=AsyncMock(
            return_value={"screenshotPath": str(screenshot), "hierarchyPath": str(hierarchy)}
        )
    )
    context = ArtemisContext(
        device=DeviceContext(mobile_platform=DevicePlatform.IOS, device_id="simulator-id")
    )
    context._active_driver = driver
    actuator = IosActuator(context)
    monkeypatch.setattr("artemis.mcp.observation.get_temp_dir", lambda name: tmp_path)

    observation, image = await observe(context, actuator.controller, settle_ms=0)
    assert observation.ok and observation.hierarchy_ok and image == screenshot.read_bytes()
    assert len(observation.elements) == 1
    element = observation.elements[0]
    assert element["center"] == element["hit_point"] == [300, 440]
    assert element["resource_id"] == "continueButton"
    assert element["activation_bundle_id"] == "com.example.Test"
    assert "Continue" in observation.elements_text

    state = SimpleNamespace(indexed_elements=observation.elements)
    executor = McpActionExecutor(context, actuator=actuator)
    name, arguments, _, _ = executor._translate("click", {"target": 1}, state)
    assert name == "click" and arguments["target"] == [375, 275]
    assert (await actuator.click(*arguments["target"])).ok
    command = driver._bridge.call.await_args.args[1]["interactionCommand"]
    assert command == "t 150.0000 220.0000 0.100"
    assert driver._bridge.call.await_args.args[1]["activationBundleId"] == "com.example.Test"


def test_ios_explorer_and_point_formatter_keep_native_hit_point():
    elements, _ = parse_hierarchy(HIERARCHY, 800, 1600)
    _, points, _ = format_minimal_list_with_points(elements, 800, 1600)
    assert points == [[300, 440]]
    index = ScreenIndex.from_hierarchy(elements, 800, 1600)
    match = index.search_text("Continue")[0].element
    assert match.center == (300, 440)
    assert match.resource_id == "continueButton"


@pytest.mark.parametrize("point", [None, [True, 20], [float("nan"), 20], [800, 200], [-1, 10], [1]])
def test_shared_formatter_keeps_android_centroid_for_missing_or_invalid_native_point(point):
    node = {
        "text": "Continue",
        "bounds": "[200,400][320,460]",
        "resource-id": "android:id/button1",
    }
    if point is not None:
        node["hit_point"] = point
    _, elements, _ = format_minimal_list_with_elements([node], 800, 1600)
    _, points, _ = format_minimal_list_with_points([node], 800, 1600)
    assert elements[0]["center"] == points[0] == [260, 430]
    assert elements[0]["resource_id"] == "android:id/button1"
    assert "hit_point" not in elements[0]
    assert ScreenIndex.from_hierarchy([node], 800, 1600).elements[0].center == (260, 430)


@pytest.mark.asyncio
async def test_ios_input_text_clear_exist_degrades_to_typing_with_honest_note(
    tmp_path, monkeypatch
):
    """iOS cannot whole-field clear; the actuator still types after focusing
    and reports the degradation instead of hard-failing the action."""
    screenshot = tmp_path / "native.png"
    Image.new("RGB", (800, 1600)).save(screenshot)
    hierarchy = tmp_path / "native.txt"
    hierarchy.write_text(HIERARCHY)
    driver = XcodeSimulatorDriver("simulator-id")
    driver._session_key = "session"
    driver._bridge = SimpleNamespace(
        call=AsyncMock(
            return_value={"screenshotPath": str(screenshot), "hierarchyPath": str(hierarchy)}
        )
    )
    context = ArtemisContext(
        device=DeviceContext(mobile_platform=DevicePlatform.IOS, device_id="simulator-id")
    )
    context._active_driver = driver
    actuator = IosActuator(context)
    monkeypatch.setattr("artemis.mcp.observation.get_temp_dir", lambda name: tmp_path)

    result = await actuator.input_text("apple.com", (500, 300), clear_exist=True)
    assert result.ok
    assert "without clearing" in result.message
    typed = driver._bridge.call.await_args.args[1]["interactionCommand"]
    assert typed.startswith("sender keyboard kbd")
