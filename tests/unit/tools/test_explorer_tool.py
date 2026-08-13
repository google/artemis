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

import json
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

from artemis.context import ArtemisContext
from artemis.graph.state import State
from artemis.tools.base import ArtemisTool, ToolRegistry
from artemis.tools.explorer_tool import (
    AskExplorer,
    AskExplorerArgs,
    AskExplorerTool,
    AskExplorerToolAlias,
    ask_explorer,
    ask_explorer_wrapper,
    get_ask_explorer_tool,
)
import pytest


def test_ask_explorer_tool_subclass_and_registry():
    """Verify AskExplorerTool is a subclass of ArtemisTool and properly registered."""
    assert issubclass(AskExplorerTool, ArtemisTool)
    assert issubclass(AskExplorer, ArtemisTool)
    assert issubclass(AskExplorerToolAlias, ArtemisTool)
    assert isinstance(ask_explorer, ArtemisTool)
    assert isinstance(ask_explorer, AskExplorerTool)

    assert ask_explorer.name == "ask_explorer"
    assert ask_explorer.category == "explorer"
    assert ask_explorer.args_schema == AskExplorerArgs

    # Registry lookup
    reg_tool = ToolRegistry.get("ask_explorer")
    assert reg_tool is not None
    assert isinstance(reg_tool, AskExplorerTool)

    # GenAI FunctionDeclaration export
    declaration = ask_explorer.to_genai_declaration()
    assert declaration.name == "ask_explorer"
    assert "query" in declaration.parameters.properties

    # Wrapper check
    assert ask_explorer_wrapper is not None
    assert ask_explorer_wrapper.tool_fn_getter == get_ask_explorer_tool


@pytest.mark.asyncio
async def test_ask_explorer_multiple_candidates():
    # Mock Context and State
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = None
    mock_ctx.agent_config = None
    mock_ctx.llm_config = None
    mock_ctx.execution_setup = None
    mock_ctx.device = MagicMock()
    mock_ctx.device.device_width = 1080
    mock_ctx.device.device_height = 2400

    mock_state = MagicMock(spec=State)
    mock_state.latest_screenshot = "/tmp/test.jpg"
    mock_state.indexed_points = [[100, 200]]  # Pre-existing point
    mock_state.indexed_elements = [
        {
            "index": 1,
            "center": [100, 200],
            "text": "Pre-existing",
            "bounds": None,
            "class": "Test",
            "resource_id": None,
            "is_ocr": False,
        }
    ]

    # Mock Explorer Agent Outcome with multiple candidates and fallback message
    explorer_outcome = {
        "candidates": [
            {
                "label": "S1",
                "coords": [500, 500],
                "description": "First Button",
            },
            {
                "label": "S2",
                "coords": [250, 750],
                "description": "Second Button",
            },
        ],
        "fallback_message": "Please choose carefully.",
    }
    explorer_output_str = json.dumps(explorer_outcome)

    # We need to patch the Explorer class inside the tool
    mock_explorer_instance = MagicMock()
    mock_explorer_instance.run = AsyncMock(return_value=explorer_output_str)

    with patch(
        "artemis.tools.explorer_tool.Explorer",
        return_value=mock_explorer_instance,
    ):
        # Retrieve the tool
        ask_explorer_tool = get_ask_explorer_tool(mock_ctx, version="pro")

        result = await ask_explorer_tool.ainvoke(
            {
                "query": "Find buttons",
                "context_feedback": "Attempt 1",
                "state": mock_state,
            }
        )

        assert mock_state.indexed_points == [
            [100, 200],
            [540, 1200],
            [270, 1800],
        ]

        assert len(mock_state.indexed_elements) == 3
        assert mock_state.indexed_elements[1]["index"] == 2
        assert mock_state.indexed_elements[1]["center"] == [540, 1200]
        assert mock_state.indexed_elements[1]["text"] == "First Button"
        assert mock_state.indexed_elements[2]["index"] == 3
        assert mock_state.indexed_elements[2]["center"] == [270, 1800]
        assert mock_state.indexed_elements[2]["text"] == "Second Button"

        assert isinstance(result, str)
        assert "Explorer successfully located the following candidate(s):" in result
        assert "- [2] 'First Button' at coordinate [500, 500]" in result
        assert "- [3] 'Second Button' at coordinate [250, 750]" in result
        assert (
            "You can click/act on them directly by calling perform_action with"
            " their respective index." in result
        )
        assert "Additional Notes from Explorer: Please choose carefully." in result


@pytest.mark.asyncio
async def test_ask_explorer_only_fallback_message():
    # Mock Context and State
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = None
    mock_ctx.agent_config = None
    mock_ctx.llm_config = None
    mock_ctx.execution_setup = None
    mock_state = MagicMock(spec=State)
    mock_state.latest_screenshot = "/tmp/test.jpg"
    mock_state.indexed_points = []
    mock_state.indexed_elements = []

    explorer_outcome = {
        "candidates": [],
        "fallback_message": ("Element not found because it is covered by a keyboard."),
    }
    explorer_output_str = json.dumps(explorer_outcome)

    mock_explorer_instance = MagicMock()
    mock_explorer_instance.run = AsyncMock(return_value=explorer_output_str)

    with patch(
        "artemis.tools.explorer_tool.Explorer",
        return_value=mock_explorer_instance,
    ):
        ask_explorer_tool = get_ask_explorer_tool(mock_ctx, version="pro")

        result = await ask_explorer_tool.ainvoke(
            {
                "query": "Find hidden button",
                "context_feedback": "",
                "state": mock_state,
            }
        )

        assert mock_state.indexed_points == []
        assert mock_state.indexed_elements == []

        assert (
            "Explorer could not locate the element. Message: Element not found"
            " because it is covered by a keyboard." in result
        )


@pytest.mark.asyncio
async def test_ask_explorer_multimodal_success():
    # Mock Context and State
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = None
    mock_ctx.agent_config = None
    mock_ctx.llm_config = None
    mock_ctx.execution_setup = None
    mock_ctx.device = MagicMock()
    mock_ctx.device.device_width = 1080
    mock_ctx.device.device_height = 2400

    mock_state = MagicMock(spec=State)
    mock_state.latest_screenshot = "/tmp/test.jpg"
    mock_state.indexed_points = [[100, 200]]
    mock_state.indexed_elements = [
        {
            "index": 1,
            "center": [100, 200],
            "text": "Pre-existing",
            "bounds": None,
            "class": "Test",
            "resource_id": None,
            "is_ocr": False,
        }
    ]

    explorer_outcome = {
        "candidates": [
            {
                "label": "S1",
                "coords": [500, 500],
                "description": "First Button",
            },
        ],
        "fallback_message": "",
    }
    explorer_output_str = json.dumps(explorer_outcome)

    mock_explorer_instance = MagicMock()
    mock_explorer_instance.run = AsyncMock(return_value=explorer_output_str)

    m_open = mock_open(read_data=b"fake_annotated_image_bytes")

    with (
        patch(
            "artemis.tools.explorer_tool.Explorer",
            return_value=mock_explorer_instance,
        ),
        patch("artemis.tools.explorer_tool.draw_dots") as mock_draw_dots,
        patch("builtins.open", m_open),
        patch("pathlib.Path.mkdir"),
    ):
        ask_explorer_tool = get_ask_explorer_tool(mock_ctx, version="pro")

        result = await ask_explorer_tool.ainvoke(
            {
                "query": "Find buttons",
                "context_feedback": "",
                "state": mock_state,
            }
        )

        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["type"] == "text"
        assert "Explorer successfully located" in result[0]["text"]
        assert result[1]["type"] == "image_url"
        assert result[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")

        mock_draw_dots.assert_called_once()
        points_arg = mock_draw_dots.call_args[0][1]
        assert points_arg == [[100, 200], [540, 1200]]
        labels_arg = mock_draw_dots.call_args[0][2]
        assert labels_arg == ["1", "2"]


@pytest.mark.asyncio
async def test_ask_explorer_direct_execution():
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = None
    mock_ctx.agent_config = None
    mock_ctx.llm_config = None
    mock_ctx.execution_setup = None
    mock_ctx.device = None

    mock_state = MagicMock(spec=State)
    mock_state.latest_screenshot = "/tmp/test.jpg"
    mock_state.indexed_points = []
    mock_state.indexed_elements = []

    mock_explorer_instance = MagicMock()
    mock_explorer_instance.run = AsyncMock(
        return_value=json.dumps(
            {
                "candidates": [],
                "fallback_message": "Direct execution not found.",
            }
        )
    )

    with patch(
        "artemis.tools.explorer_tool.Explorer",
        return_value=mock_explorer_instance,
    ):
        result = await ask_explorer.execute(
            ctx=mock_ctx, state=mock_state, query="Find icon", version="pro"
        )
        assert "Direct execution not found." in result


@pytest.mark.asyncio
async def test_ask_explorer_callable_execution():
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = None
    mock_ctx.agent_config = None
    mock_ctx.llm_config = None
    mock_ctx.execution_setup = None
    mock_ctx.device = None

    mock_state = MagicMock(spec=State)
    mock_state.latest_screenshot = "/tmp/test.jpg"
    mock_state.indexed_points = []
    mock_state.indexed_elements = []

    mock_explorer_instance = MagicMock()
    mock_explorer_instance.run = AsyncMock(
        return_value=json.dumps(
            {
                "candidates": [],
                "fallback_message": "Callable test message.",
            }
        )
    )

    with patch(
        "artemis.tools.explorer_tool.Explorer",
        return_value=mock_explorer_instance,
    ):
        result = await ask_explorer(
            ctx=mock_ctx, state=mock_state, query="Search bar", version="pro"
        )
        assert "Callable test message." in result


@pytest.mark.asyncio
async def test_ask_explorer_flash_mode():
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = None
    mock_ctx.agent_config = None
    mock_ctx.llm_config = None
    mock_ctx.execution_setup = None
    mock_ctx.device = MagicMock()
    mock_ctx.device.device_width = 1080
    mock_ctx.device.device_height = 2400

    mock_state = MagicMock(spec=State)
    mock_state.latest_screenshot = "/tmp/test.jpg"
    mock_state.indexed_points = []
    mock_state.indexed_elements = []

    with (
        patch(
            "artemis.tools.explorer_tool._run_object_detection",
            new_callable=AsyncMock,
            return_value={
                "detected": [
                    {"label": "Icon", "point": [500, 500]},
                ],
                "failed": [],
            },
        ),
        patch(
            "builtins.open",
            mock_open(read_data=json.dumps({"templates": [], "instructions": ""})),
        ),
    ):
        result = await ask_explorer.execute(
            ctx=mock_ctx, state=mock_state, query="Icon", version="flash"
        )
        assert "Explorer successfully located" in result
        assert "Icon" in result
