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

"""``ask_explorer``: tier-agnostic contract and the locate / register / render pipeline."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from artemis.agents.explorer.constants import (
    ASK_EXPLORER_CONTEXT_FEEDBACK_DESCRIPTION,
    ASK_EXPLORER_DESCRIPTION,
    ASK_EXPLORER_QUERY_DESCRIPTION,
)
from artemis.context import ArtemisContext
from artemis.graph.state import State
from artemis.tools.base import ArtemisTool, ToolRegistry
from artemis.tools.explorer_tool import (
    AskExplorerArgs,
    AskExplorerTool,
    ExplorerCandidate,
    ExplorerOutcome,
    RegisteredCandidate,
    _run_explorer_logic,
    ask_explorer,
    ask_explorer_text,
    ask_explorer_wrapper,
    get_ask_explorer_tool,
    locate,
    register_candidates,
    render_operator_blocks,
    render_text,
)

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _no_env_tier(monkeypatch):
    """The environment override must not leak into tier-resolution assertions."""
    monkeypatch.delenv("ARTEMIS_EXPLORER_VERSION", raising=False)


def _ctx(width: int | None = 1080, height: int | None = 2400, base_dir: Path | None = None):
    ctx = MagicMock(spec=ArtemisContext)
    ctx.agent_config = None
    ctx.llm_config = None
    ctx.execution_setup = None
    if base_dir is None:
        ctx.data_engine = None
    else:
        ctx.data_engine = MagicMock()
        ctx.data_engine.base_dir = str(base_dir)
    if width is None:
        ctx.device = None
    else:
        ctx.device = MagicMock()
        ctx.device.device_width = width
        ctx.device.device_height = height
    return ctx


def _state(screenshot: str | None = "/tmp/test.jpg", raw: dict | None = None):
    state = MagicMock(spec=State)
    state.latest_screenshot = screenshot
    state.indexed_points = []
    state.indexed_elements = []
    state.operator_raw_data = raw
    return state


def _explorer_returning(raw):
    instance = MagicMock()
    instance.run = AsyncMock(return_value=raw)
    return patch("artemis.tools.explorer_tool.Explorer", return_value=instance), instance


# --------------------------------------------------------------------------- #
# Contract
# --------------------------------------------------------------------------- #


def test_tool_contract_is_tier_agnostic():
    assert issubclass(AskExplorerTool, ArtemisTool)
    assert isinstance(ask_explorer, AskExplorerTool)
    assert ask_explorer.name == "ask_explorer"
    assert ask_explorer.category == "explorer"
    assert ask_explorer.args_schema is AskExplorerArgs

    fields = AskExplorerArgs.model_fields
    assert set(fields) == {"query", "context_feedback"}
    assert fields["query"].is_required()
    assert fields["query"].description == ASK_EXPLORER_QUERY_DESCRIPTION
    assert fields["context_feedback"].default == ""
    assert fields["context_feedback"].description == ASK_EXPLORER_CONTEXT_FEEDBACK_DESCRIPTION

    declaration = ask_explorer.to_genai_declaration()
    assert declaration.name == "ask_explorer"
    assert set(declaration.parameters.properties) == {"query", "context_feedback"}
    assert declaration.parameters.required == ["query"]


@pytest.mark.parametrize("version", [None, "flash", "pro", "ultra"])
def test_description_does_not_depend_on_the_tier(version):
    tool = AskExplorerTool(version=version)
    assert tool.description == ASK_EXPLORER_DESCRIPTION
    assert tool.version == version


def test_registry_and_wrapper_export():
    reg_tool = ToolRegistry.get("ask_explorer")
    assert isinstance(reg_tool, AskExplorerTool)
    assert ask_explorer_wrapper.tool_fn_getter is get_ask_explorer_tool
    assert ask_explorer_wrapper.on_success_fn("x") == "x"
    assert "boom" in ask_explorer_wrapper.on_failure_fn("boom")


# --------------------------------------------------------------------------- #
# ExplorerOutcome.from_raw
# --------------------------------------------------------------------------- #


def test_from_raw_parses_candidates_and_message():
    raw = json.dumps(
        {
            "candidates": [
                {"label": "S1", "coords": [500, 500], "description": "First Button"},
                {"label": "S2", "coords": [250, 750], "description": "Second Button"},
            ],
            "fallback_message": "Please choose carefully.",
        }
    )
    outcome = ExplorerOutcome.from_raw(raw)
    assert outcome.found and not outcome.error
    assert outcome.candidates == [
        ExplorerCandidate(label="S1", coords=(500, 500), description="First Button"),
        ExplorerCandidate(label="S2", coords=(250, 750), description="Second Button"),
    ]
    assert outcome.message == "Please choose carefully."
    assert outcome.raw == raw


def test_from_raw_accepts_a_dict_and_fills_defaults():
    outcome = ExplorerOutcome.from_raw(
        {"candidates": [{"coords": ["10", "20"]}]}, default_label="gear icon"
    )
    assert len(outcome.candidates) == 1
    cand = outcome.candidates[0]
    assert cand.label == "gear icon"
    assert cand.description == "gear icon"
    assert cand.coords == (10, 20)
    assert outcome.message == ""


def test_from_raw_drops_candidates_with_invalid_coords():
    outcome = ExplorerOutcome.from_raw(
        {
            "candidates": [
                {"label": "bad1", "coords": [1500, 10]},
                {"label": "bad2", "coords": [10]},
                {"label": "bad3", "coords": "10,10"},
                {"label": "bad4"},
                "not a dict",
                {"label": "ok", "coords": [10, 10]},
            ]
        }
    )
    assert [c.label for c in outcome.candidates] == ["ok"]
    assert not outcome.error


def test_from_raw_unreadable_text_is_an_error_outcome():
    outcome = ExplorerOutcome.from_raw("I could not parse the screen {")
    assert outcome.error and not outcome.found
    assert "unreadable answer" in outcome.message
    assert outcome.raw == "I could not parse the screen {"


def test_from_raw_non_object_json_is_an_error_outcome():
    outcome = ExplorerOutcome.from_raw("[1, 2, 3]")
    assert outcome.error
    assert "unexpected answer" in outcome.message


def test_from_raw_empty_candidates_is_a_clean_not_found():
    outcome = ExplorerOutcome.from_raw({"candidates": [], "fallback_message": "Covered."})
    assert not outcome.found and not outcome.error
    assert outcome.message == "Covered."


# --------------------------------------------------------------------------- #
# register_candidates
# --------------------------------------------------------------------------- #


def test_register_candidates_appends_after_existing_elements_using_observation_size():
    ctx = _ctx()
    state = _state(raw={"width": 1000, "height": 2000})
    state.indexed_points = [[100, 200]]
    state.indexed_elements = [{"index": 1, "center": [100, 200], "text": "Pre-existing"}]
    outcome = ExplorerOutcome(
        candidates=[
            ExplorerCandidate(label="S1", coords=(500, 500), description="First Button"),
            ExplorerCandidate(label="S2", coords=(250, 750), description="Second Button"),
        ]
    )

    registered = register_candidates(ctx, state, outcome)

    assert registered == [
        RegisteredCandidate(
            index=2, pixel=(500, 1000), coords=(500, 500), description="First Button"
        ),
        RegisteredCandidate(
            index=3, pixel=(250, 1500), coords=(250, 750), description="Second Button"
        ),
    ]
    assert state.indexed_points == [[100, 200], [500, 1000], [250, 1500]]
    assert len(state.indexed_elements) == 3
    assert state.indexed_elements[1] == {
        "index": 2,
        "center": [500, 1000],
        "text": "First Button",
        "bounds": None,
        "class": "ExplorerCandidate",
        "resource_id": None,
        "is_ocr": False,
    }
    assert state.indexed_elements[2]["index"] == 3
    assert state.indexed_elements[2]["center"] == [250, 1500]


def test_register_candidates_falls_back_to_the_device_size():
    ctx = _ctx(1080, 2400)
    state = _state(raw=None)
    outcome = ExplorerOutcome(candidates=[ExplorerCandidate("S1", (500, 500), "Btn")])
    registered = register_candidates(ctx, state, outcome)
    assert registered[0].pixel == (540, 1200)
    assert registered[0].index == 1


def test_register_candidates_creates_the_lists_when_the_state_has_none():
    ctx = _ctx()
    state = MagicMock(spec=State)
    state.indexed_points = None
    state.indexed_elements = None
    state.operator_raw_data = None
    outcome = ExplorerOutcome(candidates=[ExplorerCandidate("S1", (0, 0), "Origin")])
    registered = register_candidates(ctx, state, outcome)
    assert registered[0].index == 1
    assert state.indexed_points == [[0, 0]]
    assert state.indexed_elements[0]["text"] == "Origin"


def test_register_candidates_is_a_no_op_without_candidates():
    ctx = _ctx()
    state = _state()
    assert register_candidates(ctx, state, ExplorerOutcome(message="nope")) == []
    assert register_candidates(ctx, state, ExplorerOutcome.failure("boom")) == []
    assert state.indexed_points == []
    assert state.indexed_elements == []


# --------------------------------------------------------------------------- #
# render_text / render_operator_blocks
# --------------------------------------------------------------------------- #


def test_render_text_found_lists_indices_and_coordinates():
    outcome = ExplorerOutcome(
        candidates=[ExplorerCandidate("S1", (500, 500), "First Button")],
        message="Please choose carefully.",
    )
    registered = [RegisteredCandidate(2, (540, 1200), (500, 500), "First Button")]
    text = render_text("Find buttons", outcome, registered)
    assert "Explorer located 1 candidate(s) for 'Find buttons'" in text
    assert "- [2] 'First Button' at normalized [500, 500]" in text
    assert "target=[index]" in text
    assert "Explorer notes: Please choose carefully." in text


def test_render_text_not_found_explains_and_suggests_rephrasing():
    text = render_text("hidden button", ExplorerOutcome(message="Covered by keyboard."), [])
    assert text.startswith("Explorer could not locate 'hidden button'. Covered by keyboard.")
    assert "describing the target differently" in text
    # Without an explanation the text still reads as a complete sentence.
    text = render_text("x", ExplorerOutcome(), [])
    assert "It gave no further detail." in text


def test_render_text_error_reports_the_failure():
    text = render_text("gear icon", ExplorerOutcome.failure("Explorer failed: quota"), [])
    assert text == "Explorer could not run for 'gear icon'. Explorer failed: quota"


def test_render_operator_blocks_adds_the_annotated_image(tmp_path):
    ctx = _ctx(base_dir=tmp_path)
    state = _state(screenshot=str(tmp_path / "shot.jpg"))
    outcome = ExplorerOutcome(candidates=[ExplorerCandidate("S1", (500, 500), "First Button")])
    registered = [RegisteredCandidate(2, (540, 1200), (500, 500), "First Button")]

    def fake_draw_dots(screenshot_path, points, labels, output_path, **kwargs):
        Path(output_path).write_bytes(b"fake_annotated_image_bytes")

    with patch("artemis.tools.explorer_tool.draw_dots", side_effect=fake_draw_dots) as draw:
        result = render_operator_blocks(ctx, state, "Find buttons", outcome, registered)

    draw.assert_called_once()
    args, kwargs = draw.call_args
    assert args[0] == str(tmp_path / "shot.jpg")
    assert args[1] == [[540, 1200]]
    assert args[2] == ["2"]
    assert Path(args[3]).parent == tmp_path / "images" / "explorer_tool"
    assert kwargs["color"] == "magenta"

    assert isinstance(result, list) and len(result) == 2
    assert result[0]["type"] == "text"
    assert "Explorer located 1 candidate(s)" in result[0]["text"]
    assert result[1]["type"] == "image_url"
    assert result[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_render_operator_blocks_numbers_annotations_sequentially(tmp_path):
    ctx = _ctx(base_dir=tmp_path)
    state = _state(screenshot=str(tmp_path / "shot.jpg"))
    outcome = ExplorerOutcome(candidates=[ExplorerCandidate("S1", (500, 500), "B")])
    registered = [RegisteredCandidate(1, (540, 1200), (500, 500), "B")]
    seen: list[str] = []

    def fake_draw_dots(screenshot_path, points, labels, output_path, **kwargs):
        seen.append(Path(output_path).name)
        Path(output_path).write_bytes(b"img")

    with patch("artemis.tools.explorer_tool.draw_dots", side_effect=fake_draw_dots):
        render_operator_blocks(ctx, state, "B", outcome, registered)
        render_operator_blocks(ctx, state, "B", outcome, registered)
    assert seen == ["explorer_output_1.jpg", "explorer_output_2.jpg"]


def test_render_operator_blocks_degrades_to_text_when_drawing_fails(tmp_path):
    ctx = _ctx(base_dir=tmp_path)
    state = _state(screenshot=str(tmp_path / "shot.jpg"))
    outcome = ExplorerOutcome(candidates=[ExplorerCandidate("S1", (500, 500), "First Button")])
    registered = [RegisteredCandidate(2, (540, 1200), (500, 500), "First Button")]

    with patch("artemis.tools.explorer_tool.draw_dots", side_effect=OSError("no PIL")):
        result = render_operator_blocks(ctx, state, "Find buttons", outcome, registered)

    assert isinstance(result, str)
    assert "Explorer located 1 candidate(s)" in result


def test_render_operator_blocks_is_plain_text_without_candidates(tmp_path):
    ctx = _ctx(base_dir=tmp_path)
    state = _state(screenshot=str(tmp_path / "shot.jpg"))
    with patch("artemis.tools.explorer_tool.draw_dots") as draw:
        result = render_operator_blocks(ctx, state, "x", ExplorerOutcome(message="Nope."), [])
    draw.assert_not_called()
    assert isinstance(result, str)
    assert "Explorer could not locate 'x'. Nope." in result


# --------------------------------------------------------------------------- #
# locate
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_locate_runs_the_resolved_tier_and_parses_the_answer():
    ctx = _ctx()
    state = _state()
    raw = json.dumps({"candidates": [{"label": "S1", "coords": [500, 500]}]})
    explorer_patch, instance = _explorer_returning(raw)
    with (
        explorer_patch,
        patch(
            "artemis.tools.explorer_tool.resolve_explorer_version", return_value="ultra"
        ) as resolve,
    ):
        outcome = await locate(ctx, state, "gear icon", "was wrong", agent_name="validator")

    resolve.assert_called_once_with(ctx, explicit_version=None, agent_or_profile_name="validator")
    instance.run.assert_awaited_once_with(
        "gear icon", "was wrong", "/tmp/test.jpg", state, version="ultra"
    )
    assert outcome.found and not outcome.error
    assert outcome.candidates[0].coords == (500, 500)


@pytest.mark.asyncio
async def test_locate_forwards_a_programmatic_version_pin():
    ctx = _ctx()
    state = _state()
    explorer_patch, instance = _explorer_returning(json.dumps({"candidates": []}))
    with (
        explorer_patch,
        patch(
            "artemis.tools.explorer_tool.resolve_explorer_version", return_value="pro"
        ) as resolve,
    ):
        await locate(ctx, state, "q", version="pro", agent_name="flash")
    resolve.assert_called_once_with(ctx, explicit_version="pro", agent_or_profile_name="flash")
    assert instance.run.await_args.kwargs["version"] == "pro"


@pytest.mark.asyncio
async def test_locate_unknown_resolved_tier_falls_back_to_the_default_tier():
    ctx = _ctx()
    state = _state()
    explorer_patch, instance = _explorer_returning(json.dumps({"candidates": []}))
    with (
        explorer_patch,
        patch("artemis.tools.explorer_tool.resolve_explorer_version", return_value="turbo"),
    ):
        await locate(ctx, state, "q")
    assert instance.run.await_args.kwargs["version"] == "flash"


@pytest.mark.asyncio
async def test_locate_without_a_screenshot_is_an_error_outcome():
    ctx = _ctx()
    state = _state(screenshot=None)
    with patch("artemis.tools.explorer_tool.Explorer") as explorer_cls:
        outcome = await locate(ctx, state, "gear icon")
    explorer_cls.assert_not_called()
    assert outcome.error
    assert "No screenshot" in outcome.message


@pytest.mark.asyncio
async def test_locate_contains_explorer_exceptions_as_error_outcomes():
    ctx = _ctx()
    state = _state()
    instance = MagicMock()
    instance.run = AsyncMock(side_effect=RuntimeError("quota exhausted"))
    with patch("artemis.tools.explorer_tool.Explorer", return_value=instance):
        outcome = await locate(ctx, state, "gear icon")
    assert outcome.error and not outcome.found
    assert outcome.message == "Explorer failed: quota exhausted"
    assert "could not run for 'gear icon'" in render_text("gear icon", outcome, [])


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_ask_explorer_text_end_to_end():
    ctx = _ctx()
    state = _state()
    state.indexed_points = [[100, 200]]
    state.indexed_elements = [{"index": 1, "center": [100, 200], "text": "Pre-existing"}]
    raw = json.dumps(
        {
            "candidates": [
                {"label": "S1", "coords": [500, 500], "description": "First Button"},
                {"label": "S2", "coords": [250, 750], "description": "Second Button"},
            ],
            "fallback_message": "Please choose carefully.",
        }
    )
    explorer_patch, instance = _explorer_returning(raw)
    with explorer_patch:
        text = await ask_explorer_text(ctx, state, "Find buttons", "Attempt 1", agent_name="flash")

    instance.run.assert_awaited_once()
    assert instance.run.await_args.args[:2] == ("Find buttons", "Attempt 1")
    assert state.indexed_points == [[100, 200], [540, 1200], [270, 1800]]
    assert state.indexed_elements[1]["text"] == "First Button"
    assert state.indexed_elements[2]["text"] == "Second Button"
    assert "Explorer located 2 candidate(s) for 'Find buttons'" in text
    assert "- [2] 'First Button' at normalized [500, 500]" in text
    assert "- [3] 'Second Button' at normalized [250, 750]" in text
    assert "Explorer notes: Please choose carefully." in text


@pytest.mark.asyncio
async def test_ask_explorer_text_not_found_leaves_the_state_untouched():
    ctx = _ctx()
    state = _state()
    explorer_patch, _ = _explorer_returning(
        json.dumps({"candidates": [], "fallback_message": "Covered by a keyboard."})
    )
    with explorer_patch:
        text = await ask_explorer_text(ctx, state, "hidden button")
    assert state.indexed_points == []
    assert state.indexed_elements == []
    assert "Explorer could not locate 'hidden button'. Covered by a keyboard." in text


@pytest.mark.asyncio
async def test_langchain_tool_uses_the_operator_presentation(tmp_path):
    ctx = _ctx(base_dir=tmp_path)
    state = _state(screenshot=str(tmp_path / "shot.jpg"))
    raw = json.dumps({"candidates": [{"label": "S1", "coords": [500, 500], "description": "B"}]})
    explorer_patch, _ = _explorer_returning(raw)

    def fake_draw_dots(screenshot_path, points, labels, output_path, **kwargs):
        Path(output_path).write_bytes(b"img")

    with explorer_patch, patch("artemis.tools.explorer_tool.draw_dots", side_effect=fake_draw_dots):
        tool = get_ask_explorer_tool(ctx)
        result = await tool.ainvoke(
            {"query": "Find buttons", "context_feedback": "", "state": state}
        )

    assert tool.name == "ask_explorer"
    assert isinstance(result, list) and result[1]["type"] == "image_url"
    assert "- [1] 'B' at normalized [500, 500]" in result[0]["text"]


@pytest.mark.asyncio
async def test_direct_execute_accepts_a_version_pin_and_legacy_arg_names():
    ctx = _ctx(width=None)
    state = _state()
    explorer_patch, instance = _explorer_returning(
        json.dumps({"candidates": [], "fallback_message": "Direct execution not found."})
    )
    with explorer_patch:
        result = await ask_explorer.execute(
            ctx=ctx, state=state, Query="Find icon", ContextFeedback="cf", version="pro"
        )
    assert instance.run.await_args.args[:2] == ("Find icon", "cf")
    assert instance.run.await_args.kwargs["version"] == "pro"
    assert "Direct execution not found." in result


@pytest.mark.asyncio
async def test_run_explorer_logic_replay_shim_returns_operator_blocks(tmp_path):
    ctx = _ctx(base_dir=tmp_path)
    state = _state(screenshot=str(tmp_path / "shot.jpg"))
    raw = json.dumps({"candidates": [{"label": "S1", "coords": [500, 500], "description": "B"}]})
    explorer_patch, instance = _explorer_returning(raw)

    def fake_draw_dots(screenshot_path, points, labels, output_path, **kwargs):
        Path(output_path).write_bytes(b"img")

    with explorer_patch, patch("artemis.tools.explorer_tool.draw_dots", side_effect=fake_draw_dots):
        result = await _run_explorer_logic(ctx, state, "B", "", version="ultra")

    assert instance.run.await_args.kwargs["version"] == "ultra"
    assert isinstance(result, list) and len(result) == 2
    assert result[0]["type"] == "text" and result[1]["type"] == "image_url"
    assert state.indexed_points == [[540, 1200]]


# --------------------------------------------------------------------------- #
# Bounds and de-duplication against the existing index
# --------------------------------------------------------------------------- #


def test_from_raw_parses_bounds_and_source_and_drops_invalid_bounds():
    raw = json.dumps(
        {
            "candidates": [
                {
                    "label": "X1",
                    "coords": [500, 500],
                    "bounds": [400, 450, 600, 550],
                    "source": "xml",
                },
                {"label": "D1", "coords": [100, 100], "bounds": [600, 550, 400, 450]},
                {"label": "D2", "coords": [100, 200], "bounds": [0, 0, 1200, 10]},
            ]
        }
    )
    outcome = ExplorerOutcome.from_raw(raw)
    assert outcome.candidates[0].bounds == (400, 450, 600, 550)
    assert outcome.candidates[0].source == "xml"
    assert outcome.candidates[1].bounds is None
    assert outcome.candidates[2].bounds is None
    assert outcome.candidates[1].source == ""


def test_register_candidates_reuses_an_indexed_element_that_contains_the_point():
    ctx = _ctx()
    state = _state(raw={"width": 1000, "height": 2000})
    state.indexed_points = [[300, 400], [800, 1600]]
    state.indexed_elements = [
        {"index": 1, "center": [300, 400], "text": "Gmail", "bounds": [200, 300, 400, 500]},
        {"index": 2, "center": [800, 1600], "text": "Send", "bounds": [700, 1500, 900, 1700]},
    ]
    outcome = ExplorerOutcome(candidates=[ExplorerCandidate("D1", (310, 210), "Gmail app icon")])
    registered = register_candidates(ctx, state, outcome)
    assert registered == [
        RegisteredCandidate(
            index=1,
            pixel=(310, 420),
            coords=(310, 210),
            description="Gmail app icon",
            bounds=(200, 300, 400, 500),
            reused=True,
        )
    ]
    assert len(state.indexed_points) == 2  # nothing appended
    assert "already in your indexed list" in render_text("Gmail app icon", outcome, registered)


def test_register_candidates_reuses_a_bare_center_within_the_dedup_radius_only():
    ctx = _ctx()
    state = _state(raw={"width": 1000, "height": 2000})
    state.indexed_points = [[500, 1000]]
    state.indexed_elements = [
        {"index": 1, "center": [500, 1000], "text": "Earlier candidate", "bounds": None}
    ]
    near = ExplorerCandidate("D1", (510, 505), "Same spot")  # 10 x 10 px away
    far = ExplorerCandidate("D2", (560, 500), "Sixty px right")
    registered = register_candidates(ctx, state, ExplorerOutcome(candidates=[near, far]))
    assert [r.reused for r in registered] == [True, False]
    assert registered[0].index == 1
    assert registered[1].index == 2
    assert state.indexed_points == [[500, 1000], [560, 1000]]


def test_register_candidates_stores_pixel_bounds_and_ocr_flag():
    ctx = _ctx()
    state = _state(raw={"width": 1000, "height": 2000})
    outcome = ExplorerOutcome(
        candidates=[
            ExplorerCandidate("O3", (500, 500), "Search", bounds=(400, 450, 600, 550), source="ocr")
        ]
    )
    registered = register_candidates(ctx, state, outcome)
    assert registered[0].bounds == (400, 900, 600, 1100)
    assert registered[0].reused is False
    element = state.indexed_elements[0]
    assert element["bounds"] == [400, 900, 600, 1100]
    assert element["is_ocr"] is True
