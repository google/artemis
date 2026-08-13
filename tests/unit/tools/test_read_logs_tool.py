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

from unittest.mock import MagicMock, patch

from artemis.context import ArtemisContext
from artemis.tools.base import ArtemisTool, ToolRegistry
from artemis.tools.mobile.read_logs import (
    ReadLogs,
    ReadLogsArgs,
    ReadLogsTool,
    ReadLogsToolAlias,
    get_read_logs_tool,
    read_logs,
)
import pytest


@pytest.fixture
def mock_ctx():
    ctx = MagicMock(spec=ArtemisContext)
    return ctx


def test_read_logs_tool_subclass_and_registry():
    """Verify ReadLogsTool is a subclass of ArtemisTool and properly registered."""
    assert issubclass(ReadLogsTool, ArtemisTool)
    assert issubclass(ReadLogs, ArtemisTool)
    assert issubclass(ReadLogsToolAlias, ArtemisTool)
    assert isinstance(read_logs, ArtemisTool)
    assert isinstance(read_logs, ReadLogsTool)

    assert read_logs.name == "read_logs"
    assert read_logs.category == "diagnostic"
    assert read_logs.args_schema == ReadLogsArgs

    # Registry lookup
    reg_tool = ToolRegistry.get("read_logs")
    assert reg_tool is not None
    assert isinstance(reg_tool, ReadLogsTool)

    # GenAI FunctionDeclaration export
    declaration = read_logs.to_genai_declaration()
    assert declaration.name == "read_logs"
    assert "lines" in declaration.parameters.properties
    assert "since_time" in declaration.parameters.properties
    assert "until_time" in declaration.parameters.properties


@pytest.mark.asyncio
async def test_read_logs_direct_execution(mock_ctx):
    """Verify direct execution of ReadLogsTool."""
    mock_log_output = "08-12 10:00:00.000 1000 1000 I Test: Hello Logs\n"
    with patch(
        "artemis.tools.mobile.read_logs.fetch_and_filter_logs",
        return_value=mock_log_output,
    ) as mock_fetch:
        result = await read_logs.execute(
            ctx=mock_ctx,
            lines=100,
            since_time="08-12 09:59:00",
            until_time="08-12 10:01:00",
        )
        mock_fetch.assert_called_once_with(
            ctx=mock_ctx,
            lines=100,
            since_time="08-12 09:59:00",
            until_time="08-12 10:01:00",
        )
        assert result == mock_log_output


@pytest.mark.asyncio
async def test_read_logs_default_lines(mock_ctx):
    """Verify read_logs defaults lines to 200 when None."""
    with patch(
        "artemis.tools.mobile.read_logs.fetch_and_filter_logs",
        return_value="logs",
    ) as mock_fetch:
        result = await read_logs.execute(ctx=mock_ctx, lines=None)
        mock_fetch.assert_called_once_with(
            ctx=mock_ctx,
            lines=200,
            since_time=None,
            until_time=None,
        )
        assert result == "logs"


@pytest.mark.asyncio
async def test_read_logs_exception_handling(mock_ctx):
    """Verify error handling when fetching logs raises an exception."""
    with patch(
        "artemis.tools.mobile.read_logs.fetch_and_filter_logs",
        side_effect=RuntimeError("Device disconnected"),
    ):
        result = await read_logs.execute(ctx=mock_ctx)
        assert "Failed to read logs: Device disconnected" in result


@pytest.mark.asyncio
async def test_read_logs_callable_execution(mock_ctx):
    """Verify invoking read_logs directly as a callable."""
    with patch(
        "artemis.tools.mobile.read_logs.fetch_and_filter_logs",
        return_value="sample log stream",
    ):
        result = await read_logs(ctx=mock_ctx, lines=50)
        assert result == "sample log stream"


@pytest.mark.asyncio
async def test_get_read_logs_tool_langchain_ainvoke(mock_ctx):
    """Verify get_read_logs_tool exports a LangChain tool that works with ainvoke."""
    tool = get_read_logs_tool(mock_ctx)
    assert tool.name == "read_logs"

    with patch(
        "artemis.tools.mobile.read_logs.fetch_and_filter_logs",
        return_value="langchain log content",
    ) as mock_fetch:
        result = await tool.ainvoke({"lines": 300})
        mock_fetch.assert_called_once_with(
            ctx=mock_ctx,
            lines=300,
            since_time=None,
            until_time=None,
        )
        assert result == "langchain log content"
