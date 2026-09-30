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

"""Multi-touch semantics, model reachability, and no-replay transport regressions."""

import asyncio
import json
from unittest.mock import AsyncMock, Mock, patch

import pytest
from pydantic import ValidationError
from artemis.clients.accessibility_client import AccessibilityClient
from artemis.mcp.gestures import validate_phases
from artemis.mcp.action_names import to_canonical_call
from artemis.mcp.action_specs import operator_shell_tool, tool_declaration
from artemis.mcp.action_executor import McpActionExecutor, _ArgError
from artemis.mcp.action_session import ActionSession
from artemis.mcp.action_server import build_action_server
from artemis.mcp.actuators.mock import MockActuator
from artemis.mcp.action_types import ActionCode


def phase(points=None, duration=500):
    return {
        "duration_ms": duration,
        "pointers": points
        or [{"id": i, "path": [[x, 300], [x, 700]]} for i, x in enumerate([300, 500, 700])],
    }


def drag():
    return [
        phase([{"id": 0, "path": [[300, 400]]}], 700),
        phase([{"id": 0, "path": [[300, 400], [980, 400]]}], 800),
        phase([{"id": 0, "path": [[980, 400]]}], 1000),
    ]


def curved_drag():
    phases = drag()
    phases[1]["pointers"][0]["control_points"] = [[450, 300], [800, 300]]
    return phases


def long_press_drag(delay=0):
    return [
        {
            "kind": "long_press_drag",
            "start": [300, 400],
            "end": [650, 550],
            "duration_ms": 800,
            "release_delay_ms": delay,
        }
    ]


@pytest.mark.parametrize("delay", [0, 1200, 5000])
def test_long_press_drag_keeps_endpoint_hold_distinct_from_movement(delay):
    from artemis.mcp.gestures import gesture_duration_bound_ms

    plan = long_press_drag(delay)
    assert validate_phases(plan) == plan
    assert gesture_duration_bound_ms(plan) == 5000 + 800 + delay
    plan[0]["hold_ms"] = 700
    assert gesture_duration_bound_ms(validate_phases(plan)) == 700 + 800 + delay
    schema = json.dumps(tool_declaration("perform_gesture").parameters)
    assert '"release_delay_ms"' in schema and '"long_press_drag"' in schema


@pytest.mark.parametrize(
    "field,value",
    [
        ("release_delay_ms", -1),
        ("release_delay_ms", 5001),
        ("release_delay_ms", True),
        ("release_delay_ms", 0.5),
        ("hold_ms", 0),
        ("direction", "right"),
        ("start", [1001, 0]),
        ("end", [-1, 400]),
        ("end", [400]),
        ("duration_ms", 0),
        ("control_points", [[100, 200]]),
        ("control_points", [[100, 200], [300, 1001]]),
    ],
)
def test_invalid_long_press_drag_rejected(field, value):
    plan = long_press_drag()
    plan[0][field] = value
    with pytest.raises(ValidationError):
        validate_phases(plan)


def test_long_press_drag_cannot_mix_with_manual_phases():
    with pytest.raises(ValidationError):
        validate_phases(long_press_drag() + drag())
    with pytest.raises(ValidationError):
        validate_phases(drag() + long_press_drag())


@pytest.mark.parametrize("endpoint", [[650, 550], [400, 200], [20, 400], [980, 400]])
def test_long_press_drag_keeps_caller_endpoint_and_bezier_controls(endpoint):
    plan = long_press_drag(1200)
    plan[0]["end"] = endpoint
    plan[0]["control_points"] = [[400, 250], [600, 250]]
    assert validate_phases(plan) == plan
    executor = McpActionExecutor(Mock(), actuator=Mock())
    _, wire, _, _ = executor._translate(
        "perform_gesture", {"phases": plan, "target_description": "drag selected object"}, Mock()
    )
    assert wire["phases"] == plan


def test_long_press_drag_requires_an_explicit_endpoint():
    plan = long_press_drag()
    del plan[0]["end"]
    with pytest.raises(ValidationError):
        validate_phases(plan)


def test_curve_controls_are_preserved_without_host_sampling():
    assert validate_phases(curved_drag()) == curved_drag()
    schema = json.dumps(tool_declaration("perform_gesture").parameters)
    assert "control_points" in schema
    assert "Bezier" in operator_shell_tool("perform_gesture").description


@pytest.mark.parametrize(
    "controls",
    [
        [],
        [[1, 2]],
        [[1, 2]] * 3,
        [[-1, 2], [3, 4]],
        [[1.5, 2], [3, 4]],
        [[True, 2], [3, 4]],
        [[1, 2, 3], [3, 4]],
    ],
)
def test_invalid_curve_controls_rejected(controls):
    phases = curved_drag()
    phases[1]["pointers"][0]["control_points"] = controls
    with pytest.raises(ValidationError):
        validate_phases(phases)


@pytest.mark.parametrize("path", [[[300, 400]], [[300, 400], [500, 400], [980, 400]]])
def test_curve_requires_only_start_and_end(path):
    phases = curved_drag()
    phases[1]["pointers"][0]["path"] = path
    with pytest.raises(ValidationError):
        validate_phases(phases)


def test_three_fingers_and_continuous_drag_are_valid():
    assert len(validate_phases([phase()])[0]["pointers"]) == 3
    assert validate_phases(drag()) == drag()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p[0]["pointers"][0]["path"][0].__setitem__(0, True),
        lambda p: p[0]["pointers"][0]["path"][0].__setitem__(0, 1.5),
        lambda p: p[0]["pointers"][0]["path"][0].__setitem__(0, "30"),
        lambda p: p[0]["pointers"][0]["path"][0].__setitem__(0, 1001),
        lambda p: p[0]["pointers"][0].__setitem__("id", 1),
        lambda p: p[0].__setitem__("duration_ms", 0),
        lambda p: p[0].__setitem__("duration_ms", 5001),
        lambda p: p[0].__setitem__("unexpected", True),
        lambda p: p[0]["pointers"][0].__setitem__("path", []),
    ],
)
def test_invalid_input_is_rejected(mutate):
    p = [phase()]
    mutate(p)
    with pytest.raises(ValidationError):
        validate_phases(p)


@pytest.mark.parametrize("change", ["jump", "ids", "budget"])
def test_continuity_and_total_budget(change):
    p = drag()
    if change == "jump":
        p[1]["pointers"][0]["path"][0][0] = 500
    elif change == "ids":
        p[1]["pointers"][0]["id"] = 1
    else:
        p = [phase([{"id": 0, "path": [[500, 500]]}], 5000)] * 7
    with pytest.raises(ValidationError):
        validate_phases(p)


def test_model_schema_has_nested_paths_and_shared_semantics():
    shell = operator_shell_tool("perform_gesture")
    args = shell.args_schema(phases=drag(), target_description="launcher icon")
    assert validate_phases(args.phases) == drag()
    declaration = tool_declaration("perform_gesture")
    encoded = json.dumps(declaration.parameters)
    assert "$ref" not in encoded and '"duration_ms"' in encoded and '"pointers"' in encoded
    assert "maintain contact" in shell.description and "Observe the screen" in shell.description


@pytest.mark.parametrize("plan", [drag(), curved_drag(), long_press_drag(), long_press_drag(1200)])
def test_pro_and_flash_lower_to_the_same_plan_without_early_execution(plan):
    from artemis.agents.operator.operator import OperatorNode

    node = OperatorNode(Mock())
    arguments = {"phases": plan, "target_description": "launcher icon"}
    decisions, error = node._translate_and_validate_tool(
        {"name": "perform_gesture", "args": arguments}, Mock()
    )
    assert error is None and decisions[0]["target_description"] == "launcher icon"
    pro_name, pro_args = to_canonical_call(decisions[0])
    executor = McpActionExecutor(Mock(), actuator=Mock())
    name, wire, _, recorded = executor._translate("perform_gesture", arguments, Mock())
    assert (name, wire) == (pro_name, pro_args)
    assert recorded["target_description"] == "launcher icon"
    assert "target_description" not in wire
    with pytest.raises(_ArgError):
        executor._translate("perform_gesture", {"phases": drag()}, Mock())


@pytest.mark.asyncio
@pytest.mark.parametrize("plan", [drag(), long_press_drag(), long_press_drag(1200)])
async def test_mcp_executes_all_phases_as_one_action(plan):
    actuator = MockActuator()
    session = ActionSession(build_action_server(actuator))
    await session.start()
    try:
        result = await session.call("perform_gesture", {"phases": plan})
        assert result.ok, result
        actions = [a for a in actuator.action_history if a["action"] == "perform_gesture"]
        assert actions == [{"action": "perform_gesture", "phases": plan}]
    finally:
        await session.aclose()


@pytest.mark.asyncio
async def test_missing_or_incomplete_helper_does_not_succeed():
    actuator = MockActuator()
    actuator.controller.perform_gesture = AsyncMock(
        return_value={"success": False, "status": "unsupported", "error": "old helper"}
    )
    assert (await actuator.perform_gesture([phase()])).code == ActionCode.UNSUPPORTED
    actuator.controller.perform_gesture.return_value = {
        "success": True,
        "status": "completed",
        "release_confirmed": False,
    }
    assert not (await actuator.perform_gesture([phase()])).ok


def client_with_capabilities():
    client = AccessibilityClient("test-device")
    client._http = Mock(
        side_effect=[
            json.dumps(
                {"capabilities": ["perform_gesture"], "gesture_continuation": True}
            ).encode(),
            json.dumps(
                {"success": True, "status": "completed", "release_confirmed": True}
            ).encode(),
        ]
    )
    return client


def test_helper_receives_one_plan_with_duration_aware_timeout():
    client = client_with_capabilities()
    assert client.perform_gesture(drag(), "request-1")["success"]
    args, kwargs = client._http.call_args
    assert args == (
        "/action",
        {"cmd": "perform_gesture", "request_id": "request-1", "phases": drag()},
    )
    assert kwargs["timeout"] == 12.5


def test_helper_probe_prevents_actions_on_old_apk():
    client = client_with_capabilities()
    client._http.side_effect = [b"{}"]
    assert client.perform_gesture(drag(), "request-1")["status"] == "unsupported"
    assert client._http.call_count == 1


def test_old_helper_rejects_curve_before_any_touch():
    client = client_with_capabilities()
    assert client.perform_gesture(curved_drag(), "curve")["status"] == "unsupported"
    assert client._http.call_count == 1


def test_old_helper_rejects_long_press_drag_without_input():
    client = client_with_capabilities()
    assert client.perform_gesture(long_press_drag(), "long-press-drag")["status"] == "unsupported"
    assert client._http.call_count == 1


def test_long_press_drag_transmitted_without_host_geometry_and_with_full_timeout():
    client = client_with_capabilities()
    client._http.side_effect = [
        b'{"capabilities":["perform_gesture","gesture_long_press_drag"],"gesture_continuation":true}',
        b'{"success":true,"status":"completed","release_confirmed":true}',
    ]
    assert client.perform_gesture(long_press_drag(1200), "long-press-drag")["success"]
    assert client._http.call_args.args[1]["phases"] == long_press_drag(1200)
    assert client._http.call_args.kwargs["timeout"] == 17


def test_curved_long_press_drag_checks_capability_before_input():
    client = client_with_capabilities()
    client._http.side_effect = [
        b'{"capabilities":["perform_gesture","gesture_long_press_drag"],"gesture_continuation":true}',
    ]
    plan = long_press_drag()
    plan[0]["control_points"] = [[400, 250], [600, 250]]
    assert client.perform_gesture(plan, "curve")["status"] == "unsupported"
    assert client._http.call_count == 1


def test_curved_long_press_drag_reaches_helper_with_interior_endpoint():
    client = client_with_capabilities()
    client._http.side_effect = [
        b'{"capabilities":["perform_gesture","gesture_long_press_drag","gesture_cubic_bezier"],"gesture_continuation":true}',
        b'{"success":true,"status":"completed","release_confirmed":true}',
    ]
    plan = long_press_drag(1000)
    plan[0]["control_points"] = [[400, 250], [600, 250]]
    assert client.perform_gesture(plan, "curve")["success"]
    assert client._http.call_args.args[1]["phases"] == plan


@pytest.mark.parametrize("plan", [drag(), long_press_drag()])
def test_helper_without_continuation_rejects_before_input(plan):
    client = client_with_capabilities()
    client._http.side_effect = [
        b'{"capabilities":["perform_gesture","gesture_long_press_drag"],"gesture_continuation":false}',
    ]
    assert client.perform_gesture(plan, "android7")["status"] == "unsupported"
    assert client._http.call_count == 1


def test_curve_controls_reach_native_helper_unchanged():
    client = client_with_capabilities()
    client._http.side_effect = [
        b'{"capabilities":["perform_gesture","gesture_cubic_bezier"],"gesture_continuation":true}',
        b'{"success":true,"status":"completed","release_confirmed":true}',
    ]
    assert client.perform_gesture(curved_drag(), "curve")["success"]
    assert client._http.call_args.args[1]["phases"] == curved_drag()


def test_lost_action_response_is_not_replayed():
    client = client_with_capabilities()
    client._http.side_effect = [
        b'{"capabilities":["perform_gesture"],"gesture_continuation":true}',
        TimeoutError(),
    ]
    with pytest.raises(TimeoutError):
        client.perform_gesture(drag(), "request-1")
    assert client._http.call_count == 2


@pytest.mark.asyncio
async def test_driver_cancel_targets_the_same_request_and_waits_for_release():
    import threading
    from artemis.drivers.android.adb_driver import AndroidAdbDriver

    started, released = threading.Event(), threading.Event()
    ids = []

    def execute(phases, request_id):
        ids.append(request_id)
        started.set()
        assert released.wait(5)
        return {"success": False, "status": "cancelled", "release_confirmed": True}

    def cancel(request_id):
        assert request_id == ids[0]
        released.set()
        return {"status": "cancelling"}

    client = Mock()
    client.perform_gesture.side_effect = execute
    client.cancel_gesture.side_effect = cancel
    driver = AndroidAdbDriver("test-device", Mock())
    with patch("artemis.clients.accessibility_client.AccessibilityClient", return_value=client):
        task = asyncio.create_task(driver.perform_gesture(drag()))
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert released.is_set() and client.perform_gesture.call_count == 1


@pytest.mark.asyncio
async def test_gesture_uses_existing_result_without_mutating_history():
    from copy import deepcopy
    from types import SimpleNamespace
    from artemis.agents.validator.action_execution import exec_action

    actuator = MockActuator()
    result = await actuator.perform_gesture(drag())
    assert result.ok and result.code == ActionCode.OK
    assert result.message == "Gesture completed. Observe the screen to verify the intended effect."
    assert "release_confirmed" in result.detail
    assert "release_confirmed" not in result.message
    session = Mock(call=AsyncMock(return_value=result))
    action = {"action": "perform_gesture", "phases": drag()}
    original = deepcopy(action)
    assert await exec_action(SimpleNamespace(device=None), session, action) == (True, "")
    assert action == original


@pytest.mark.asyncio
async def test_continuous_gesture_is_not_automatically_retried_after_unknown_outcome():
    from types import SimpleNamespace
    from artemis.agents.validator.execution_loop import _process_action

    node = SimpleNamespace(_exec_action=AsyncMock(return_value=(False, "outcome unknown")))
    with (
        patch(
            "artemis.agents.validator.execution_loop._run_precondition_gate",
            AsyncMock(return_value=(True, None, "")),
        ),
        patch(
            "artemis.agents.validator.execution_loop._capture_live_screenshot",
            AsyncMock(return_value=None),
        ),
    ):
        outcome = await _process_action(
            node,
            Mock(),
            Mock(),
            {"action": "perform_gesture", "phases": drag()},
            "perform_gesture",
            "",
            burst=False,
        )
    assert not outcome.success
    assert node._exec_action.await_count == 1
