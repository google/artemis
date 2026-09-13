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

"""Explorer in-memory screen search, label registry, bounds enrichment and pre-pass."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from artemis.agents.explorer.explorer import Explorer
from artemis.agents.explorer.screen_index import ScreenElement, ScreenIndex
from artemis.context import ArtemisContext
from artemis.graph.state import State

RUN_SETUP = "artemis.agents.explorer.run_setup"
PERCEPTION_TOOLS = "artemis.agents.explorer.perception_tools"
W, H = 1080, 2400

#: One "Settings" button, two "Send" buttons and two OCR-only texts.
HIERARCHY = [
    {
        "text": "Settings",
        "bounds": "[500,400][600,560]",
        "class": "android.widget.Button",
        "resource-id": "app:id/settings",
        "clickable": "true",
    },
    {"text": "Send", "bounds": "[100,2000][300,2100]", "class": "android.widget.Button"},
    {"text": "Send", "bounds": "[700,2000][900,2100]", "class": "android.widget.Button"},
    {
        "bounds": "[0,0][1080,2400]",
        "class": "android.widget.FrameLayout",
        "ocr_elements": [
            {"text": "Settings page", "bounds": "[100,100][300,160]"},
            {"text": "Welcome", "bounds": "[100,200][400,260]"},
        ],
    },
]

#: "Settings" [500,400][600,560] on a 1080x2400 screen, in normalized units.
SETTINGS_COORDS = [509, 200]
SETTINGS_BOUNDS = [462, 166, 555, 233]


def _context() -> MagicMock:
    ctx = MagicMock(spec=ArtemisContext)
    ctx.device = MagicMock()
    ctx.device.device_width = W
    ctx.device.device_height = H
    ctx.data_engine = MagicMock()
    ctx.data_engine.base_dir = None
    ctx.llm_config = MagicMock()
    ctx.llm_config.explorer = MagicMock()
    ctx.llm_config.explorer.model = "gemini-3.8-flash"
    ctx.agent_config = MagicMock()
    ctx.agent_config.denylisted_tools = {}
    return ctx


def _state(screenshot: str, hierarchy=None) -> MagicMock:
    state = MagicMock(spec=State)
    state.latest_screenshot = screenshot
    state.latest_ui_hierarchy = HIERARCHY if hierarchy is None else hierarchy
    state.operator_raw_data = {}
    return state


def _indexed_explorer(hierarchy=None) -> Explorer:
    """An Explorer whose screen is already indexed (no ``run`` involved)."""
    explorer = Explorer(_context())
    explorer.width, explorer.height = W, H
    explorer.screenshot_path = "/tmp/screen.jpg"
    explorer.screen_index = ScreenIndex.from_hierarchy(
        HIERARCHY if hierarchy is None else hierarchy, W, H
    )
    return explorer


def _storage(record=None) -> MagicMock:
    storage = MagicMock()
    storage.get_image.return_value = record
    return storage


def _outcome(candidates=None, fallback: str = "") -> str:
    return json.dumps({"candidates": candidates or [], "fallback_message": fallback})


@pytest.fixture
def screenshot(tmp_path):
    path = tmp_path / "screen.jpg"
    path.write_bytes(b"fake-jpeg-bytes")
    return str(path)


@pytest.fixture
def no_drawing():
    """Keeps the annotation helpers away from the file system."""
    draw_dots = MagicMock()
    with (
        patch(f"{RUN_SETUP}.draw_dots", draw_dots),
        patch(f"{PERCEPTION_TOOLS}.draw_dots", draw_dots),
        patch("pathlib.Path.mkdir"),
        patch("glob.glob", return_value=[]),
    ):
        yield draw_dots


# --------------------------------------------------------------------------- #
# Screen index construction inside run()
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_index_is_built_from_state_hierarchy_when_record_is_missing(screenshot, no_drawing):
    explorer = Explorer(_context())
    explorer._run_flash = AsyncMock(return_value=_outcome(fallback="nothing"))

    with (
        patch(f"{RUN_SETUP}.StorageManager", return_value=_storage(None)),
        patch(f"{RUN_SETUP}.perform_ocr", new_callable=AsyncMock) as perform_ocr,
    ):
        await explorer.run("gear icon", "", screenshot, _state(screenshot), version="flash")
        text = (await explorer._search_ui_helper("Settings", prefix="X"))["text"]

    perform_ocr.assert_not_awaited()
    assert len(explorer.screen_index) == 5
    assert "not found in Data Engine" not in text
    assert "[X1] 'Settings' at [509,200]" in text


@pytest.mark.asyncio
async def test_index_ignores_state_hierarchy_for_another_screenshot(screenshot):
    explorer = Explorer(_context())
    explorer._run_flash = AsyncMock(return_value=_outcome())
    state = _state("/tmp/other.jpg")

    with patch(f"{RUN_SETUP}.StorageManager", return_value=_storage(None)):
        await explorer.run("Settings", "", screenshot, state, version="flash")

    assert len(explorer.screen_index) == 0
    explorer._run_flash.assert_awaited_once_with("Settings", screenshot)
    assert (await explorer._search_ui_helper("Settings"))["text"] == (
        "No UI-tree data is available for this screen; rely on visual detection."
    )


# --------------------------------------------------------------------------- #
# Perception helpers answer from the index
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_search_ui_helper_merges_lenient_matches_without_duplicates(no_drawing):
    hierarchy = [*HIERARCHY, {"text": "Settled", "bounds": "[100,300][300,360]"}]
    explorer = _indexed_explorer(hierarchy)
    spy = MagicMock(wraps=explorer.screen_index.search_text)
    explorer.screen_index.search_text = spy

    result = await explorer._search_ui_helper("Settings", prefix="X", color="green")

    assert [call.args[1] for call in spy.call_args_list] == [0.7, 0.4]
    parts = result["text"].split(" | ")
    assert parts[0] == "[X1] 'Settings' at [509,200]"
    assert parts[1] == "[O2] 'Settings page' at [185,54]"
    # "Settled" (similarity ~0.53) only arrives through the lenient pass...
    assert any(part.startswith("[X") and "'Settled'" in part for part in parts)
    # ...and the strict hits are not listed a second time.
    assert sum("'Settings'" in part for part in parts) == 1
    assert explorer.label_registry["X1"].text == "Settings"
    assert explorer.label_registry["O2"].source == "ocr"
    assert no_drawing.call_args.kwargs["color"] == "green"
    assert result["image_path"] is not None


@pytest.mark.asyncio
async def test_search_ui_helper_skips_lenient_pass_with_enough_strict_hits(no_drawing):
    explorer = _indexed_explorer()
    strict = explorer.screen_index.search_text("Settings", 0.4)  # 5 hits, all "strict"
    explorer.screen_index.search_text = MagicMock(return_value=strict)

    await explorer._search_ui_helper("Settings", prefix="X")

    explorer.screen_index.search_text.assert_called_once_with("Settings", 0.7)


@pytest.mark.asyncio
async def test_search_by_coords_helper_reports_innermost_first(no_drawing):
    explorer = _indexed_explorer(
        [
            {"text": "Card", "bounds": "[0,0][1080,1200]", "class": "android.widget.FrameLayout"},
            {"text": "Title", "bounds": "[500,400][600,560]", "class": "android.widget.TextView"},
        ]
    )

    hit = await explorer._search_by_coords_helper(550, 200, prefix="X", color="blue")
    miss = await explorer._search_by_coords_helper(10, 990, prefix="X", color="blue")

    assert hit["text"] == (
        "Matched element at [550,200]: [X1]"
        " | 'Title' class=android.widget.TextView source=ui-tree"
        " | 'Card' class=android.widget.FrameLayout source=ui-tree"
    )
    assert explorer.label_registry["X1"].text == "Title"
    assert miss["text"] == "No elements found at [10,990]."
    assert "X2" not in explorer.label_registry
    # The blue dot is drawn for both probes at the pixel position.
    assert no_drawing.call_count == 2
    assert no_drawing.call_args_list[0].args[1] == [[594, 480]]
    assert no_drawing.call_args_list[0].args[2] == ["X1"]


@pytest.mark.asyncio
async def test_get_ocr_list_reads_the_index(no_drawing):
    explorer = _indexed_explorer()

    with patch(f"{RUN_SETUP}.StorageManager") as storage_cls:
        result = await explorer.exec_get_ocr_list()

    storage_cls.assert_not_called()
    assert result["text"].splitlines() == [
        "[O1] 'Settings page' coords: [185,54]",
        "[O2] 'Welcome' coords: [231,95]",
    ]
    assert result["image_path"] is not None
    assert {k: v.source for k, v in explorer.label_registry.items()} == {"O1": "ocr", "O2": "ocr"}


@pytest.mark.asyncio
async def test_get_ocr_list_without_ocr_elements():
    explorer = _indexed_explorer([{"text": "Only XML", "bounds": "[0,0][100,100]"}])
    result = await explorer.exec_get_ocr_list()
    assert result == {"text": "No text elements detected on the screen.", "image_path": None}


# --------------------------------------------------------------------------- #
# Label registry from the initial numbered list
# --------------------------------------------------------------------------- #


def test_initial_list_registers_numbered_labels(no_drawing):
    explorer = _indexed_explorer()

    minimal_list, _marked = explorer._annotate_initial_screenshot(
        HIERARCHY, "/tmp/screen.jpg", None, ""
    )

    assert minimal_list.splitlines()[0].startswith("[1] Text: 'Settings'")
    assert sorted(explorer.label_registry) == ["1", "2", "3", "4", "5"]
    settings = explorer.label_registry["1"]
    assert settings.bounds == (500, 400, 600, 560)
    assert settings.source == "xml" and settings.interactive  # taken from the index
    assert explorer.label_registry["4"].source == "ocr"
    assert explorer.label_registry["4"].text == "Settings page"
    assert explorer.global_label_idx == 6
    assert no_drawing.call_args.args[1][0] == [550, 480]


# --------------------------------------------------------------------------- #
# Submit enrichment
# --------------------------------------------------------------------------- #


def test_enrich_candidates_adds_normalized_bounds_only_when_label_and_coords_agree():
    explorer = _indexed_explorer()
    settings = explorer.screen_index.exact_matches("Settings")[0]
    explorer._register_label("X1", settings)

    inside = {"label": "X1", "coords": SETTINGS_COORDS, "description": "gear"}
    # 614px is 14px right of the element, inside the 2% (21.6px) tolerance.
    near = {"label": "X1", "coords": [569, 200], "description": "gear"}
    far = {"label": "X1", "coords": [100, 900], "description": "gear"}
    unknown = {"label": "D7", "coords": SETTINGS_COORDS, "description": "gear"}
    invalid = {"label": "X1", "coords": [5000, 5], "description": "gear"}

    enriched = explorer._enrich_candidates([inside, near, far, unknown, invalid, "junk"])

    assert enriched[0] == {**inside, "bounds": SETTINGS_BOUNDS, "source": "xml"}
    assert enriched[1] == {**near, "bounds": SETTINGS_BOUNDS, "source": "xml"}
    assert enriched[2:] == [far, unknown, invalid, "junk"]
    assert "bounds" not in far and "bounds" not in inside  # inputs are never mutated


def test_enrich_candidates_withholds_degenerate_bounds():
    explorer = _indexed_explorer()
    explorer._register_label("X9", ScreenElement("dot", (10, 10, 11, 11), "xml"))
    cand = {"label": "X9", "coords": [9, 4]}
    assert explorer._enrich_candidates([cand]) == [cand]


def test_native_submit_enriches_registered_candidates():
    explorer = _indexed_explorer()
    explorer._register_label("O2", explorer.screen_index.ocr_elements[0])
    call = MagicMock()
    call.name = "submit_answer"
    call.args = {
        "candidates": [
            {"label": "O2", "coords": [185, 54], "description": "header"},
            {"label": "D3", "coords": [900, 100], "description": "icon"},
        ],
        "fallback_message": "",
    }
    turn_record = {"tool_calls": []}

    raw = explorer._process_submit_answer([call], turn_record, [])

    data = json.loads(raw)
    assert list(data) == ["candidates", "fallback_message"]
    assert data["candidates"][0] == {
        "label": "O2",
        "coords": [185, 54],
        "description": "header",
        "bounds": [92, 41, 277, 66],
        "source": "ocr",
    }
    assert data["candidates"][1] == call.args["candidates"][1]


def test_universal_submit_enriches_registered_candidates():
    explorer = _indexed_explorer()
    explorer._register_label("1", explorer.screen_index.exact_matches("Settings")[0])
    tool_calls = [
        {
            "name": "submit_answer",
            "args": {
                "candidates": [
                    {"label": "1", "coords": SETTINGS_COORDS, "description": "gear"},
                    {"label": "1", "coords": [5, 5000], "description": "bad"},
                ],
                "fallback_message": "note",
            },
            "id": "c1",
        }
    ]

    data = json.loads(explorer._universal_submit_outcome(tool_calls))

    assert data["fallback_message"] == "note"
    assert data["candidates"] == [
        {
            "label": "1",
            "coords": SETTINGS_COORDS,
            "description": "gear",
            "bounds": SETTINGS_BOUNDS,
            "source": "xml",
        }
    ]


# --------------------------------------------------------------------------- #
# Structural pre-pass
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("version", ["flash", "pro"])
@pytest.mark.asyncio
async def test_prepass_answers_unique_exact_match_without_the_engine(screenshot, version):
    explorer = Explorer(_context())
    explorer._run_flash = AsyncMock()
    explorer._run_loop = AsyncMock()

    with patch(f"{RUN_SETUP}.StorageManager", return_value=_storage(None)):
        raw = await explorer.run("  settings ", "", screenshot, _state(screenshot), version=version)

    explorer._run_flash.assert_not_called()
    explorer._run_loop.assert_not_called()
    assert json.loads(raw) == {
        "candidates": [
            {
                "label": "T1",
                "coords": SETTINGS_COORDS,
                "bounds": SETTINGS_BOUNDS,
                "source": "xml",
                "description": "Settings (exact text match)",
            }
        ],
        "fallback_message": "",
    }
    assert explorer.label_registry["T1"].text == "Settings"


@pytest.mark.asyncio
async def test_prepass_leaves_ambiguous_matches_to_the_engine(screenshot):
    explorer = Explorer(_context())
    engine = _outcome([{"label": "D1", "coords": [185, 854], "description": "Send"}])
    explorer._run_flash = AsyncMock(return_value=engine)

    with patch(f"{RUN_SETUP}.StorageManager", return_value=_storage(None)):
        raw = await explorer.run("Send", "", screenshot, _state(screenshot), version="flash")

    explorer._run_flash.assert_awaited_once_with("Send", screenshot)
    assert raw == engine


@pytest.mark.asyncio
async def test_prepass_without_pipe_or_match_passes_the_query_through(screenshot):
    explorer = Explorer(_context())
    engine = _outcome(fallback="Failed to detect: gear icon")
    explorer._run_flash = AsyncMock(return_value=engine)

    with patch(f"{RUN_SETUP}.StorageManager", return_value=_storage(None)):
        raw = await explorer.run("gear icon", "", screenshot, _state(screenshot), version="flash")

    explorer._run_flash.assert_awaited_once_with("gear icon", screenshot)
    assert raw == engine


@pytest.mark.asyncio
async def test_prepass_merges_partial_results_with_the_engine_outcome(screenshot):
    explorer = Explorer(_context())
    detected = {"label": "D1", "coords": [185, 854], "description": "Send"}
    explorer._run_loop = AsyncMock(return_value=_outcome([detected], fallback="two Send buttons"))

    with patch(f"{RUN_SETUP}.StorageManager", return_value=_storage(None)):
        raw = await explorer.run(
            "Settings | Send | Welcome", "", screenshot, _state(screenshot), version="pro"
        )

    # Only the unresolved part reaches the engine; "Welcome" is an OCR exact match.
    assert explorer._run_loop.await_args.args[1] == "Send"
    data = json.loads(raw)
    assert [c["label"] for c in data["candidates"]] == ["T1", "T2", "D1"]
    assert data["candidates"][1]["source"] == "ocr"
    assert data["candidates"][1]["description"] == "Welcome (exact text match)"
    assert data["candidates"][2] == detected
    assert data["fallback_message"] == "two Send buttons"


def test_merge_prepass_keeps_structural_hits_over_an_unreadable_engine_answer():
    prepass = [{"label": "T1", "coords": [1, 2]}]
    merged = json.loads(Explorer._merge_prepass(prepass, "Error: max iterations"))
    assert merged == {"candidates": prepass, "fallback_message": "Error: max iterations"}
    assert Explorer._merge_prepass([], "raw") == "raw"


@pytest.mark.asyncio
async def test_prepass_never_runs_on_the_fly_ocr_for_flash(screenshot):
    record = SimpleNamespace(ui_tree=HIERARCHY[:3], ocr_result=None)
    explorer = Explorer(_context())
    explorer._run_flash = AsyncMock(return_value=_outcome())
    explorer._run_loop = AsyncMock(return_value=_outcome())

    with (
        patch(f"{RUN_SETUP}.StorageManager", return_value=_storage(record)),
        patch(f"{RUN_SETUP}.is_ocr_configured", return_value=True),
        patch(f"{RUN_SETUP}.perform_ocr", new_callable=AsyncMock, return_value=[]) as perform_ocr,
    ):
        await explorer.run("gear icon", "", screenshot, _state(screenshot, []), version="flash")
        perform_ocr.assert_not_awaited()
        assert len(explorer.screen_index) == 3  # the record still feeds the index

        await explorer.run("gear icon", "", screenshot, _state(screenshot, []), version="pro")
        perform_ocr.assert_awaited_once()
