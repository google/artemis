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

"""A Flash multi-action turn resolves every index against one element list.

Defect: each action's post-observation replaced ``state.indexed_elements``
and the executor resolved index targets against the state at call time, so
``click(3)`` then ``click(5)`` decided on one screen sent the second tap to
whatever the screen after the first click listed as ``[5]``. The runner now
snapshots the list once per turn and threads it through
``McpActionExecutor.execute(index_elements=...)``; the state's list is still
refreshed after every action (that is what the next observation shows).
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from langchain_core.messages import ToolMessage

from artemis.agents.flash.runner import FlashRunner, _TurnRecord
from artemis.context import ArtemisContext
from artemis.graph.state import State
from artemis.mcp.action_executor import McpActionExecutor, _ArgError
from artemis.mcp.action_types import ActionResult, ObserveResult


def _el(index: int, text: str, cx: int, cy: int) -> dict:
    return {
        "index": index,
        "center": [cx, cy],
        "text": text,
        "bounds": [cx - 100, cy - 50, cx + 100, cy + 50],
        "class": "android.widget.TextView",
        "resource_id": f"id/{text.lower().replace(' ', '_')}",
        "is_ocr": False,
    }


# The list of the observation the model decided on (1080x2400 device).
ORIGINAL = [
    _el(1, "Home", 540, 200),
    _el(2, "Search", 540, 600),
    _el(3, "Wi-Fi", 540, 1000),
    _el(4, "Bluetooth", 540, 1400),
    _el(5, "Display", 540, 1800),
]
# The screen after the first click: fewer elements, different numbering.
REFRESHED = [_el(1, "Back", 100, 100), _el(2, "Wi-Fi toggle", 900, 300)]


class _FakeSession:
    """An action session whose observations replay a scripted list sequence."""

    started = True

    def __init__(self, observations: list[list[dict]]):
        self.calls: list[tuple[str, dict]] = []
        self._observations = list(observations)

    async def call(self, name, args):
        self.calls.append((name, dict(args)))
        return ActionResult.success(name, f"{name} dispatched")

    async def observe(self, settle_ms=0):
        elements = self._observations.pop(0) if self._observations else []
        return ObserveResult(
            ok=True,
            elements_text="\n".join(f"[{e['index']}] {e['text']}" for e in elements),
            elements=elements,
            hierarchy_ok=True,
        )


@pytest.fixture
def mock_context():
    ctx = Mock(spec=ArtemisContext)
    ctx.llm_config = Mock()
    llm_cfg = Mock()
    llm_cfg.model = "gemini-2.5-flash"
    llm_cfg.temperature = 0.1
    ctx.llm_config.get_agent.return_value = llm_cfg
    ctx.device = Mock()
    ctx.device.device_width = 1080
    ctx.device.device_height = 2400
    ctx.data_engine = None
    ctx.adb_client = None
    ctx.driver = Mock()
    return ctx


def _runner(mock_context, observations):
    with (
        patch("artemis.controllers.unified_controller.get_driver"),
        patch("artemis.agents.flash.runner.VisualStepSummarizer"),
    ):
        runner = FlashRunner(mock_context, goal="Open Wi-Fi and Display")
    runner.summarizer = None
    runner.executor._session = _FakeSession(observations)
    # No device: the executor's observation already refreshed the state.
    runner._capture_post_screenshot = AsyncMock(return_value=None)
    return runner


def _state(elements):
    state = State(initial_goal="g")
    state.indexed_elements = list(elements)
    state.indexed_points = [e["center"] for e in elements]
    return state


def _click(index, tc_id):
    return {"name": "click", "args": {"target": index}, "id": tc_id}


# --- Executor: the explicit list wins over the state ---------------------------------


def _executor():
    ctx = Mock()
    ctx.device.device_width = 1080
    ctx.device.device_height = 2400
    actuator = Mock()
    actuator.controller = Mock()
    return McpActionExecutor(ctx, actuator=actuator)


def test_resolve_index_uses_the_given_list_and_falls_back_to_the_state():
    executor = _executor()
    state = _state(REFRESHED)

    # None -> the state's current list (Pro operator / validator / explorer callers).
    point, recorded = executor._resolve_index(2, "click", state)
    assert point == [833, 125] and recorded["target_text"] == "Wi-Fi toggle"

    # An explicit list: index 5 exists only there.
    point, recorded = executor._resolve_index(5, "click", state, index_elements=ORIGINAL)
    assert point == [500, 750] and recorded["target_text"] == "Display"
    assert recorded["target_resource_id"] == "id/display"

    # The snapshot bounds the valid range, not the state.
    with pytest.raises(_ArgError, match="Active index range is 1 to 2"):
        executor._resolve_index(5, "click", state, index_elements=REFRESHED)
    _, _, _, recorded = executor._translate(
        "long_press", {"target": 4}, state, index_elements=ORIGINAL
    )
    assert recorded["target_text"] == "Bluetooth"
    _, wire, _, recorded = executor._translate(
        "input_text", {"target": 2, "text": "hi"}, state, index_elements=ORIGINAL
    )
    assert wire["target"] == [500, 250] and recorded["target_text"] == "Search"


# --- Runner: one snapshot per turn ------------------------------------------------------


@pytest.mark.asyncio
async def test_two_index_clicks_in_one_turn_resolve_against_the_original_list(mock_context):
    """``click(3)`` then ``click(5)``: the first click's observation lists only
    two elements, yet the second index still lands on the original ``[5]``
    (Display), with its semantics recorded from the original element."""
    runner = _runner(mock_context, observations=[REFRESHED, [_el(1, "Display page", 1, 1)]])
    state = _state(ORIGINAL)
    messages: list = []
    turn = _TurnRecord()

    final, _, xml_list, _ = await runner._process_tool_calls(
        [_click(3, "tc-1"), _click(5, "tc-2")],
        state,
        messages,
        "thought",
        {},
        b"pre",
        "xml",
        0,
        turn,
    )

    assert final is None
    session = runner.executor._session
    assert [args["target"] for _, args in session.calls] == [[500, 417], [500, 750]]
    assert [status for _, status, _ in turn.actions] == ["success", "success"]
    tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
    assert [m.status for m in tool_msgs] == ["success", "success"]
    # The state kept being refreshed: the NEXT observation shows the newest list.
    assert [e["text"] for e in state.indexed_elements] == ["Display page"]
    assert xml_list == "[1] Display page"


@pytest.mark.asyncio
async def test_second_index_beyond_the_refreshed_list_is_still_valid(mock_context):
    """Without the snapshot ``click(5)`` after the refresh was an
    'Active index range is 1 to 2' error; with it the turn's own list is
    authoritative and the recorded metadata comes from that list."""
    runner = _runner(mock_context, observations=[REFRESHED, REFRESHED])
    state = _state(ORIGINAL)
    captured = []
    real_execute = runner.executor.execute

    async def spy(name, args, tc_id, st, **kwargs):
        result = await real_execute(name, args, tc_id, st, **kwargs)
        captured.append(result)
        return result

    runner.executor.execute = spy

    await runner._process_tool_calls(
        [_click(4, "tc-1"), _click(5, "tc-2")],
        state,
        [],
        "thought",
        {},
        b"pre",
        "xml",
        0,
        _TurnRecord(),
    )

    assert [r.status for r in captured] == ["success", "success"]
    assert [r.metadata["target_coordinates"] for r in captured] == [[500, 583], [500, 750]]
    assert [r.metadata["target_semantics"]["target_text"] for r in captured] == [
        "Bluetooth",
        "Display",
    ]
    assert captured[1].metadata["target_semantics"]["target_bounds"] == [407, 729, 593, 771]


@pytest.mark.asyncio
async def test_explorer_candidates_registered_before_an_action_join_the_snapshot(mock_context):
    """``ask_explorer`` appends to the state's list in place; before any
    action that list is the snapshot, so the new index resolves in the same
    turn even though the action's observation then replaces the state list."""
    runner = _runner(mock_context, observations=[REFRESHED])
    state = _state(ORIGINAL)

    async def fake_explorer(query, context_feedback, st):
        st.indexed_points.append([540, 2200])
        st.indexed_elements.append(_el(6, "Hidden gear", 540, 2200))
        return "Found 'gear' at index 6.", True

    runner.executor._ask_explorer = fake_explorer

    await runner._process_tool_calls(
        [
            {"name": "ask_explorer", "args": {"query": "gear"}, "id": "tc-0"},
            _click(6, "tc-1"),
        ],
        state,
        [],
        "thought",
        {},
        b"pre",
        "xml",
        0,
        _TurnRecord(),
    )

    session = runner.executor._session
    assert [args["target"] for _, args in session.calls] == [[500, 917]]
    assert [e["text"] for e in state.indexed_elements] == ["Back", "Wi-Fi toggle"]


@pytest.mark.asyncio
async def test_explorer_after_an_action_moves_the_snapshot_to_the_list_it_numbered(mock_context):
    """After an action replaced the state list, the explorer numbers its
    candidates against that replaced list; the snapshot follows so the index
    the explorer just handed out resolves correctly."""
    runner = _runner(mock_context, observations=[REFRESHED, REFRESHED])
    state = _state(ORIGINAL)

    async def fake_explorer(query, context_feedback, st):
        st.indexed_points.append([200, 2000])
        st.indexed_elements.append(_el(len(st.indexed_elements) + 1, "Gear", 200, 2000))
        return "Found.", True

    runner.executor._ask_explorer = fake_explorer

    await runner._process_tool_calls(
        [
            _click(3, "tc-1"),
            {"name": "ask_explorer", "args": {"query": "gear"}, "id": "tc-2"},
            _click(3, "tc-3"),  # the explorer's index on the refreshed list
        ],
        state,
        [],
        "thought",
        {},
        b"pre",
        "xml",
        0,
        _TurnRecord(),
    )

    session = runner.executor._session
    assert [args["target"] for _, args in session.calls] == [[500, 417], [185, 833]]
