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
from artemis.tools.base import ArtemisTool, ToolRegistry
from artemis.tools.log_tool import (
    AnalyzeLogs,
    AnalyzeLogsArgs,
    AnalyzeLogsTool,
    LogTool,
    analyze_logs,
    analyze_logs_wrapper,
    get_analyze_logs_tool,
)
import pytest


@pytest.mark.asyncio
async def test_analyze_logs_tool_execution():
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = None

    mock_state = MagicMock(spec=State)

    expected_output = "Found 2 errors in system log: NullPointerException at line 42."

    mock_analyst = MagicMock()
    mock_analyst.run = AsyncMock(return_value=expected_output)

    with patch(
        "artemis.tools.log_tool.LogAnalyzerNode",
        return_value=mock_analyst,
    ):
        tool = get_analyze_logs_tool(mock_ctx)

        result = await tool.ainvoke(
            {
                "specific_query": "Check for crash exceptions",
                "state": mock_state,
            }
        )

        mock_analyst.run.assert_called_once_with("Check for crash exceptions", mock_state)
        assert result == expected_output


@pytest.mark.asyncio
async def test_analyze_logs_tool_direct_call():
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_state = MagicMock(spec=State)

    mock_analyst = MagicMock()
    mock_analyst.run = AsyncMock(return_value="Log scan complete.")

    with patch(
        "artemis.tools.log_tool.LogAnalyzerNode",
        return_value=mock_analyst,
    ):
        result = await analyze_logs(
            ctx=mock_ctx,
            specific_query="Find ANR",
            state=mock_state,
        )
        mock_analyst.run.assert_called_once_with("Find ANR", mock_state)
        assert result == "Log scan complete."


@pytest.mark.asyncio
async def test_analyze_logs_tool_no_context():
    result = await analyze_logs(
        ctx=None,
        specific_query="Find crash",
    )
    assert result == "Error: ArtemisContext is required for LogAnalyzer."


def test_analyze_logs_tool_subclass_and_registry():
    """Verify AnalyzeLogsTool is an ArtemisTool subclass and registered in ToolRegistry."""
    assert issubclass(AnalyzeLogsTool, ArtemisTool)
    assert issubclass(AnalyzeLogs, ArtemisTool)
    assert issubclass(LogTool, ArtemisTool)
    assert isinstance(analyze_logs, ArtemisTool)
    assert isinstance(analyze_logs, AnalyzeLogsTool)

    assert analyze_logs.name == "analyze_logs"
    assert analyze_logs.category == "custom"
    assert analyze_logs.args_schema == AnalyzeLogsArgs

    # Registry lookup
    reg_tool = ToolRegistry.get("analyze_logs")
    assert reg_tool is not None
    assert isinstance(reg_tool, AnalyzeLogsTool)

    # GenAI FunctionDeclaration export
    declaration = analyze_logs.to_genai_declaration()
    assert declaration.name == "analyze_logs"
    assert "specific_query" in declaration.parameters.properties

    # Wrapper check
    assert analyze_logs_wrapper is not None
    assert analyze_logs_wrapper.tool_fn_getter == get_analyze_logs_tool
