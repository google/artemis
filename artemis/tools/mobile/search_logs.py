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

import re
from typing import Any

from langchain_core.tools.base import BaseTool
from pydantic import BaseModel, Field

from artemis.context import ArtemisContext
from artemis.data_engine.trace import trace_langchain_tool
from artemis.drivers.base import BaseDeviceDriver
from artemis.graph.state import State
from artemis.tools.base import ArtemisTool, ToolCategory, ToolRegistry
from artemis.tools.mobile.log_utils import fetch_and_filter_logs
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


# pylint: disable=too-many-branches,too-many-locals,too-many-statements
def search_and_merge_logs(
    logs: str,
    keyword: str,
    context_lines: int = 50,
    is_regex: bool = False,
) -> str:
    """Searches for keyword/pattern in logs and merges overlapping context windows."""
    log_lines = logs.splitlines()

    if is_regex:
        try:
            pattern = re.compile(keyword, re.IGNORECASE)
        except re.error as e:
            return f"Error: Invalid regex: {e}"
    else:
        keyword_lower = keyword.lower()

    # 1. Collect all raw match ranges
    ranges = []
    for i, line in enumerate(log_lines):
        matched = False
        if is_regex:
            if pattern.search(line):
                matched = True
        else:
            if keyword_lower in line.lower():
                matched = True

        if matched:
            start = max(0, i - context_lines)
            end = min(len(log_lines), i + context_lines + 1)
            ranges.append((start, end, i))

    if not ranges:
        return f"No matches found for '{keyword}'."

    # 2. Merge overlapping/adjacent ranges
    ranges.sort(key=lambda x: x[0])

    merged_ranges = []
    curr_start, curr_end, curr_matches = (
        ranges[0][0],
        ranges[0][1],
        [ranges[0][2]],
    )

    for start, end, match_idx in ranges[1:]:
        if start <= curr_end:  # Overlaps or adjacent
            curr_end = max(curr_end, end)
            curr_matches.append(match_idx)
        else:
            merged_ranges.append((curr_start, curr_end, curr_matches))
            curr_start = start
            curr_end = end
            curr_matches = [match_idx]
    merged_ranges.append((curr_start, curr_end, curr_matches))

    # 3. Format output
    matches = []
    total_lines = 0
    max_return_lines = 300

    truncated = False
    for start, end, match_indices in merged_ranges:
        if total_lines >= max_return_lines:
            truncated = True
            break

        match_str = ", ".join(map(str, match_indices))
        matches.append(f"--- Match at line(s) {match_str} ---")
        for idx in range(start, end):
            line_content = log_lines[idx]
            if idx in match_indices:
                matches.append(f"{idx:4d}: [MATCH] {line_content}")
            else:
                matches.append(f"{idx:4d}:         {line_content}")

            total_lines += 1
            if total_lines >= max_return_lines:
                truncated = True
                break

        matches.append("-" * 20)

    if truncated:
        matches.append(
            "\n... [SYSTEM WARNING: Output truncated to protect Context Window."
            " Too many matches exceeded the 300 lines limit. You MUST use a"
            " more specific keyword, narrow the time range, or use"
            " context_lines=0.] ..."
        )

    return "\n".join(matches)


class SearchLogsArgs(BaseModel):
    """Arguments schema for search_logs tool."""

    keyword: str = Field(
        ...,
        description=("The keyword or regular expression to search for. Case-insensitive."),
    )
    is_regex: bool = Field(
        default=False,
        description=("Whether to treat keyword as a regular expression. Default is False."),
    )
    lines: int | None = Field(
        default=10000,
        description=("Number of recent log lines to search in. Default is 10000."),
    )
    since_time: str | None = Field(
        default=None,
        description=(
            "Target timestamp in 'MM-DD HH:MM:SS.ms' format OR relative seconds"
            " (e.g., '15.5') to search logs since. If provided, we will search"
            " logs starting from this time."
        ),
    )
    until_time: str | None = Field(
        default=None,
        description=(
            "Target timestamp in 'MM-DD HH:MM:SS.ms' format OR relative seconds"
            " (e.g., '20.0') to search logs until. If provided, we will filter"
            " logs up to this time."
        ),
    )
    context_lines: int | None = Field(
        default=0,
        description=(
            "Number of lines to include before and after each match for"
            " context. Default is 0. Only increase if you need more context"
            " around a very specific match."
        ),
    )


SEARCH_LOGS_DOCSTRING = (
    "[DIAGNOSTIC] Searches logs for a keyword or regex pattern.\n\n"
    "- Use when: Looking for specific errors, tags, or keywords without"
    " reading all logs.\n"
    "- Input: keyword (str): Search pattern. is_regex (bool, optional):"
    " Treat keyword as regex. context_lines (int, optional): Lines around"
    " match. since_time (str, optional): Timestamp or relative seconds to"
    " search since. until_time (str, optional): Timestamp or relative seconds"
    " to search until.\n"
    "- Output: Matching lines with context."
)


class SearchLogsTool(ArtemisTool):
    """Universal tool for searching logs for keywords or regex patterns."""

    def __init__(self, category: ToolCategory = "diagnostic"):
        super().__init__(
            name="search_logs",
            description=SEARCH_LOGS_DOCSTRING,
            args_schema=SearchLogsArgs,
            category=category,
        )

    # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    async def execute(
        self,
        driver: BaseDeviceDriver | None = None,  # pylint: disable=unused-argument
        ctx: ArtemisContext | None = None,
        keyword: str = "",
        is_regex: bool = False,
        lines: int | None = 10000,
        since_time: str | None = None,
        until_time: str | None = None,
        context_lines: int | None = 0,
        tool_call_id: str | None = None,  # pylint: disable=unused-argument
        state: State | None = None,  # pylint: disable=unused-argument
        **kwargs: Any,
    ) -> str:
        try:
            num_lines = 10000 if lines is None else lines
            ctx_lines = 0 if context_lines is None else context_lines
            logs = fetch_and_filter_logs(
                ctx=ctx,
                lines=num_lines,
                since_time=since_time,
                until_time=until_time,
            )
            return search_and_merge_logs(
                logs=logs,
                keyword=keyword,
                context_lines=ctx_lines,
                is_regex=is_regex,
            )
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error(f"Failed to search logs: {e}")
            return f"Error searching logs: {e}"


# Universal tool instance & aliases
search_logs = SearchLogsTool()
SearchLogs = SearchLogsTool
SearchLogsToolAlias = SearchLogsTool
ToolRegistry.register(search_logs)


def get_search_logs_tool(ctx: ArtemisContext) -> BaseTool:
    """Exports search_logs as a LangChain BaseTool."""
    return trace_langchain_tool(search_logs.to_langchain_tool(ctx), ctx)
