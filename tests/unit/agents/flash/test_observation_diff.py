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
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from langchain_core.messages import HumanMessage

from artemis.agents.flash.runner import FlashRunner, _TurnRecord
from artemis.graph.state import State
from artemis.mcp.action_types import ActionResult, ObserveResult
from artemis.mcp.observation import observe
from artemis.mcp.screen_diff import NO_SCREEN_CHANGES, UI_DIFF_MARKER


def _el(index, text, cx=540, cy=200):
    return {
        "index": index,
        "center": [cx, cy],
        "text": text,
        "bounds": [cx - 100, cy - 50, cx + 100, cy + 50],
        "class": "android.widget.TextView",
        "resource_id": f"id/{text.lower().replace(' ', '_')}",
        "is_ocr": False,
    }


def _node(text, bounds):
    return {
        "text": text,
        "content-desc": "",
        "bounds": bounds,
        "class": "android.widget.TextView",
        "resource-id": f"com.app:id/{text.lower().replace(' ', '_')}",
    }


def _screen_data(nodes):
    return SimpleNamespace(
        base64=base64.b64encode(b"IMG").decode("ascii"),
        elements=nodes,
        width=1080,
        height=2400,
    )


@pytest.fixture
def mock_context():
    ctx = Mock()
    ctx.llm_config = Mock()
    ctx.device = Mock()
    ctx.device.device_width = 1080
    ctx.device.device_height = 2400
    ctx.data_engine = None
    ctx.adb_client = None
    ctx.driver = Mock()
    return ctx


def _texts(tail):
    assert isinstance(tail, HumanMessage)
    return [b["text"] for b in tail.content if isinstance(b, dict) and b.get("type") == "text"]


def _runner(mock_context):
    with patch("artemis.controllers.unified_controller.get_driver"):
        return FlashRunner(mock_context, goal="g")


def test_tail_without_diff_has_no_diff_block(mock_context):
    tail = _runner(mock_context)._build_tail(Mock(), 1, b"IMG", "[1] Text: 'Wi-Fi'")
    assert not any(UI_DIFF_MARKER in text for text in _texts(tail))


def test_tail_leads_text_with_diff_and_hides_it_from_history(mock_context):
    from artemis.memory.transcript import EPHEMERAL_BLOCKS_KEY

    runner = _runner(mock_context)
    diff = "~ [2] Text: 'Bluetooth' | Bounds: [0,10][100,110]"
    tail = runner._build_tail(Mock(), 2, b"IMG", "[1] Text: 'Wi-Fi'\n[2] Text: 'Bluetooth'", diff)
    blocks = tail.content
    diff_at = next(
        i
        for i, block in enumerate(blocks)
        if isinstance(block, dict) and UI_DIFF_MARKER in block.get("text", "")
    )
    list_at = next(
        i
        for i, block in enumerate(blocks)
        if isinstance(block, dict) and "--- Visible UI Elements ---" in block.get("text", "")
    )
    assert diff_at < list_at
    assert diff in blocks[diff_at]["text"]
    assert diff_at in tail.additional_kwargs[EPHEMERAL_BLOCKS_KEY]


def test_tail_with_empty_diff_names_no_changes(mock_context):
    tail = _runner(mock_context)._build_tail(Mock(), 2, None, "[1] Text: 'Wi-Fi'", "")
    assert any(NO_SCREEN_CHANGES in text for text in _texts(tail))


@pytest.mark.asyncio
async def test_second_observation_reports_the_changed_line(mock_context, tmp_path):
    controller = Mock()
    controller.get_screen_data = AsyncMock(
        return_value=_screen_data([_node("Wi-Fi", "[100,200][300,400]")])
    )
    with patch("artemis.mcp.observation.get_temp_dir", return_value=tmp_path):
        first, _ = await observe(mock_context, controller, settle_ms=0)
        controller.get_screen_data = AsyncMock(
            return_value=_screen_data([_node("Wi-Fi", "[100,200][300,500]")])
        )
        second, _ = await observe(mock_context, controller, settle_ms=0)
    assert first.diff_text is None
    assert second.diff_text is not None
    assert second.diff_text.startswith("~ [1] Text: 'Wi-Fi'")
    assert second.elements_text is not None


@pytest.mark.asyncio
async def test_unchanged_observation_reports_an_empty_diff(mock_context, tmp_path):
    controller = Mock()
    controller.get_screen_data = AsyncMock(
        return_value=_screen_data([_node("Wi-Fi", "[100,200][300,400]")])
    )
    with patch("artemis.mcp.observation.get_temp_dir", return_value=tmp_path):
        await observe(mock_context, controller, settle_ms=0)
        second, _ = await observe(mock_context, controller, settle_ms=0)
    assert second.diff_text == ""


class _FakeSession:
    started = True

    def __init__(self, elements, diff_text):
        self._elements = elements
        self._diff_text = diff_text

    async def call(self, name, args):
        return ActionResult.success(name, f"{name} dispatched")

    async def observe(self, settle_ms=0):
        return ObserveResult(
            ok=True,
            elements_text="\n".join(f"[{e['index']}] {e['text']}" for e in self._elements),
            elements=self._elements,
            diff_text=self._diff_text,
            hierarchy_ok=True,
        )


@pytest.mark.asyncio
async def test_action_diff_reaches_the_next_tail(mock_context):
    from unittest.mock import AsyncMock

    from artemis.context import ArtemisContext

    ctx = Mock(spec=ArtemisContext)
    ctx.llm_config = Mock()
    ctx.device = Mock()
    ctx.device.device_width = 1080
    ctx.device.device_height = 2400
    ctx.data_engine = None
    ctx.adb_client = None
    ctx.driver = Mock()
    runner = _runner(ctx)
    runner.summarizer = None
    runner._capture_post_screenshot = AsyncMock(return_value=None)
    refreshed = [_el(1, "Back"), _el(2, "Wi-Fi toggle")]
    runner.executor._session = _FakeSession(refreshed, "+ [2] Text: 'Wi-Fi toggle'")
    state = State(initial_goal="g")
    state.indexed_elements = [_el(1, "Wi-Fi")]
    state.indexed_points = [[540, 200]]
    _, _, xml_list, xml_diff, _ = await runner._process_tool_calls(
        [{"name": "click", "args": {"target": 1}, "id": "tc-1"}],
        state,
        [],
        "thought",
        {},
        b"pre",
        "xml",
        None,
        0,
        _TurnRecord(),
    )
    assert xml_diff == "+ [2] Text: 'Wi-Fi toggle'"
    tail = runner._build_tail(Mock(), 2, b"IMG", xml_list, xml_diff)
    assert any("+ [2] Text: 'Wi-Fi toggle'" in text for text in _texts(tail))
