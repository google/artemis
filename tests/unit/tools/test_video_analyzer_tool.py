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

from artemis.constants import VALIDATOR_MESSAGES_KEY
from artemis.context import ArtemisContext
from artemis.graph.state import State
from artemis.tools.base import ArtemisTool, ToolRegistry
from artemis.tools.video_tool import (
    DIAGNOSER_VIDEO_ANALYZER_DOCSTRING,
    OPERATOR_VIDEO_ANALYZER_DOCSTRING,
    VideoAnalyzerArgs,
    VideoAnalyzerTool,
    get_video_analyzer_tool,
    get_video_analyzer_tool_pure,
    video_analyzer,
)
from langgraph.types import Command
import pytest


@pytest.fixture
def mock_ctx():
    ctx = MagicMock(spec=ArtemisContext)
    return ctx


@pytest.fixture
def mock_state(mock_ctx):
    state = MagicMock(spec=State)

    async def _mock_asanitize_update(ctx, update, agent):
        return update

    state.asanitize_update = AsyncMock(side_effect=_mock_asanitize_update)
    return state


def test_video_analyzer_tool_subclass_and_registry():
    """Verify VideoAnalyzerTool is a subclass of ArtemisTool and properly registered."""
    assert issubclass(VideoAnalyzerTool, ArtemisTool)
    assert isinstance(video_analyzer, ArtemisTool)
    assert isinstance(video_analyzer, VideoAnalyzerTool)

    assert video_analyzer.name == "video_analyzer"
    assert video_analyzer.category == "perception"
    assert video_analyzer.args_schema == VideoAnalyzerArgs

    # Registry lookup
    reg_tool = ToolRegistry.get("video_analyzer")
    assert reg_tool is not None
    assert isinstance(reg_tool, VideoAnalyzerTool)

    # GenAI FunctionDeclaration export
    declaration = video_analyzer.to_genai_declaration()
    assert declaration.name == "video_analyzer"
    assert "time_description" in declaration.parameters.properties
    assert "purpose" in declaration.parameters.properties


@pytest.mark.asyncio
async def test_video_analyzer_direct_execution_success(mock_ctx):
    """Verify direct execution of VideoAnalyzerTool.execute returns outcome directly."""
    with patch(
        "artemis.tools.video_tool.VideoAnalyzer.run",
        new_callable=AsyncMock,
        return_value=("Video analysis success details", "success"),
    ):
        result = await video_analyzer.execute(
            ctx=mock_ctx,
            time_description="from 0s to 5s",
            purpose="Verify video playback",
        )
        assert result == "Video analysis success details"


@pytest.mark.asyncio
async def test_video_analyzer_direct_execution_failed(mock_ctx):
    """Verify direct execution handles failed status properly."""
    with patch(
        "artemis.tools.video_tool.VideoAnalyzer.run",
        new_callable=AsyncMock,
        return_value=("Could not find target element", "failed"),
    ):
        result = await video_analyzer.execute(
            ctx=mock_ctx,
            time_description="from 10s to 15s",
            purpose="Check button click",
        )
        assert result == "Video analysis failed: Could not find target element"


@pytest.mark.asyncio
async def test_video_analyzer_with_state_command(mock_ctx, mock_state):
    """Verify execute returns a Command with ToolMessage when state is provided."""
    with patch(
        "artemis.tools.video_tool.VideoAnalyzer.run",
        new_callable=AsyncMock,
        return_value=("Action verified in video", "success"),
    ):
        cmd = await video_analyzer.execute(
            ctx=mock_ctx,
            time_description="from 5s to 10s",
            purpose="Verify action",
            tool_call_id="call_video_123",
            state=mock_state,
        )
        assert isinstance(cmd, Command)
        assert VALIDATOR_MESSAGES_KEY in cmd.update
        messages = cmd.update[VALIDATOR_MESSAGES_KEY]
        assert len(messages) == 1
        assert messages[0].tool_call_id == "call_video_123"
        assert messages[0].content == "Action verified in video"
        assert messages[0].status == "success"


@pytest.mark.asyncio
async def test_video_analyzer_exception_handling(mock_ctx, mock_state):
    """Verify execute gracefully handles exceptions."""
    with patch(
        "artemis.tools.video_tool.VideoAnalyzer.run",
        new_callable=AsyncMock,
        side_effect=RuntimeError("Video processing error"),
    ):
        # Without state
        res = await video_analyzer.execute(
            ctx=mock_ctx,
            time_description="from 0s to 5s",
            purpose="Test error",
        )
        assert "Error running video analyzer: Video processing error" in res

        # With state
        cmd = await video_analyzer.execute(
            ctx=mock_ctx,
            time_description="from 0s to 5s",
            purpose="Test error",
            tool_call_id="call_err",
            state=mock_state,
        )
        assert isinstance(cmd, Command)
        assert VALIDATOR_MESSAGES_KEY in cmd.update
        messages = cmd.update[VALIDATOR_MESSAGES_KEY]
        assert messages[0].status == "error"
        assert "Error running video analyzer" in messages[0].content


@pytest.mark.asyncio
async def test_video_analyzer_callable_execution(mock_ctx):
    """Verify invoking video_analyzer directly as a callable."""
    with patch(
        "artemis.tools.video_tool.VideoAnalyzer.run",
        new_callable=AsyncMock,
        return_value=("Callable output", "success"),
    ):
        result = await video_analyzer(
            ctx=mock_ctx,
            time_description="from 1s to 2s",
            purpose="Callable check",
        )
        assert result == "Callable output"


def test_get_video_analyzer_tool_roles(mock_ctx):
    """Verify get_video_analyzer_tool returns tool with role-specific description."""
    op_tool = get_video_analyzer_tool(mock_ctx, role="operator")
    assert op_tool.name == "video_analyzer"
    assert op_tool.description.strip() == OPERATOR_VIDEO_ANALYZER_DOCSTRING.strip()

    diag_tool = get_video_analyzer_tool(mock_ctx, role="diagnoser")
    assert diag_tool.name == "video_analyzer"
    assert diag_tool.description.strip() == DIAGNOSER_VIDEO_ANALYZER_DOCSTRING.strip()


@pytest.mark.asyncio
async def test_get_video_analyzer_tool_pure_ainvoke(mock_ctx):
    """Verify get_video_analyzer_tool_pure exports a LangChain tool."""
    pure_tool = get_video_analyzer_tool_pure(mock_ctx)
    assert pure_tool.name == "video_analyzer"

    with patch(
        "artemis.tools.video_tool.VideoAnalyzer.run",
        new_callable=AsyncMock,
        return_value=("Pure tool output", "success"),
    ):
        result = await pure_tool.ainvoke(
            {
                "time_description": "from 2s to 4s",
                "purpose": "Pure invoke check",
            }
        )
        assert result == "Pure tool output"
