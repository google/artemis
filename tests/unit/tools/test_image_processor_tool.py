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

from unittest.mock import AsyncMock, MagicMock, patch

from artemis.context import ArtemisContext
from artemis.graph.state import State
from artemis.tools.image_processor_tool import get_ask_image_processor_tool
import pytest


@pytest.mark.asyncio
async def test_ask_vision_coder_success():
    # Mock Context and State
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = None

    mock_state = MagicMock(spec=State)
    mock_state.latest_screenshot = "/tmp/test_screen.jpg"

    # Mock VisionCoder Agent Outcome
    vision_coder_outcome = {
        "new_image_path": "/tmp/coder_output.jpg",
        "summary": "Found red button at pixel coords (100, 200)",
    }

    # We need to patch the ImageProcessor class inside the tool
    mock_coder_instance = MagicMock()
    mock_coder_instance.run = AsyncMock(return_value=vision_coder_outcome)

    with patch(
        "artemis.agents.image_processor.image_processor.ImageProcessor",
        return_value=mock_coder_instance,
    ):
        # Retrieve the tool
        ask_vision_coder_tool = get_ask_image_processor_tool(mock_ctx)

        # Invoke the tool
        result = await ask_vision_coder_tool.ainvoke(
            {
                "instruction": "Find the red button",
                "target_image_id": "img_0",
                "state": mock_state,
            }
        )

        # Verify the agent was called with correct arguments
        mock_coder_instance.run.assert_called_once_with(
            "Find the red button", "/tmp/test_screen.jpg"
        )

        # Verify the output matches
        assert result == vision_coder_outcome


def test_ask_image_processor_tool_subclass_and_registry():
    """Verify AskImageProcessorTool is an ArtemisTool subclass and registered in ToolRegistry."""
    from artemis.tools.base import ArtemisTool, ToolRegistry
    from artemis.tools.image_processor_tool import (
        AskImageProcessor,
        AskImageProcessorTool,
        AskVisionCoderArgs,
        AskVisionCoderTool,
        ImageProcessorTool,
        ask_image_processor,
    )

    assert issubclass(AskImageProcessorTool, ArtemisTool)
    assert issubclass(AskImageProcessor, ArtemisTool)
    assert issubclass(AskVisionCoderTool, ArtemisTool)
    assert issubclass(ImageProcessorTool, ArtemisTool)
    assert isinstance(ask_image_processor, ArtemisTool)
    assert isinstance(ask_image_processor, AskImageProcessorTool)

    assert ask_image_processor.name == "ask_image_processor"
    assert ask_image_processor.category == "custom"
    assert ask_image_processor.args_schema == AskVisionCoderArgs

    # Registry lookup
    reg_tool = ToolRegistry.get("ask_image_processor")
    assert reg_tool is not None
    assert isinstance(reg_tool, AskImageProcessorTool)

    # GenAI FunctionDeclaration export
    declaration = ask_image_processor.to_genai_declaration()
    assert declaration.name == "ask_image_processor"
    assert "instruction" in declaration.parameters.properties
    assert "target_image_id" in declaration.parameters.properties
