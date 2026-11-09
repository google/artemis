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

"""Explorer run wiring: the universal loop, initial annotation, perception tools."""

import json

from langchain_core.messages import AIMessage, HumanMessage
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from artemis.agents.explorer.explorer import Explorer
from artemis.agents.explorer.screen_index import ScreenIndex
from artemis.context import ArtemisContext
from artemis.data_engine.models import ImageRecord
from artemis.graph.state import State

EXPLORER = "artemis.agents.explorer.explorer"
RUN_SETUP = "artemis.agents.explorer.run_setup"
PERCEPTION_TOOLS = "artemis.agents.explorer.perception_tools"
UNIVERSAL_RUNNER = "artemis.agents.explorer.universal_runner"


@pytest.fixture
def screenshot(tmp_path):
    path = tmp_path / "screen.jpg"
    path.write_bytes(b"fake-jpeg-bytes")
    return str(path)


@pytest.fixture
def ctx(tmp_path):
    context = MagicMock(spec=ArtemisContext)
    context.device = MagicMock()
    context.device.device_width = 1080
    context.device.device_height = 2400
    context.data_engine = MagicMock()
    context.data_engine.base_dir = tmp_path
    context.llm_config = MagicMock()
    context.llm_config.explorer = MagicMock()
    context.agent_config = MagicMock()
    context.agent_config.denylisted_tools = {}
    return context


def _state(screenshot: str, hierarchy=None) -> MagicMock:
    state = MagicMock(spec=State)
    state.latest_screenshot = screenshot
    state.latest_ui_hierarchy = hierarchy if hierarchy is not None else []
    state.operator_raw_data = {}
    return state


def _chat_model(*responses) -> MagicMock:
    llm = MagicMock()
    llm.bind_tools.return_value.ainvoke = AsyncMock(side_effect=list(responses))
    return llm


def _tool_call(name: str, args: dict) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{name}"}])


def _submit(candidates: list[dict], fallback: str = "") -> AIMessage:
    return _tool_call("submit_answer", {"candidates": candidates, "fallback_message": fallback})


@pytest.fixture
def storage():
    store = MagicMock()
    store.get_image.return_value = None
    return store


async def _run(explorer, ctx, storage, screenshot, state, **kwargs):
    with (
        patch(f"{EXPLORER}.is_ocr_configured", return_value=True),
        patch(f"{RUN_SETUP}.StorageManager", return_value=storage),
    ):
        return await explorer.run(
            kwargs.pop("query", "Find button"),
            "",
            screenshot,
            state,
            **kwargs,
        )


# --------------------------------------------------------------------------- #
# Universal loop
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_explorer_run_returns_submitted_candidates(ctx, storage, screenshot):
    llm = _chat_model(
        _submit([{"label": "S1", "coords": [500, 500], "description": "Target button"}])
    )
    explorer = Explorer(ctx)
    with patch(f"{UNIVERSAL_RUNNER}.get_llm", return_value=llm):
        raw = await _run(explorer, ctx, storage, screenshot, _state(screenshot))

    assert json.loads(raw) == {
        "candidates": [{"label": "S1", "coords": [500, 500], "description": "Target button"}],
        "fallback_message": "",
    }


@pytest.mark.asyncio
async def test_explorer_run_drops_out_of_bounds_candidates(ctx, storage, screenshot):
    llm = _chat_model(
        _submit(
            [
                {"label": "S1", "coords": [1500, -200], "description": "Ambiguous button"},
                {"label": "S2", "coords": [500, 500], "description": "Corrected button"},
            ]
        )
    )
    explorer = Explorer(ctx)
    with patch(f"{UNIVERSAL_RUNNER}.get_llm", return_value=llm):
        raw = await _run(explorer, ctx, storage, screenshot, _state(screenshot))

    assert [c["label"] for c in json.loads(raw)["candidates"]] == ["S2"]


@pytest.mark.asyncio
async def test_explorer_denylisted_tool_is_hidden_from_the_model(ctx, storage, screenshot):
    ctx.agent_config.denylisted_tools = {"explorer": ["inspect_region"]}
    llm = _chat_model(_submit([]))
    explorer = Explorer(ctx)
    with patch(f"{UNIVERSAL_RUNNER}.get_llm", return_value=llm):
        await _run(explorer, ctx, storage, screenshot, _state(screenshot), version="ultra")

    bound_names = {t["function"]["name"] for t in llm.bind_tools.call_args_list[0].args[0]}
    assert "inspect_region" not in bound_names
    assert "ask_perception_tool" in bound_names
    system_message = llm.bind_tools.return_value.ainvoke.call_args.args[0][0]
    assert "# TOOL DENYLIST" in system_message.content
    assert "inspect_region" in system_message.content


@pytest.mark.asyncio
async def test_explorer_final_turn_tool_stripping(ctx, storage, screenshot):
    """On the final turn the loop exposes only ``submit_answer`` and warns the model."""
    llm = _chat_model(
        _tool_call("ask_perception_tool", {"search_query": "q", "nx": 1, "ny": 2}),
        _tool_call("ask_perception_tool", {"search_query": "q", "nx": 1, "ny": 2}),
        _tool_call("ask_perception_tool", {"search_query": "q", "nx": 1, "ny": 2}),
    )
    explorer = Explorer(ctx)
    explorer.exec_ask_perception_tool = AsyncMock(return_value={"text": "ok"})
    with patch(f"{UNIVERSAL_RUNNER}.get_llm", return_value=llm):
        raw = await _run(explorer, ctx, storage, screenshot, _state(screenshot), version="pro")

    # The pro tier budgets 3 turns; only the last one strips the other tools.
    assert llm.bind_tools.call_count == 2
    final_tools = llm.bind_tools.call_args_list[1].args[0]
    assert [t["function"]["name"] for t in final_tools] == ["submit_answer"]

    final_messages = llm.bind_tools.return_value.ainvoke.call_args_list[2].args[0]
    warnings = [
        message.content
        for message in final_messages
        if isinstance(message, HumanMessage) and isinstance(message.content, str)
    ]
    assert any("[WARNING] This is your final iteration" in text for text in warnings)
    assert json.loads(raw)["candidates"] == []


# --------------------------------------------------------------------------- #
# Initial annotation
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_explorer_initial_visual_marking(ctx, storage, screenshot):
    storage.get_image.return_value = "dummy_hash"
    hierarchy = [{"text": "Clickable Button", "bounds": "[100,100][200,200]"}]
    llm = _chat_model(_submit([{"label": "1", "coords": [500, 500], "description": "target"}]))

    explorer = Explorer(ctx)
    with (
        patch(f"{UNIVERSAL_RUNNER}.get_llm", return_value=llm),
        patch(f"{RUN_SETUP}.draw_dots") as mock_draw_dots,
    ):
        await _run(
            explorer,
            ctx,
            storage,
            screenshot,
            _state(screenshot, hierarchy),
        )

    mock_draw_dots.assert_called_once()
    assert mock_draw_dots.call_args.args[1] == [[150, 150]]
    assert mock_draw_dots.call_args.args[2] == ["1"]


@pytest.mark.asyncio
async def test_explorer_initial_visual_marking_previous_screenshot(ctx, storage, screenshot):
    storage.get_image.return_value = ImageRecord(
        image_name="previous",
        ui_tree=[{"text": "Previous Button", "bounds": "[100,100][200,200]"}],
    )
    current = _state(
        "/tmp/current_screenshot.jpg", [{"text": "Current", "bounds": "[300,300][400,400]"}]
    )
    llm = _chat_model(_submit([{"label": "1", "coords": [500, 500], "description": "target"}]))

    explorer = Explorer(ctx)
    with (
        patch(f"{UNIVERSAL_RUNNER}.get_llm", return_value=llm),
        patch(f"{RUN_SETUP}.draw_dots") as mock_draw_dots,
    ):
        await _run(explorer, ctx, storage, screenshot, current)

    mock_draw_dots.assert_called_once()
    assert mock_draw_dots.call_args.args[1] == [[150, 150]]
    assert mock_draw_dots.call_args.args[1] != [[350, 350]]


@pytest.mark.asyncio
async def test_explorer_initial_visual_marking_previous_screenshot_no_ui_tree(
    ctx, storage, screenshot
):
    storage.get_image.return_value = None
    current = _state(
        "/tmp/current_screenshot.jpg", [{"text": "Current", "bounds": "[300,300][400,400]"}]
    )
    llm = _chat_model(_submit([{"label": "1", "coords": [500, 500], "description": "target"}]))

    explorer = Explorer(ctx)
    with (
        patch(f"{UNIVERSAL_RUNNER}.get_llm", return_value=llm),
        patch(f"{RUN_SETUP}.draw_dots") as mock_draw_dots,
    ):
        await _run(explorer, ctx, storage, screenshot, current)

    mock_draw_dots.assert_not_called()


@pytest.mark.asyncio
async def test_explorer_initial_visual_marking_previous_screenshot_ocr_fusion(
    ctx, storage, screenshot
):
    record = ImageRecord(
        image_name="previous",
        ui_tree=[{"text": "", "bounds": "[100,100][200,200]"}],
        ocr_result=[
            {"text": "full screen", "position": [{"x": 0, "y": 0}]},
            {
                "text": "OCR Text",
                "position": [
                    {"x": 110, "y": 110},
                    {"x": 190, "y": 110},
                    {"x": 190, "y": 190},
                    {"x": 110, "y": 190},
                ],
            },
        ],
    )
    storage.get_image.return_value = record
    current = _state("/tmp/current_screenshot.jpg")
    llm = _chat_model(_submit([{"label": "1", "coords": [500, 500], "description": "target"}]))

    explorer = Explorer(ctx)
    with (
        patch(f"{UNIVERSAL_RUNNER}.get_llm", return_value=llm),
        patch(f"{RUN_SETUP}.draw_dots") as mock_draw_dots,
    ):
        await _run(explorer, ctx, storage, screenshot, current)

    mock_draw_dots.assert_called_once()
    assert mock_draw_dots.call_args.args[1] == [[150, 150]]


@pytest.mark.asyncio
async def test_explorer_initial_visual_marking_previous_screenshot_on_the_fly_ocr(
    ctx, storage, screenshot
):
    record = ImageRecord(
        image_name="previous", ui_tree=[{"text": "", "bounds": "[100,100][200,200]"}]
    )
    storage.get_image.return_value = record
    current = _state("/tmp/current_screenshot.jpg")
    ocr_result = [
        {"text": "full screen", "position": [{"x": 0, "y": 0}]},
        {
            "text": "On-the-fly OCR Text",
            "position": [
                {"x": 110, "y": 110},
                {"x": 190, "y": 110},
                {"x": 190, "y": 190},
                {"x": 110, "y": 190},
            ],
        },
    ]
    llm = _chat_model(_submit([{"label": "1", "coords": [500, 500], "description": "target"}]))

    explorer = Explorer(ctx)
    with (
        patch(f"{UNIVERSAL_RUNNER}.get_llm", return_value=llm),
        patch(f"{RUN_SETUP}.perform_ocr", new_callable=AsyncMock, return_value=ocr_result) as ocr,
        patch(f"{RUN_SETUP}.draw_dots") as mock_draw_dots,
    ):
        await _run(explorer, ctx, storage, screenshot, current)

    ocr.assert_awaited_once()
    mock_draw_dots.assert_called_once()
    assert mock_draw_dots.call_args.args[1] == [[150, 150]]


# --------------------------------------------------------------------------- #
# Perception tools (engine independent)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_explorer_inspect_region(ctx):
    import pathlib

    import numpy as np

    explorer = Explorer(ctx)
    explorer.width = 1080
    explorer.height = 2400
    explorer.screenshot_path = "/tmp/test_screenshot.jpg"
    explorer.image_name = "test_image"

    dummy_img = np.zeros((2400, 1080, 3), dtype=np.uint8)

    with (
        patch("cv2.imread", return_value=dummy_img) as mock_imread,
        patch("cv2.resize", return_value=dummy_img) as mock_resize,
        patch("cv2.imwrite", return_value=True) as mock_imwrite,
        patch("pathlib.Path.mkdir") as mock_mkdir,
        patch("glob.glob", return_value=[]),
        patch(f"{PERCEPTION_TOOLS}.settings") as mock_settings,
    ):
        mock_settings.TRACES_PATH = pathlib.Path("/tmp/traces")
        res = await explorer.exec_inspect_region(
            x_min=100, y_min=200, x_max=300, y_max=400, zoom_factor=2.0
        )
        assert res["image_path"] is not None
        assert "Inspected region coordinates" in res["text"]
        mock_imread.assert_called_once_with("/tmp/test_screenshot.jpg")

        # 108..324 by 480..960 pixels, doubled by the zoom factor.
        mock_resize.assert_called_once()
        assert mock_resize.call_args.args[1] == (432, 960)
        mock_imwrite.assert_called_once()


@pytest.mark.asyncio
async def test_explorer_ask_perception_tool(ctx):
    """The perception bundle answers from the in-memory screen index.

    No Data Engine lookup is involved: the text search, the coordinate
    audit and the OCR inventory all read ``explorer.screen_index``.
    """
    from unittest.mock import mock_open

    import numpy as np

    ctx.data_engine.base_dir = "/tmp/test_session"
    explorer = Explorer(ctx)
    explorer.image_name = "dummy_hash"
    explorer.screenshot_path = "/tmp/test_screenshot.jpg"
    explorer.width = 1080
    explorer.height = 2400
    explorer.global_label_idx = 1
    explorer.image_pool = {
        "img_0": {
            "path": "/tmp/test_screenshot.jpg",
            "transform": {
                "offset_x": 0.0,
                "offset_y": 0.0,
                "scale_x": 1.0,
                "scale_y": 1.0,
            },
            "description": "Original complete screenshot",
        }
    }
    explorer.screen_index = ScreenIndex.from_hierarchy(
        [
            {
                "bounds": "[500,400][600,560]",
                "text": "Settings",
                "resource-id": "com.android.settings:id/btn_settings",
                "class": "android.widget.Button",
                "clickable": "true",
            },
            {
                "bounds": "[100,100][200,200]",
                "class": "android.widget.FrameLayout [OCR]",
                "ocr_elements": [{"text": "Settings page", "bounds": "[100,100][200,200]"}],
            },
        ],
        1080,
        2400,
    )

    mock_detector_res = {
        "detected": [{"label": "profile icon", "point": [900, 100]}],
        "failed": [],
    }
    mock_draw_dots = MagicMock()

    with (
        patch(f"{RUN_SETUP}.StorageManager") as storage_cls,
        patch(f"{PERCEPTION_TOOLS}._run_object_detection", return_value=mock_detector_res),
        patch(f"{PERCEPTION_TOOLS}.draw_dots", mock_draw_dots),
        patch("artemis.utils.visualization.draw_dots", mock_draw_dots),
        patch("pathlib.Path.mkdir"),
        patch("glob.glob", return_value=[]),
        patch("builtins.open", mock_open(read_data=b'{"templates": []}')),
        patch("cv2.imread", return_value=np.zeros((2400, 1080, 3), dtype=np.uint8)),
    ):
        result = await explorer.exec_ask_perception_tool(
            search_query="Settings",
            nx=550,
            ny=200,
            detect_queries=["profile icon"],
        )

    storage_cls.assert_not_called()
    assert "Text Search Results are:" in result["text"]
    assert "Coordinate Search Results are:" in result["text"]
    assert "Object Detection Results are:" in result["text"]

    assert "[X1] 'Settings' at [509,200]" in result["text"]
    assert "[O2] 'Settings page' at [138,62]" in result["text"]
    assert (
        "Matched element at [550,200]: [X3] | 'Settings' class=android.widget.Button"
        " id=com.android.settings:id/btn_settings source=ui-tree interactive"
    ) in result["text"]
    assert "[D4]" in result["text"]

    assert explorer.label_registry["X1"].text == "Settings"
    assert explorer.label_registry["O2"].source == "ocr"
    assert explorer.label_registry["X3"] is explorer.label_registry["X1"]
    assert "D4" not in explorer.label_registry

    assert len(result["image_paths"]) == 3
    assert mock_draw_dots.call_count == 3
    assert mock_draw_dots.call_args_list[0].kwargs.get("color") == "green"
    assert mock_draw_dots.call_args_list[0].args[2] == ["X1", "O2"]
    assert mock_draw_dots.call_args_list[1].kwargs.get("color") == "blue"
    assert mock_draw_dots.call_args_list[1].args[2] == ["X3"]
    assert mock_draw_dots.call_args_list[2].kwargs.get("color") == "red"
    assert mock_draw_dots.call_args_list[2].args[2] == ["D4"]
