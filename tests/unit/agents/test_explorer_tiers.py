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

"""Explorer tier integration: turn budgets, tool exposure, prompts."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
import pytest

from artemis.agents.explorer.explorer import Explorer
from artemis.agents.explorer.tiers import EXPLORER_TIERS, PERCEPTION_TOOLS, get_tier
from artemis.context import ArtemisContext
from artemis.graph.state import State

EXPLORER = "artemis.agents.explorer.explorer"
RUN_SETUP = "artemis.agents.explorer.run_setup"
UNIVERSAL_RUNNER = "artemis.agents.explorer.universal_runner"


def _context(model: str = "gemini-3.8-flash", denylist: list[str] | None = None) -> MagicMock:
    ctx = MagicMock(spec=ArtemisContext)
    ctx.device = MagicMock()
    ctx.device.device_width = 1080
    ctx.device.device_height = 2400
    ctx.data_engine = MagicMock()
    ctx.data_engine.base_dir = None
    ctx.llm_config = MagicMock()
    ctx.llm_config.explorer = MagicMock()
    ctx.llm_config.explorer.model = model
    ctx.llm_config.explorer.temperature = 0.1
    ctx.llm_config.explorer.thinking_level = None
    ctx.llm_config.explorer.fallback = None
    ctx.agent_config = MagicMock()
    ctx.agent_config.denylisted_tools = {"explorer": denylist or []}
    return ctx


def _state(screenshot: str) -> MagicMock:
    state = MagicMock(spec=State)
    state.latest_screenshot = screenshot
    state.latest_ui_hierarchy = []
    state.operator_raw_data = {}
    return state


def _submit_response(coords=(500, 600)) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "submit_answer",
                "args": {
                    "candidates": [
                        {"label": "D1", "coords": list(coords), "description": "target"}
                    ],
                    "fallback_message": "",
                },
                "id": "call_submit",
            }
        ],
    )


def _chat_model(*responses) -> MagicMock:
    llm = MagicMock()
    llm.bind_tools.return_value.ainvoke = AsyncMock(side_effect=list(responses))
    return llm


@pytest.fixture
def screenshot(tmp_path):
    path = tmp_path / "screen.jpg"
    path.write_bytes(b"fake-jpeg-bytes")
    return str(path)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    monkeypatch.delenv("ARTEMIS_EXPLORER_CACHING", raising=False)
    monkeypatch.setenv("ARTEMIS_USE_FILE_API", "false")


# --------------------------------------------------------------------------- #
# Tier -> turn budget / tool exposure
# --------------------------------------------------------------------------- #


def test_construction_applies_no_tier():
    explorer = Explorer(_context())
    assert explorer.tier is None
    assert explorer.max_turns == 0


@pytest.mark.parametrize(
    ("version", "max_turns", "expected_tools"),
    [
        ("pro", 3, {"ask_perception_tool", "submit_answer"}),
        ("ultra", 8, PERCEPTION_TOOLS | {"submit_answer"}),
    ],
)
def test_tier_sets_turn_budget_and_exposed_tools(version, max_turns, expected_tools):
    explorer = Explorer(_context())
    with patch(f"{EXPLORER}.is_ocr_configured", return_value=True):
        explorer._apply_tier(get_tier(version))
        universal_names = {t["function"]["name"] for t in explorer._universal_exposed_tools()}

    assert explorer.max_turns == max_turns
    assert universal_names == expected_tools
    assert explorer.denylisted_tools >= get_tier(version).hidden_tools


def test_unconfigured_ocr_and_user_denylist_hide_tools_on_ultra():
    explorer = Explorer(_context(denylist=["inspect_region"]))
    with patch(f"{EXPLORER}.is_ocr_configured", return_value=False):
        explorer._apply_tier(get_tier("ultra"))
    names = {t["function"]["name"] for t in explorer._universal_exposed_tools()}
    assert "get_ocr_list" not in names
    assert "inspect_region" not in names
    assert {"detect_objects", "ask_image_processor", "ask_perception_tool"} <= names


# --------------------------------------------------------------------------- #
# Universal run
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pro_run_binds_only_tier_tools(screenshot):
    ctx = _context(model="claude-sonnet-4")
    llm = _chat_model(_submit_response(coords=(100, 200)))
    storage = MagicMock()
    storage.get_image.return_value = None

    with (
        patch(f"{UNIVERSAL_RUNNER}.get_llm", return_value=llm),
        patch(f"{RUN_SETUP}.StorageManager", return_value=storage),
    ):
        explorer = Explorer(ctx)
        raw = await explorer.run("find it", "", screenshot, _state(screenshot), version="pro")

    outcome = json.loads(raw)
    assert outcome["candidates"][0]["coords"] == [100, 200]
    # The universal engine only sees the pro tier's tools.
    bound_names = {t["function"]["name"] for t in llm.bind_tools.call_args_list[0].args[0]}
    assert bound_names == {"ask_perception_tool", "submit_answer"}


# --------------------------------------------------------------------------- #
# Universal dispatch delivers tool images
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_universal_dispatch_appends_image_message(tmp_path):
    explorer = Explorer(_context(model="claude-sonnet-4"))
    img_a = tmp_path / "a.jpg"
    img_b = tmp_path / "b.jpg"
    img_a.write_bytes(b"aaa")
    img_b.write_bytes(b"bbb")
    explorer.exec_ask_perception_tool = AsyncMock(
        return_value={"text": "perception text", "image_paths": [str(img_a), str(tmp_path / "x")]}
    )
    explorer.exec_inspect_region = AsyncMock(
        return_value={"text": "zoomed", "image_path": str(img_b)}
    )
    messages = []
    tool_calls = [
        {
            "name": "ask_perception_tool",
            "args": {"search_query": "q", "nx": 1, "ny": 2, "detect_queries": ["q"]},
            "id": "c1",
        },
        {
            "name": "inspect_region",
            "args": {"x_min": 0, "y_min": 0, "x_max": 10, "y_max": 10},
            "id": "c2",
        },
    ]

    await explorer._universal_dispatch_tools(tool_calls, messages, turn=1)

    assert [type(m) for m in messages] == [ToolMessage, ToolMessage, HumanMessage]
    assert messages[0].content == "perception text"
    human = messages[2]
    assert human.content[0] == {
        "type": "text",
        "text": "[Annotated image(s) returned by: ask_perception_tool, inspect_region]",
    }
    image_blocks = human.content[1:]
    assert len(image_blocks) == 2  # the missing file is skipped
    assert all(b["type"] == "image_url" for b in image_blocks)
    assert image_blocks[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")


@pytest.mark.asyncio
async def test_universal_dispatch_without_images_adds_no_human_message():
    explorer = Explorer(_context(model="claude-sonnet-4"))
    explorer.exec_get_ocr_list = AsyncMock(return_value={"text": "none", "image_path": None})
    messages = []
    await explorer._universal_dispatch_tools(
        [{"name": "get_ocr_list", "args": {}, "id": "c1"}], messages, turn=1
    )
    assert [type(m) for m in messages] == [ToolMessage]


@pytest.mark.asyncio
async def test_universal_dispatch_rejects_denylisted_tool():
    explorer = Explorer(_context(model="claude-sonnet-4"))
    explorer._apply_tier(get_tier("pro"))
    explorer.exec_inspect_region = AsyncMock()
    messages = []
    await explorer._universal_dispatch_tools(
        [{"name": "inspect_region", "args": {}, "id": "c1"}], messages, turn=1
    )
    explorer.exec_inspect_region.assert_not_called()
    assert "denylisted" in messages[0].content


# --------------------------------------------------------------------------- #
# Tier-aware prompt
# --------------------------------------------------------------------------- #


def test_pro_prompt_only_describes_exposed_tools():
    explorer = Explorer(_context())
    explorer._apply_tier(get_tier("pro"))
    prompt, error = explorer._build_prompt_template(get_tier("pro"))
    assert error is None
    assert "ask_perception_tool" in prompt
    assert "inspect_region" not in prompt
    assert "Image Pool" not in prompt
    assert "ask_image_processor" not in prompt
    assert "get_ocr_list" not in prompt
    assert "# TOOL DENYLIST" not in prompt
    assert "# EXECUTION CONSTRAINT" in prompt
    assert "maximum of 3 turns" in prompt


def test_ultra_prompt_describes_all_tools():
    explorer = Explorer(_context())
    with patch(f"{EXPLORER}.is_ocr_configured", return_value=True):
        explorer._apply_tier(get_tier("ultra"))
        prompt, error = explorer._build_prompt_template(get_tier("ultra"))
    assert error is None
    for tool in PERCEPTION_TOOLS:
        assert tool in prompt
    assert "Image Pool" in prompt
    assert "maximum of 8 turns" in prompt
    assert "# TOOL DENYLIST" not in prompt


def test_prompt_denylist_section_only_for_user_denied_tier_tools():
    explorer = Explorer(_context(denylist=["inspect_region", "search_xml_ocr"]))
    with patch(f"{EXPLORER}.is_ocr_configured", return_value=True):
        explorer._apply_tier(get_tier("ultra"))
        ultra_prompt, _ = explorer._build_prompt_template(get_tier("ultra"))
        explorer._apply_tier(get_tier("pro"))
        pro_prompt, _ = explorer._build_prompt_template(get_tier("pro"))

    assert "# TOOL DENYLIST" in ultra_prompt
    assert "cannot be used: inspect_region" in ultra_prompt
    assert "search_xml_ocr" not in ultra_prompt
    # Denied tools lose their guidance bullets too.
    assert "Targeted Verification" not in ultra_prompt
    # On pro, inspect_region is tier-hidden, so it is never mentioned.
    assert "# TOOL DENYLIST" not in pro_prompt
    assert "inspect_region" not in pro_prompt


def test_prompt_sections_render_shape():
    explorer = Explorer(_context())
    explorer._apply_tier(get_tier("pro"))
    prompt, _ = explorer._build_prompt_template(get_tier("pro"))
    lines = prompt.splitlines()
    assert lines[0] == "# IDENTITY"
    assert any(line.startswith("# OPERATING PRINCIPLES") for line in lines)
    body = [line for line in lines if line and not line.startswith("#")]
    assert all(line.startswith("- ") or line.startswith("You are") for line in body)


# --------------------------------------------------------------------------- #
# Flash path
# --------------------------------------------------------------------------- #
# HTTP client lifecycle
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_run_closes_http_client_when_the_loop_fails(screenshot):
    storage = MagicMock()
    storage.get_image.return_value = None
    explorer = Explorer(_context(model="claude-sonnet-4"))
    http_client = MagicMock()
    http_client.aclose = AsyncMock()
    explorer.http_client = http_client

    llm = MagicMock()
    llm.bind_tools.return_value.ainvoke = AsyncMock(side_effect=RuntimeError("boom"))
    with (
        patch(f"{RUN_SETUP}.StorageManager", return_value=storage),
        patch(f"{UNIVERSAL_RUNNER}.get_llm", return_value=llm),
        pytest.raises(RuntimeError),
    ):
        await explorer.run("t", "", screenshot, _state(screenshot), version="pro")

    http_client.aclose.assert_awaited_once()
    assert explorer.http_client is None


def test_tier_table_is_single_source_for_versions():
    assert set(EXPLORER_TIERS) == {"flash", "pro", "ultra"}
    assert get_tier("bogus").name == "flash"


@pytest.mark.parametrize("version", [None, "", "turbo", "PRO ", "Ultra"])
def test_get_tier_normalizes_or_falls_back(version):
    """Unknown or empty names fall back to the default tier; case and spaces are tolerated."""
    tier = get_tier(version)
    normalized = str(version or "").strip().lower()
    expected = normalized if normalized in EXPLORER_TIERS else "flash"
    assert tier.name == expected
