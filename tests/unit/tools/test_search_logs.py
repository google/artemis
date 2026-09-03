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
from artemis.tools.base import ArtemisTool
from artemis.tools.mobile.search_logs import (
    SearchLogs,
    SearchLogsArgs,
    SearchLogsTool,
    SearchLogsToolAlias,
    get_search_logs_tool,
    search_and_merge_logs,
    search_logs,
)
import pytest


def test_search_and_merge_logs_no_matches():
    logs = "line 0: info\nline 1: info\nline 2: info"
    result = search_and_merge_logs(logs, "ERROR", context_lines=2)
    assert result == "No matches found for 'ERROR'."


def test_search_and_merge_logs_single_match():
    logs = "\n".join([f"line {i}" if i != 5 else "ERROR: failure" for i in range(10)])
    result = search_and_merge_logs(logs, "ERROR", context_lines=2)

    expected = (
        "--- Match at line(s) 5 ---\n"
        "   3:         line 3\n"
        "   4:         line 4\n"
        "   5: [MATCH] ERROR: failure\n"
        "   6:         line 6\n"
        "   7:         line 7\n"
        "--------------------"
    )
    assert result == expected


def test_search_and_merge_logs_overlapping_matches():
    logs = "\n".join([f"line {i}" if i not in [4, 6] else "ERROR: failure" for i in range(10)])
    result = search_and_merge_logs(logs, "ERROR", context_lines=2)

    # Match at 4 (range 2 to 6 inclusive)
    # Match at 6 (range 4 to 8 inclusive)
    # Merged range: 2 to 8 inclusive
    expected = (
        "--- Match at line(s) 4, 6 ---\n"
        "   2:         line 2\n"
        "   3:         line 3\n"
        "   4: [MATCH] ERROR: failure\n"
        "   5:         line 5\n"
        "   6: [MATCH] ERROR: failure\n"
        "   7:         line 7\n"
        "   8:         line 8\n"
        "--------------------"
    )
    assert result == expected


def test_search_and_merge_logs_adjacent_matches():
    # Match at 3 (range 1 to 5 inclusive)
    # Match at 6 (range 4 to 8 inclusive)
    # Overlap is at lines 4-5. They are adjacent/overlapping and should merge.
    logs = "\n".join([f"line {i}" if i not in [3, 6] else "ERROR: failure" for i in range(10)])
    result = search_and_merge_logs(logs, "ERROR", context_lines=2)

    expected = (
        "--- Match at line(s) 3, 6 ---\n"
        "   1:         line 1\n"
        "   2:         line 2\n"
        "   3: [MATCH] ERROR: failure\n"
        "   4:         line 4\n"
        "   5:         line 5\n"
        "   6: [MATCH] ERROR: failure\n"
        "   7:         line 7\n"
        "   8:         line 8\n"
        "--------------------"
    )
    assert result == expected


def test_search_and_merge_logs_multiple_separate_blocks():
    # Match at 2 (range 0 to 4 inclusive)
    # Match at 8 (range 6 to 9 inclusive since len is 10)
    # Non-overlapping since end of first is 4, start of second is 6.
    logs = "\n".join([f"line {i}" if i not in [2, 8] else "ERROR: failure" for i in range(10)])
    result = search_and_merge_logs(logs, "ERROR", context_lines=2)

    expected = (
        "--- Match at line(s) 2 ---\n"
        "   0:         line 0\n"
        "   1:         line 1\n"
        "   2: [MATCH] ERROR: failure\n"
        "   3:         line 3\n"
        "   4:         line 4\n"
        "--------------------\n"
        "--- Match at line(s) 8 ---\n"
        "   6:         line 6\n"
        "   7:         line 7\n"
        "   8: [MATCH] ERROR: failure\n"
        "   9:         line 9\n"
        "--------------------"
    )
    assert result == expected


def test_search_and_merge_logs_regex():
    logs = "line 0\nERR_123: failed\nline 2\nERR_456: critical"
    result = search_and_merge_logs(logs, r"ERR_\d+", context_lines=1, is_regex=True)

    expected = (
        "--- Match at line(s) 1, 3 ---\n"
        "   0:         line 0\n"
        "   1: [MATCH] ERR_123: failed\n"
        "   2:         line 2\n"
        "   3: [MATCH] ERR_456: critical\n"
        "--------------------"
    )
    assert result == expected


def test_search_and_merge_logs_invalid_regex():
    logs = "line 0"
    result = search_and_merge_logs(logs, "[invalid", is_regex=True)
    assert result.startswith("Error: Invalid regex:")


# ---------------------------------------------------------------------------
# SearchLogsTool Unit Tests
# ---------------------------------------------------------------------------
@pytest.fixture
def mock_ctx():
    return MagicMock(spec=ArtemisContext)


def test_search_logs_tool_subclass():
    """Verify SearchLogsTool is a subclass of ArtemisTool."""
    assert issubclass(SearchLogsTool, ArtemisTool)
    assert issubclass(SearchLogs, ArtemisTool)
    assert issubclass(SearchLogsToolAlias, ArtemisTool)
    assert isinstance(search_logs, ArtemisTool)
    assert isinstance(search_logs, SearchLogsTool)

    assert search_logs.name == "search_logs"
    assert search_logs.category == "diagnostic"
    assert search_logs.args_schema == SearchLogsArgs

    # GenAI FunctionDeclaration export
    declaration = search_logs.to_genai_declaration()
    assert declaration.name == "search_logs"
    assert "keyword" in declaration.parameters.properties
    assert "is_regex" in declaration.parameters.properties
    assert "lines" in declaration.parameters.properties
    assert "since_time" in declaration.parameters.properties
    assert "until_time" in declaration.parameters.properties
    assert "context_lines" in declaration.parameters.properties


@pytest.mark.asyncio
async def test_search_logs_direct_execution(mock_ctx):
    """Verify direct execution of SearchLogsTool."""
    mock_log_output = "line 1: FATAL: crash occurred\nline 2: normal"
    with patch(
        "artemis.tools.mobile.search_logs.fetch_and_filter_logs",
        return_value=mock_log_output,
    ) as mock_fetch:
        result = await search_logs.execute(
            ctx=mock_ctx,
            keyword="FATAL",
            is_regex=False,
            lines=5000,
            since_time="08-12 10:00:00",
            until_time="08-12 10:05:00",
            context_lines=1,
        )
        mock_fetch.assert_called_once_with(
            ctx=mock_ctx,
            lines=5000,
            since_time="08-12 10:00:00",
            until_time="08-12 10:05:00",
        )
        assert "[MATCH] line 1: FATAL: crash occurred" in result


@pytest.mark.asyncio
async def test_search_logs_default_args(mock_ctx):
    """Verify search_logs defaults lines to 10000 and context_lines to 0."""
    with patch(
        "artemis.tools.mobile.search_logs.fetch_and_filter_logs",
        return_value="info line\nerror line",
    ) as mock_fetch:
        result = await search_logs.execute(
            ctx=mock_ctx, keyword="error", lines=None, context_lines=None
        )
        mock_fetch.assert_called_once_with(
            ctx=mock_ctx,
            lines=10000,
            since_time=None,
            until_time=None,
        )
        assert "[MATCH] error line" in result


@pytest.mark.asyncio
async def test_search_logs_exception_handling(mock_ctx):
    """Verify error handling when searching logs raises an exception."""
    with patch(
        "artemis.tools.mobile.search_logs.fetch_and_filter_logs",
        side_effect=RuntimeError("Device unreachable"),
    ):
        result = await search_logs.execute(ctx=mock_ctx, keyword="test")
        assert "Error searching logs: Device unreachable" in result


@pytest.mark.asyncio
async def test_search_logs_callable_execution(mock_ctx):
    """Verify invoking search_logs directly as a callable."""
    with patch(
        "artemis.tools.mobile.search_logs.fetch_and_filter_logs",
        return_value="NullPointerException in activity",
    ):
        result = await search_logs(ctx=mock_ctx, keyword="NullPointer", context_lines=0)
        assert "[MATCH] NullPointerException in activity" in result


@pytest.mark.asyncio
async def test_get_search_logs_tool_langchain_ainvoke(mock_ctx):
    """Verify get_search_logs_tool exports a LangChain tool that works with ainvoke."""
    tool = get_search_logs_tool(mock_ctx)
    assert tool.name == "search_logs"

    with patch(
        "artemis.tools.mobile.search_logs.fetch_and_filter_logs",
        return_value="critical error detected",
    ) as mock_fetch:
        result = await tool.ainvoke({"keyword": "critical", "lines": 2000})
        mock_fetch.assert_called_once_with(
            ctx=mock_ctx,
            lines=2000,
            since_time=None,
            until_time=None,
        )
        assert "[MATCH] critical error detected" in result
