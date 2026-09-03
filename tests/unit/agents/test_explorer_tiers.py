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

"""Explorer tier integration: engine selection, tool exposure, caching, prompts."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from google.genai import types
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
import pytest

from artemis.agents.explorer.explorer import Explorer
from artemis.agents.explorer.tiers import EXPLORER_TIERS, PERCEPTION_TOOLS, get_tier
from artemis.config import settings
from artemis.context import ArtemisContext
from artemis.graph.state import State

FACADE = "artemis.agents.explorer.explorer"


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
    ctx.agent_config.explorer = SimpleNamespace(caching=None)
    ctx.execution_setup = SimpleNamespace(explorer=SimpleNamespace(caching=None))
    return ctx


def _state(screenshot: str) -> MagicMock:
    state = MagicMock(spec=State)
    state.latest_screenshot = screenshot
    state.latest_ui_hierarchy = []
    state.operator_raw_data = {}
    return state


def _submit_response(coords=(500, 600)) -> MagicMock:
    call = MagicMock()
    call.name = "submit_answer"
    call.args = {
        "candidates": [{"label": "D1", "coords": list(coords), "description": "target"}],
        "fallback_message": "",
    }
    response = MagicMock()
    response.function_calls = [call]
    response.candidates = [MagicMock()]
    response.candidates[0].content = types.Content(
        role="model",
        parts=[types.Part.from_function_call(name="submit_answer", args=call.args)],
    )
    return response


def _native_client(responses) -> MagicMock:
    client = MagicMock()
    client.aio.models.generate_content = AsyncMock(side_effect=list(responses))
    cache = SimpleNamespace(name="cachedContents/abc")
    client.aio.caches.create = AsyncMock(return_value=cache)
    client.aio.caches.delete = AsyncMock()
    client.aio.files.upload = AsyncMock(
        return_value=SimpleNamespace(uri="files/1", mime_type="image/jpeg", name="files/1")
    )
    client.aio.files.delete = AsyncMock()
    return client


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


def test_construction_creates_no_genai_client():
    with patch(f"{FACADE}.genai.Client") as client_cls:
        explorer = Explorer(_context())
    client_cls.assert_not_called()
    assert explorer.client is None
    assert explorer.tier is None


@pytest.mark.parametrize(
    ("version", "max_turns", "expected_tools"),
    [
        ("pro", 3, {"ask_perception_tool", "submit_answer"}),
        ("ultra", 8, PERCEPTION_TOOLS | {"submit_answer"}),
    ],
)
def test_tier_sets_turn_budget_and_exposed_tools(version, max_turns, expected_tools):
    explorer = Explorer(_context())
    with patch(f"{FACADE}.is_ocr_configured", return_value=True):
        explorer._apply_tier(get_tier(version))
        native_names = {t.name for t in explorer.get_exposed_tools()}
        universal_names = {t["function"]["name"] for t in explorer._universal_exposed_tools()}

    assert explorer.max_turns == max_turns
    assert native_names == expected_tools
    assert universal_names == expected_tools
    assert explorer.denylisted_tools >= get_tier(version).hidden_tools


def test_unconfigured_ocr_and_user_denylist_hide_tools_on_ultra():
    explorer = Explorer(_context(denylist=["inspect_region"]))
    with patch(f"{FACADE}.is_ocr_configured", return_value=False):
        explorer._apply_tier(get_tier("ultra"))
    names = {t.name for t in explorer.get_exposed_tools()}
    assert "get_ocr_list" not in names
    assert "inspect_region" not in names
    assert {"detect_objects", "ask_image_processor", "ask_perception_tool"} <= names


# --------------------------------------------------------------------------- #
# Engine selection
# --------------------------------------------------------------------------- #


def test_gemini_model_without_google_key_uses_universal_engine():
    with patch.object(settings, "GOOGLE_API_KEY", None):
        explorer = Explorer(_context(model="gemini-3.8-flash"))
    assert explorer.use_native_gemini is False


def test_gemini_model_with_shared_client_uses_native_engine():
    ctx = _context(model="google/gemini-3.8-pro")
    ctx._genai_client = MagicMock()
    with patch.object(settings, "GOOGLE_API_KEY", None):
        explorer = Explorer(ctx)
    assert explorer.use_native_gemini is True
    assert explorer.model_name == "gemini-3.8-pro"


@pytest.mark.asyncio
async def test_universal_run_without_google_key_never_touches_genai(screenshot):
    ctx = _context(model="claude-sonnet-4")
    response = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "submit_answer",
                "args": {
                    "candidates": [{"label": "1", "coords": [100, 200], "description": "ok"}],
                    "fallback_message": "",
                },
                "id": "call_1",
            }
        ],
    )
    llm = MagicMock()
    bound = MagicMock()
    bound.ainvoke = AsyncMock(return_value=response)
    llm.bind_tools.return_value = bound
    storage = MagicMock()
    storage.get_image.return_value = None

    with (
        patch.object(settings, "GOOGLE_API_KEY", None),
        patch(f"{FACADE}.genai.Client") as client_cls,
        patch(f"{FACADE}.get_llm", return_value=llm),
        patch(f"{FACADE}.StorageManager", return_value=storage),
    ):
        explorer = Explorer(ctx)
        raw = await explorer.run("find it", "", screenshot, _state(screenshot), version="pro")

    client_cls.assert_not_called()
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
# Caching precedence
# --------------------------------------------------------------------------- #


def test_caching_tier_defaults():
    explorer = Explorer(_context())
    with patch.object(settings, "EXPLORER_CACHING", None):
        assert explorer._resolve_caching(get_tier("pro"), None) is False
        assert explorer._resolve_caching(get_tier("ultra"), None) is True


def test_caching_precedence(monkeypatch):
    ctx = _context()
    explorer = Explorer(ctx)
    pro = get_tier("pro")

    with patch.object(settings, "EXPLORER_CACHING", None):
        ctx.agent_config.explorer = SimpleNamespace(caching=True)
        assert explorer._resolve_caching(pro, None) is True  # agent config beats tier

        monkeypatch.setenv("ARTEMIS_EXPLORER_CACHING", "false")
        assert explorer._resolve_caching(pro, None) is False  # env beats agent config

        assert explorer._resolve_caching(pro, True) is True  # explicit beats env

    with patch.object(settings, "EXPLORER_CACHING", True):
        monkeypatch.delenv("ARTEMIS_EXPLORER_CACHING")
        ctx.agent_config.explorer = SimpleNamespace(caching=None)
        ctx.execution_setup = SimpleNamespace(explorer=SimpleNamespace(caching=False))
        assert explorer._resolve_caching(pro, None) is False  # execution setup beats settings

        ctx.execution_setup = SimpleNamespace(explorer=SimpleNamespace(caching=None))
        assert explorer._resolve_caching(pro, None) is True  # settings beat tier


def test_caching_ignores_mock_config_values():
    """A MagicMock context must fall through to the tier default, not force True."""
    ctx = MagicMock()
    explorer = Explorer(ctx)
    with patch.object(settings, "EXPLORER_CACHING", None):
        assert explorer._resolve_caching(get_tier("pro"), None) is False


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
    with patch(f"{FACADE}.is_ocr_configured", return_value=True):
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
    with patch(f"{FACADE}.is_ocr_configured", return_value=True):
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


@pytest.mark.asyncio
async def test_flash_run_returns_detected_candidates(screenshot):
    detection = AsyncMock(
        return_value={
            "detected": [
                {"label": "gear icon", "point": [900, 100]},
                {"label": "send button", "point": [500, 950]},
            ],
            "failed": [],
        }
    )
    with (
        patch(f"{FACADE}._run_object_detection", detection),
        patch(f"{FACADE}.genai.Client") as client_cls,
    ):
        explorer = Explorer(_context())
        raw = await explorer.run(
            "gear icon | send button", "", screenshot, _state(screenshot), version="flash"
        )

    client_cls.assert_not_called()
    outcome = json.loads(raw)
    assert [c["label"] for c in outcome["candidates"]] == ["D1", "D2"]
    assert outcome["candidates"][0] == {
        "label": "D1",
        "coords": [900, 100],
        "description": "gear icon",
    }
    assert outcome["fallback_message"] == ""
    assert detection.call_args.args[2] == ["gear icon", "send button"]


@pytest.mark.asyncio
async def test_flash_run_reports_missing_detection(screenshot):
    with patch(f"{FACADE}._run_object_detection", AsyncMock(return_value={"detected": []})):
        raw = await Explorer(_context()).run(
            "ghost", "", screenshot, _state(screenshot), version="flash"
        )
    assert json.loads(raw) == {"candidates": [], "fallback_message": "Failed to detect: ghost"}


# --------------------------------------------------------------------------- #
# Native engine: async SDK usage and resource lifecycle
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_native_ultra_run_uses_async_cache_api(screenshot):
    ctx = _context()
    client = _native_client([_submit_response()])
    ctx._genai_client = client
    storage = MagicMock()
    storage.get_image.return_value = None

    with (
        patch(f"{FACADE}.genai.Client") as client_cls,
        patch(f"{FACADE}.StorageManager", return_value=storage),
        patch.object(settings, "EXPLORER_CACHING", None),
    ):
        explorer = Explorer(ctx)
        raw = await explorer.run("target", "", screenshot, _state(screenshot), version="ultra")

    client_cls.assert_not_called()
    assert json.loads(raw)["candidates"][0]["coords"] == [500, 600]
    client.aio.caches.create.assert_awaited_once()
    client.aio.caches.delete.assert_awaited_once_with(name="cachedContents/abc")
    config = client.aio.models.generate_content.call_args.kwargs["config"]
    assert config.cached_content == "cachedContents/abc"


@pytest.mark.asyncio
async def test_native_pro_run_skips_cache_by_default(screenshot):
    ctx = _context()
    client = _native_client([_submit_response()])
    ctx._genai_client = client
    storage = MagicMock()
    storage.get_image.return_value = None

    with (
        patch(f"{FACADE}.StorageManager", return_value=storage),
        patch.object(settings, "EXPLORER_CACHING", None),
    ):
        await Explorer(ctx).run("target", "", screenshot, _state(screenshot), version="pro")

    client.aio.caches.create.assert_not_awaited()
    client.aio.caches.delete.assert_not_awaited()
    config = client.aio.models.generate_content.call_args.kwargs["config"]
    assert {t.name for t in config.tools[0].function_declarations} == {
        "ask_perception_tool",
        "submit_answer",
    }


@pytest.mark.asyncio
async def test_native_file_api_uploads_and_deletes_asynchronously(screenshot, monkeypatch):
    monkeypatch.setenv("ARTEMIS_USE_FILE_API", "true")
    ctx = _context()
    client = _native_client([_submit_response()])
    ctx._genai_client = client
    storage = MagicMock()
    storage.get_image.return_value = None

    with patch(f"{FACADE}.StorageManager", return_value=storage):
        await Explorer(ctx).run("target", "", screenshot, _state(screenshot), version="pro")

    client.aio.files.upload.assert_awaited_once_with(file=screenshot)
    client.aio.files.delete.assert_awaited_once_with(name="files/1")


@pytest.mark.asyncio
async def test_run_closes_http_client_on_both_engines(screenshot):
    storage = MagicMock()
    storage.get_image.return_value = None

    # Native engine
    ctx = _context()
    client = _native_client([_submit_response()])
    ctx._genai_client = client
    native = Explorer(ctx)
    native.http_client = SimpleNamespace(aclose=AsyncMock())
    with patch(f"{FACADE}.StorageManager", return_value=storage):
        await native.run("t", "", screenshot, _state(screenshot), version="pro")
    native_close = native.http_client  # already reset
    assert native_close is None

    # Universal engine (model call fails -> finally still closes)
    llm = MagicMock()
    llm.bind_tools.return_value.ainvoke = AsyncMock(side_effect=RuntimeError("boom"))
    universal = Explorer(_context(model="claude-sonnet-4"))
    http_client = SimpleNamespace(aclose=AsyncMock())
    universal.http_client = http_client
    with (
        patch(f"{FACADE}.StorageManager", return_value=storage),
        patch(f"{FACADE}.get_llm", return_value=llm),
        pytest.raises(RuntimeError),
    ):
        await universal.run("t", "", screenshot, _state(screenshot), version="pro")
    http_client.aclose.assert_awaited_once()
    assert universal.http_client is None


def test_missing_tool_calls_without_candidates_does_not_crash():
    explorer = Explorer(_context())
    response = MagicMock()
    response.candidates = None
    contents = []
    turn_record = {"tool_calls": []}
    explorer._handle_missing_tool_calls(contents, response, turn_record, ["hi"], 3, 1)
    assert contents[0].role == "model"
    assert contents[0].parts[0].text == "hi"
    assert "2 more" in contents[1].parts[0].text
    assert turn_record["tool_calls"][0]["name"] == "hallucinated_plain_text"


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
