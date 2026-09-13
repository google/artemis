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
from unittest.mock import AsyncMock, MagicMock, patch

from artemis.context import ArtemisContext
from artemis.drivers.base import BaseDeviceDriver
from artemis.graph.state import State
from artemis.tools.base import ArtemisTool
from artemis.tools.mobile.ocr import (
    OCRRecognition,
    OcrArgs,
    OcrRecognition,
    OcrRecognitionTool,
    OcrTool,
    get_ocr_tool,
    ocr_recognition,
    ocr_recognition_wrapper,
)
from langchain_core.messages import ToolMessage
import pytest


@pytest.fixture
def mock_ctx():
    ctx = MagicMock(spec=ArtemisContext)
    ctx.device = MagicMock()
    ctx.device.device_width = 1080
    ctx.device.device_height = 2400
    return ctx


@pytest.fixture
def mock_driver():
    driver = MagicMock(spec=BaseDeviceDriver)
    driver.screen_size = (1080, 2400)
    screen_data = MagicMock()
    screen_data.screenshot_base64 = base64.b64encode(b"dummy_image_data").decode("utf-8")
    screen_data.screenshot_bytes = b"dummy_image_data"
    driver.get_screen_data = AsyncMock(return_value=screen_data)
    return driver


@pytest.fixture
def mock_state(tmp_path):
    screenshot_file = tmp_path / "test_screenshot.png"
    screenshot_file.write_bytes(b"dummy_image_content")

    state = MagicMock(spec=State)
    state.latest_screenshot = str(screenshot_file)
    return state


def test_ocr_tool_subclass():
    """Verify OcrRecognitionTool is a subclass of ArtemisTool."""
    assert issubclass(OcrRecognitionTool, ArtemisTool)
    assert issubclass(OcrRecognition, ArtemisTool)
    assert issubclass(OCRRecognition, ArtemisTool)
    assert issubclass(OcrTool, ArtemisTool)
    assert isinstance(ocr_recognition, ArtemisTool)
    assert isinstance(ocr_recognition, OcrRecognitionTool)

    assert ocr_recognition.name == "ocr_recognition"
    assert ocr_recognition.category == "explorer"
    assert ocr_recognition.args_schema == OcrArgs

    # GenAI FunctionDeclaration export
    declaration = ocr_recognition.to_genai_declaration()
    assert declaration.name == "ocr_recognition"

    # Wrapper check
    assert ocr_recognition_wrapper is not None
    assert ocr_recognition_wrapper.tool_fn_getter == get_ocr_tool


class SimpleState:
    def __init__(self, screenshot_path: str):
        self.latest_screenshot = screenshot_path


@pytest.mark.asyncio
async def test_ocr_direct_execution_with_state(mock_ctx, tmp_path):
    """Verify direct execution with state and screenshot file."""
    screenshot_file = tmp_path / "test_screenshot.png"
    screenshot_file.write_bytes(b"dummy_image_content")
    simple_state = SimpleState(str(screenshot_file))

    mock_results = [
        {
            "text": "Submit",
            "position": [
                {"x": 100, "y": 200},
                {"x": 200, "y": 200},
                {"x": 200, "y": 300},
                {"x": 100, "y": 300},
            ],
        }
    ]
    with patch(
        "artemis.tools.mobile.ocr.perform_ocr",
        new_callable=AsyncMock,
        return_value=mock_results,
    ):
        result = await ocr_recognition.execute(ctx=mock_ctx, state=simple_state)
        assert isinstance(result, ToolMessage)
        assert "OCR Recognition successful." in result.content
        assert "Submit" in result.content


@pytest.mark.asyncio
async def test_ocr_direct_execution_with_driver(mock_ctx, mock_driver):
    """Verify direct execution with driver."""
    mock_results = [
        {
            "text": "Settings",
            "position": [
                {"x": 500, "y": 500},
                {"x": 600, "y": 600},
            ],
        }
    ]
    with patch(
        "artemis.tools.mobile.ocr.perform_ocr",
        new_callable=AsyncMock,
        return_value=mock_results,
    ):
        result = await ocr_recognition.execute(ctx=mock_ctx, driver=mock_driver)
        assert "OCR Recognition successful." in result
        assert "Settings" in result


@pytest.mark.asyncio
async def test_ocr_no_text_detected(mock_ctx, tmp_path):
    """Verify outcome when no text is detected."""
    screenshot_file = tmp_path / "test_empty_screenshot.png"
    screenshot_file.write_bytes(b"dummy_empty_content")
    simple_state = SimpleState(str(screenshot_file))

    with patch(
        "artemis.tools.mobile.ocr.perform_ocr",
        new_callable=AsyncMock,
        return_value=[],
    ):
        result = await ocr_recognition.execute(ctx=mock_ctx, state=simple_state)
        assert isinstance(result, ToolMessage)
        assert "No text detected on the screen." in result.content


@pytest.mark.asyncio
async def test_ocr_with_state_command(mock_ctx, mock_state):
    """Verify OcrRecognitionTool returns ToolMessage when state is provided."""
    mock_results = [{"text": "Hello", "position": [{"x": 100, "y": 100}]}]
    with patch(
        "artemis.tools.mobile.ocr.perform_ocr",
        new_callable=AsyncMock,
        return_value=mock_results,
    ):
        cmd = await ocr_recognition.execute(
            ctx=mock_ctx,
            tool_call_id="call_ocr_1",
            state=mock_state,
        )
        assert isinstance(cmd, ToolMessage)
        assert cmd.tool_call_id == "call_ocr_1"
        assert cmd.status == "success"
        assert "Hello" in cmd.content


@pytest.mark.asyncio
async def test_ocr_failure_missing_screenshot(mock_ctx):
    """Verify error handling when screenshot path is missing."""
    empty_state = MagicMock(spec=State)
    empty_state.latest_screenshot = None

    cmd = await ocr_recognition.execute(
        ctx=mock_ctx,
        tool_call_id="call_ocr_err",
        state=empty_state,
    )
    assert isinstance(cmd, ToolMessage)
    assert cmd.status == "error"
    assert "OCR Recognition failed" in cmd.content


@pytest.mark.asyncio
async def test_ocr_callable_execution(mock_ctx, mock_driver):
    """Verify invoking ocr_recognition directly as a callable."""
    with patch(
        "artemis.tools.mobile.ocr.perform_ocr",
        new_callable=AsyncMock,
        return_value=[{"text": "Callable", "position": []}],
    ):
        result = await ocr_recognition(ctx=mock_ctx, driver=mock_driver)
        assert "OCR Recognition successful." in result
        assert "Callable" in result


@pytest.mark.asyncio
async def test_get_ocr_tool_langchain_ainvoke(mock_ctx, mock_driver):
    """Verify get_ocr_tool exports a LangChain tool that works with ainvoke."""
    mock_ctx._active_driver = mock_driver
    ocr_tool = get_ocr_tool(mock_ctx)
    assert ocr_tool.name == "ocr_recognition"

    with patch(
        "artemis.tools.mobile.ocr.perform_ocr",
        new_callable=AsyncMock,
        return_value=[{"text": "LangChain", "position": []}],
    ):
        result = await ocr_tool.ainvoke({})
        assert "OCR Recognition successful." in result
        assert "LangChain" in result
