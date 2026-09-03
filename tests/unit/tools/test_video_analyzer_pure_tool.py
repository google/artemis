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
from artemis.tools.base import ArtemisTool
from artemis.tools.video_tool import (
    VideoAnalyzerArgs,
    VideoAnalyzerPure,
    VideoAnalyzerPureTool,
    get_video_analyzer_tool_pure,
    video_analyzer_pure,
)
import pytest


@pytest.fixture
def mock_ctx():
    ctx = MagicMock(spec=ArtemisContext)
    return ctx


def test_video_analyzer_pure_tool_subclass():
    """Verify VideoAnalyzerPureTool is a subclass of ArtemisTool."""
    assert issubclass(VideoAnalyzerPureTool, ArtemisTool)
    assert issubclass(VideoAnalyzerPure, ArtemisTool)
    assert isinstance(video_analyzer_pure, ArtemisTool)
    assert isinstance(video_analyzer_pure, VideoAnalyzerPureTool)

    assert video_analyzer_pure.name == "video_analyzer_pure"
    assert video_analyzer_pure.category == "perception"
    assert video_analyzer_pure.args_schema == VideoAnalyzerArgs

    # GenAI FunctionDeclaration export
    declaration = video_analyzer_pure.to_genai_declaration()
    assert declaration.name == "video_analyzer_pure"
    assert "time_description" in declaration.parameters.properties
    assert "purpose" in declaration.parameters.properties


@pytest.mark.asyncio
async def test_video_analyzer_pure_direct_execution_success(mock_ctx):
    """Verify direct execution of VideoAnalyzerPureTool.execute returns outcome directly."""
    with patch(
        "artemis.tools.video_tool.VideoAnalyzer.run",
        new_callable=AsyncMock,
        return_value=("Pure video analysis success", "success"),
    ):
        result = await video_analyzer_pure.execute(
            ctx=mock_ctx,
            time_description="from 0s to 5s",
            purpose="Verify video playback",
        )
        assert result == "Pure video analysis success"


@pytest.mark.asyncio
async def test_video_analyzer_pure_direct_execution_failed(mock_ctx):
    """Verify direct execution handles failed status properly."""
    with patch(
        "artemis.tools.video_tool.VideoAnalyzer.run",
        new_callable=AsyncMock,
        return_value=("Pure could not find element", "failed"),
    ):
        result = await video_analyzer_pure.execute(
            ctx=mock_ctx,
            time_description="from 10s to 15s",
            purpose="Check button click",
        )
        assert result == "Video analysis failed: Pure could not find element"


@pytest.mark.asyncio
async def test_video_analyzer_pure_callable_execution(mock_ctx):
    """Verify invoking video_analyzer_pure directly as a callable."""
    with patch(
        "artemis.tools.video_tool.VideoAnalyzer.run",
        new_callable=AsyncMock,
        return_value=("Pure callable output", "success"),
    ):
        result = await video_analyzer_pure(
            ctx=mock_ctx,
            time_description="from 1s to 2s",
            purpose="Callable check",
        )
        assert result == "Pure callable output"


@pytest.mark.asyncio
async def test_video_analyzer_pure_exception_handling(mock_ctx):
    """Verify VideoAnalyzerPureTool gracefully handles exceptions."""
    with patch(
        "artemis.tools.video_tool.VideoAnalyzer.run",
        new_callable=AsyncMock,
        side_effect=RuntimeError("Video processing error"),
    ):
        result = await video_analyzer_pure.execute(
            ctx=mock_ctx,
            time_description="from 0s to 5s",
            purpose="Test error",
        )
        assert "Error running video analyzer: Video processing error" in result


@pytest.mark.asyncio
async def test_get_video_analyzer_tool_pure_ainvoke(mock_ctx):
    """Verify get_video_analyzer_tool_pure exports a LangChain tool named 'video_analyzer'."""
    pure_tool = get_video_analyzer_tool_pure(mock_ctx)
    assert pure_tool.name == "video_analyzer"

    with patch(
        "artemis.tools.video_tool.VideoAnalyzer.run",
        new_callable=AsyncMock,
        return_value=("Exported pure tool output", "success"),
    ):
        result = await pure_tool.ainvoke(
            {
                "time_description": "from 2s to 4s",
                "purpose": "Pure invoke check",
            }
        )
        assert result == "Exported pure tool output"
