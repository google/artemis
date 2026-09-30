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

"""Pin existing action contracts and execution behavior while adding gestures."""

from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from langchain_core.utils.function_calling import convert_to_openai_tool
import pytest

from artemis.agents.validator.action_execution import exec_action
from artemis.agents.validator.execution_loop import _process_action
from artemis.mcp.action_server import build_action_server
from artemis.mcp.action_specs import OPERATOR_SHELL_ORDER, operator_shell_tool, tool_declaration
from artemis.mcp.action_types import ActionCode, ActionResult
from artemis.mcp.actuators import MockActuator
from artemis.utils.task_tree import format_action_clean, format_action_intent, format_result_clean


# Canonical JSON digests of the upstream 351ca84 fixtures, before perform_gesture.
# These pins are independent of the new gesture fixture and require no Git at test time.
LEGACY_SCHEMAS = {
    "operator": "51882b1d081f24fa0dc8e79cd74a94ed13ee3eccda84cdc233bbbd0515cf8ad0",
    "declarations": "ba06a25d8587769f9c518329706f6f1f15826a071a4db995f41c85a73c9d796f",
    "wire": "ea41842f9a3440dd2eef2ccb5e156c8a136fb13319e5c7751f3d0653daacdd85",
}


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@pytest.mark.asyncio
async def test_existing_action_schemas_and_descriptions_match_upstream():
    operator = {
        name: convert_to_openai_tool(operator_shell_tool(name))
        for name in OPERATOR_SHELL_ORDER
        if name != "perform_gesture"
    }
    declarations = {
        name.upper() + "_TOOL": dict(tool_declaration(name))
        for name in (
            "click",
            "click_sequence",
            "long_press",
            "input_text",
            "swipe",
            "press_key",
            "manage_app",
            "wait_for_delay",
        )
    }
    tools = await build_action_server(MockActuator()).list_tools()
    wire = {
        t.name: {
            "description": t.description,
            "inputSchema": t.inputSchema,
            "outputSchema": t.outputSchema,
        }
        for t in tools
        if t.name != "perform_gesture"
    }
    assert digest(operator) == LEGACY_SCHEMAS["operator"]
    assert digest(declarations) == LEGACY_SCHEMAS["declarations"]
    assert digest(wire) == LEGACY_SCHEMAS["wire"]


LEGACY_ACTIONS = [
    (
        {"action": "tap", "normalized_coordinates": [300, 400]},
        "click",
        {"target": [300, 400], "times": 1, "delay_ms": 100},
    ),
    (
        {"action": "swipe", "normalized_coordinates": [300, 400, 650, 550], "duration": 800},
        "swipe",
        {"start": [300, 400], "end": [650, 550], "duration_ms": 800},
    ),
    (
        {"action": "focus_and_input_text", "normalized_coordinates": [300, 400], "text": "hello"},
        "input_text",
        {"target": [300, 400], "text": "hello", "clear_exist": False},
    ),
    (
        {"action": "launch_app", "app_name": "settings"},
        "manage_app",
        {"action": "launch", "app_name": "settings"},
    ),
    ({"action": "press_key", "keycode": "KEYCODE_HOME"}, "press_key", {"key": "home"}),
    ({"action": "back"}, "press_key", {"key": "back"}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("action,name,wire", LEGACY_ACTIONS)
@pytest.mark.parametrize("ok", [True, False])
async def test_existing_action_results_do_not_add_history_fields(action, name, wire, ok):
    original = deepcopy(action)
    result = (
        ActionResult.success(name, "completed")
        if ok
        else ActionResult.failure(name, "outcome unknown", code=ActionCode.TIMEOUT)
    )
    session = Mock(call=AsyncMock(return_value=result))
    assert await exec_action(SimpleNamespace(device=None), session, action) == (
        (True, "") if ok else (False, "outcome unknown")
    )
    session.call.assert_awaited_once_with(name, wire)
    assert action == original
    assert format_result_clean({"status": "dispatched", "execution": [action]}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action", ["tap", "swipe", "focus_and_input_text", "launch_app", "press_key", "perform_gesture"]
)
@pytest.mark.parametrize("burst", [False, True])
@pytest.mark.parametrize("throws", [False, True])
async def test_retry_change_is_scoped_to_perform_gesture(action, burst, throws):
    execute = (
        AsyncMock(side_effect=TimeoutError("outcome unknown"))
        if throws
        else AsyncMock(return_value=(False, "outcome unknown"))
    )
    node = SimpleNamespace(_exec_action=execute)
    with (
        patch(
            "artemis.agents.validator.execution_loop._run_precondition_gate",
            AsyncMock(return_value=(True, None, "")),
        ),
        patch(
            "artemis.agents.validator.execution_loop._capture_live_screenshot",
            AsyncMock(return_value=None),
        ),
        patch("artemis.agents.validator.execution_loop.asyncio.sleep", AsyncMock()),
    ):
        outcome = await _process_action(
            node, Mock(), Mock(), {"action": action}, action, "", burst=burst
        )
    expected_attempts = 1 if burst or action in ("launch_app", "perform_gesture") else 2
    assert not outcome.success
    assert execute.await_count == expected_attempts
    assert outcome.action_item == {
        "action": action,
        "attempts": ["outcome unknown"] * expected_attempts,
    }


@pytest.mark.parametrize(
    "action,past,intent",
    [
        (
            {"action": "click", "target": [300, 400], "target_description": "button"},
            "Tapped 'button' (self-described) at [300, 400]",
            "tap 'button' (self-described) at [300, 400]",
        ),
        (
            {"action": "swipe", "coordinates": [300, 400, 650, 550], "duration": 800},
            "Swiped from [300, 400] to [650, 550] over 800ms",
            "swipe from [300, 400] to [650, 550] over 800ms",
        ),
        (
            {
                "action": "input_text",
                "text": "hello",
                "target": [300, 400],
                "target_description": "search field",
            },
            "Inputted 'hello' into 'search field' (self-described) at [300, 400]",
            "type 'hello' into 'search field' (self-described) at [300, 400]",
        ),
        ({"action": "press_key", "key": "home"}, "Pressed key 'home'", "press key 'home'"),
    ],
)
def test_existing_history_rendering_matches_upstream(action, past, intent):
    assert format_action_clean(action) == past
    assert format_action_intent(action) == intent
    assert format_result_clean({"status": "dispatched", "execution": [action]}) is None
    assert (
        format_result_clean(
            {"status": "failed", "execution": [{**action, "attempts": ["timeout"]}]}
        )
        == "Error: timeout"
    )
