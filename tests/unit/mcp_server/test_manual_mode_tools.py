"""mobile_observe / mobile_act / mobile_session_end, exercised against a mock device."""

import pytest

from artemis.mcp.action_executor import McpActionExecutor
from artemis.mcp.actuators.mock import MockActuator
from mcp_server.manual_session import ManualSessionRegistry
import mcp_server.tools.manual_mode as manual_mode

pytestmark = pytest.mark.asyncio


def _mock_executor_factory():
    """One MockActuator per device serial, wrapped in a real McpActionExecutor."""
    actuators: dict[str | None, MockActuator] = {}

    def factory(device_serial: str | None) -> McpActionExecutor:
        actuator = MockActuator()
        actuators[device_serial] = actuator
        return McpActionExecutor(actuator.ctx, actuator=actuator, agent_name="manual")

    factory.actuators = actuators
    return factory


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch):
    """Each test gets its own registry/mock devices instead of the module singleton."""
    factory = _mock_executor_factory()
    registry = ManualSessionRegistry(executor_factory=factory)
    monkeypatch.setattr(manual_mode, "_registry", registry)
    yield registry
    for key in list(registry._sessions):
        registry.end(key)


def _seed_indexed_element(session) -> None:
    """The repo's own MockActuator screen fails hierarchy parsing (pre-existing,
    unrelated to this feature -- see the executing-plans review note), so index
    targets are seeded directly, the same way
    tests/unit/mcp/test_action_executor_semantics.py builds its indexed state.
    """
    session.indexed_elements = [
        {
            "index": 1,
            "center": [540, 1248],
            "text": "Wi-Fi",
            "bounds": [40, 1152, 1040, 1344],
            "class": "android.widget.TextView",
            "resource_id": "android:id/title",
            "is_ocr": False,
        }
    ]
    session.indexed_points = [el["center"] for el in session.indexed_elements]


async def test_mobile_observe_returns_screenshot_and_elements():
    result = await manual_mode.mobile_observe(device_serial="dev1")
    assert result["status"] == "success"
    assert result["device_serial"] == "dev1"
    assert isinstance(result["elements_text"], str)
    assert result["width"] and result["height"]


async def test_mobile_act_rejects_disallowed_action():
    result = await manual_mode.mobile_act(action="ask_explorer", args={}, device_serial="dev1")
    assert result["status"] == "error"
    assert "not available in manual mode" in result["error"]


async def test_mobile_act_click_by_index_after_observe():
    observed = await manual_mode.mobile_observe(device_serial="dev1")
    assert observed["status"] == "success"

    session = manual_mode._registry.get_or_create("dev1")
    _seed_indexed_element(session)

    result = await manual_mode.mobile_act(action="click", args={"target": 1}, device_serial="dev1")
    assert result["status"] == "success"
    assert result["device_serial"] == "dev1"


async def test_mobile_act_invalid_index_surfaces_executor_error():
    await manual_mode.mobile_observe(device_serial="dev1")
    session = manual_mode._registry.get_or_create("dev1")
    _seed_indexed_element(session)

    result = await manual_mode.mobile_act(
        action="click", args={"target": 9999}, device_serial="dev1"
    )
    assert result["status"] == "error"
    assert "Invalid target index" in result["message"]


async def test_mobile_session_end_is_idempotent():
    await manual_mode.mobile_observe(device_serial="dev1")
    first = manual_mode.mobile_session_end(device_serial="dev1")
    second = manual_mode.mobile_session_end(device_serial="dev1")
    assert first == {"status": "success", "device_serial": "dev1", "ended": True}
    assert second == {"status": "success", "device_serial": "dev1", "ended": False}
