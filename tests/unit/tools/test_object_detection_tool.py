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

"""Unit tests for ObjectDetectionTool and OperatorObjectDetectionTool."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

from artemis.context import ArtemisContext
from artemis.graph.state import State
from artemis.tools.base import ArtemisTool, ToolRegistry
from artemis.tools.object_detection_tool import (
    ObjectDetection,
    ObjectDetectionArgs,
    ObjectDetectionTool,
    ObjectDetectorTool,
    OperatorObjectDetection,
    OperatorObjectDetectionArgs,
    OperatorObjectDetectionTool,
    OperatorObjectDetectorTool,
    get_object_detector_tool,
    get_operator_object_detector_tool,
    object_detection,
    object_detection_wrapper,
    operator_object_detection,
    operator_object_detection_wrapper,
)
import pytest


def test_object_detection_tool_subclass_and_registry():
    """Verify ObjectDetectionTool and OperatorObjectDetectionTool subclass ArtemisTool and register."""
    assert issubclass(ObjectDetectionTool, ArtemisTool)
    assert issubclass(ObjectDetection, ArtemisTool)
    assert issubclass(ObjectDetectorTool, ArtemisTool)
    assert issubclass(OperatorObjectDetectionTool, ArtemisTool)
    assert issubclass(OperatorObjectDetectionTool, ObjectDetectionTool)
    assert issubclass(OperatorObjectDetection, ArtemisTool)
    assert issubclass(OperatorObjectDetectorTool, ArtemisTool)

    assert isinstance(object_detection, ArtemisTool)
    assert isinstance(object_detection, ObjectDetectionTool)
    assert isinstance(operator_object_detection, ArtemisTool)
    assert isinstance(operator_object_detection, OperatorObjectDetectionTool)

    # Properties
    assert object_detection.name == "object_detection"
    assert object_detection.category == "perception"
    assert object_detection.args_schema == ObjectDetectionArgs

    assert operator_object_detection.name == "object_detection"
    assert operator_object_detection.category == "perception"
    assert operator_object_detection.args_schema == OperatorObjectDetectionArgs

    # Registry lookup
    reg_tool = ToolRegistry.get("object_detection")
    assert reg_tool is not None
    assert isinstance(reg_tool, ObjectDetectionTool)

    # GenAI FunctionDeclaration export
    declaration = object_detection.to_genai_declaration()
    assert declaration.name == "object_detection"
    assert "image_path" in declaration.parameters.properties
    assert "queries" in declaration.parameters.properties

    op_declaration = operator_object_detection.to_genai_declaration()
    assert op_declaration.name == "object_detection"
    assert "queries" in op_declaration.parameters.properties

    # Wrappers
    assert object_detection_wrapper.tool_fn_getter == get_object_detector_tool
    assert operator_object_detection_wrapper.tool_fn_getter == get_operator_object_detector_tool


@pytest.mark.asyncio
async def test_object_detection_direct_execution_success():
    """Verify direct async call to object_detection with image_path."""
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = None

    expected_result = {
        "detected": [{"label": "button", "point": [100, 200]}],
        "failed": [],
    }

    with patch(
        "artemis.tools.object_detection_tool._run_object_detection",
        AsyncMock(return_value=expected_result),
    ) as mock_run:
        result = await object_detection(
            ctx=mock_ctx,
            image_path="/path/to/screenshot.jpg",
            queries=["button"],
        )

        mock_run.assert_called_once()
        assert json.loads(result) == expected_result


@pytest.mark.asyncio
async def test_operator_object_detection_direct_execution_with_state():
    """Verify operator_object_detection reads screenshot from state."""
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = None

    mock_state = MagicMock(spec=State)
    mock_state.latest_screenshot = "/path/to/state_screen.jpg"
    mock_state.asanitize_update = None  # Non-langgraph direct call

    expected_result = {
        "detected": [{"label": "search bar", "point": [500, 100]}],
        "failed": [],
    }

    with patch(
        "artemis.tools.object_detection_tool._run_object_detection",
        AsyncMock(return_value=expected_result),
    ) as mock_run:
        result = await operator_object_detection(
            ctx=mock_ctx,
            state=mock_state,
            queries=["search bar"],
        )

        mock_run.assert_called_once()
        # Verify it used the latest_screenshot from state
        call_args = mock_run.call_args[0]
        assert call_args[1] == "/path/to/state_screen.jpg"
        assert json.loads(result) == expected_result


@pytest.mark.asyncio
async def test_object_detection_no_image_error():
    """Verify error when no image path is given and no state screenshot is available."""
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = None

    result = await object_detection(
        ctx=mock_ctx,
        image_path=None,
        queries=["icon"],
    )
    assert "Object Detection failed" in result
    assert "No image path provided" in result


@pytest.mark.asyncio
async def test_object_detection_exception_handling():
    """Verify graceful handling when _run_object_detection raises an exception."""
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = None

    with patch(
        "artemis.tools.object_detection_tool._run_object_detection",
        AsyncMock(side_effect=RuntimeError("Model inference timeout")),
    ):
        result = await object_detection(
            ctx=mock_ctx,
            image_path="/path/to/screen.jpg",
            queries=["icon"],
        )
        assert "Object Detection failed: Model inference timeout" in result


def test_get_langchain_tools():
    """Verify LangChain BaseTool exports."""
    mock_ctx = MagicMock(spec=ArtemisContext)
    tool = get_object_detector_tool(mock_ctx)
    op_tool = get_operator_object_detector_tool(mock_ctx)

    assert tool.name == "object_detection"
    assert tool.args_schema == ObjectDetectionArgs

    assert op_tool.name == "object_detection"
    assert op_tool.args_schema == OperatorObjectDetectionArgs
