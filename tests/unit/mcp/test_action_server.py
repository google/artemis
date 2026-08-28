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

"""End-to-end tests of the unified action server over the in-memory transport."""

import pytest

from artemis.mcp.action_manifest import (
    ActuatorContractError,
    DEVICE_ACTIONS,
    INTERNAL_ACTIONS,
    REQUIRED_ACTIONS,
    ExtensionTool,
)
from artemis.mcp.action_server import build_action_server
from artemis.mcp.action_session import ActionSession
from artemis.mcp.actuators import MockActuator

pytestmark = pytest.mark.asyncio


async def _session_for(actuator) -> ActionSession:
    return await ActionSession(build_action_server(actuator)).start()


async def test_click_end_to_end():
    actuator = MockActuator(width=1000, height=2000)
    session = await _session_for(actuator)
    try:
        res = await session.call("click", {"target": [500, 500]})
        assert res.ok
        assert res.message == "Clicked at [500, 500] (normalized) successfully."
        assert actuator.action_history[-1]["action"] == "tap"
        assert actuator.action_history[-1]["x"] == 500
        assert actuator.action_history[-1]["y"] == 1000
    finally:
        await session.aclose()


async def test_device_refusal_is_not_a_protocol_error():
    """A failed action reaches the caller as ok=False, never as an exception."""
    actuator = MockActuator()
    session = await _session_for(actuator)
    try:
        res = await session.call("press_key", {"key": ""})
        assert not res.ok
        assert res.code.value == "INVALID_ARGS"
        assert res.message == "Error executing key press ''."
    finally:
        await session.aclose()


async def test_partial_actuator_hides_optional_tools():
    """An unimplemented optional tool does not exist anywhere on the surface."""
    minimal = MockActuator(capabilities=REQUIRED_ACTIONS | INTERNAL_ACTIONS)
    session = await _session_for(minimal)
    try:
        listed = {t.name for t in (await session._session.list_tools()).tools}
        assert "click_sequence" in listed
        assert "observe_screen" in listed
        for absent in ("click", "swipe", "manage_app", "wait_for_text", "open_link"):
            assert absent not in listed
    finally:
        await session.aclose()


async def test_missing_required_action_fails_at_build_time():
    broken = MockActuator(capabilities=DEVICE_ACTIONS - {"click_sequence"})
    with pytest.raises(ActuatorContractError, match="click_sequence"):
        build_action_server(broken)


async def test_extension_tool_is_served_and_callable():
    async def calibrate(sweep_speed: int = 1) -> str:
        return f"calibrated at speed {sweep_speed}"

    ext = ExtensionTool(
        name="calibrate_arm",
        description=(
            "Runs the robot arm's touch calibration sweep. Use before the first "
            "physical action of a session, or after any missed tap."
        ),
        input_schema={"type": "object", "properties": {}},
        handler=calibrate,
        targets=frozenset({"flash"}),
    )
    actuator = MockActuator(extensions=[ext])
    session = await _session_for(actuator)
    try:
        listed = {t.name for t in (await session._session.list_tools()).tools}
        assert "calibrate_arm" in listed
        result = await session.call_raw("calibrate_arm", {"sweep_speed": 3})
        text = next(b.text for b in result.content if b.type == "text")
        assert text == "calibrated at speed 3"
    finally:
        await session.aclose()


async def test_extension_name_collision_fails_at_build_time():
    async def bogus() -> str:
        return "x"

    ext = ExtensionTool(
        name="click",
        description="An extension improperly reusing a canonical tool name, which "
        "must be rejected at build time.",
        input_schema={"type": "object"},
        handler=bogus,
    )
    with pytest.raises(ActuatorContractError, match="collides"):
        build_action_server(MockActuator(extensions=[ext]))


async def test_unknown_tool_raises_protocol_error():
    session = await _session_for(MockActuator())
    try:
        with pytest.raises(RuntimeError, match="not_a_tool"):
            await session.call_raw("not_a_tool", {})
    finally:
        await session.aclose()


async def test_screenshot_and_hierarchy_helpers():
    session = await _session_for(MockActuator(width=1000, height=2000))
    try:
        b64 = await session.screenshot_b64()
        assert isinstance(b64, str) and len(b64) > 0
        elements = await session.ui_hierarchy()
        assert elements is not None
    finally:
        await session.aclose()


async def test_structured_content_carries_real_status():
    """The wire result exposes ok/code so no consumer ever sniffs substrings again."""
    session = await _session_for(MockActuator())
    try:
        raw = await session.call_raw("click", {"target": [10, 10]})
        sc = raw.structuredContent
        assert sc["ok"] is True
        assert sc["code"] == "OK"
        assert sc["action"] == "click"
        assert raw.isError is False
    finally:
        await session.aclose()
