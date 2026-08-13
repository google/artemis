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


class ReadLogsArgs(BaseModel):
    """Arguments schema for read_logs tool."""

    lines: int | None = Field(
        default=200,
        description=(
            "Number of recent log lines to read. Default is 200. Use larger"
            " numbers only if necessary."
        ),
    )
    since_time: str | None = Field(
        default=None,
        description=(
            "Target timestamp in 'MM-DD HH:MM:SS.ms' format OR relative seconds"
            " (e.g., '15.5') to read logs since. If provided, we will try to"
            " fetch logs starting from this time."
        ),
    )
    until_time: str | None = Field(
        default=None,
        description=(
            "Target timestamp in 'MM-DD HH:MM:SS.ms' format OR relative seconds"
            " (e.g., '20.0') to read logs until. If provided, we will filter"
            " logs up to this time."
        ),
    )


READ_LOGS_DOCSTRING = (
    "[DIAGNOSTIC] Fetches raw Android logs.\n\n"
    "- Use when: You need to scan logs for a time window or see the sequence"
    " of events.\n"
    "- Input: lines (int, optional): Number of lines to read (default 200)."
    " since_time (str, optional): Timestamp or relative seconds to read since."
    " until_time (str, optional): Timestamp or relative seconds to read until.\n"
    "- Output: Raw log text."
)


class ReadLogsTool(ArtemisTool):
    """Universal tool for fetching raw Android logs."""

    def __init__(self, category: ToolCategory = "diagnostic"):
        super().__init__(
            name="read_logs",
            description=READ_LOGS_DOCSTRING,
            args_schema=ReadLogsArgs,
            category=category,
        )

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    async def execute(
        self,
        driver: BaseDeviceDriver | None = None,  # pylint: disable=unused-argument
        ctx: ArtemisContext | None = None,
        lines: int | None = 200,
        since_time: str | None = None,
        until_time: str | None = None,
        tool_call_id: str | None = None,  # pylint: disable=unused-argument
        state: State | None = None,  # pylint: disable=unused-argument
        **kwargs: Any,
    ) -> str:
        try:
            num_lines = 200 if lines is None else lines
            logs = fetch_and_filter_logs(
                ctx=ctx,
                lines=num_lines,
                since_time=since_time,
                until_time=until_time,
            )
            return logs
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error(f"Failed to read logs: {e}")
            return f"Failed to read logs: {e}"


# Universal tool instance & aliases
read_logs = ReadLogsTool()
ReadLogs = ReadLogsTool
ReadLogsToolAlias = ReadLogsTool
ToolRegistry.register(read_logs)


def get_read_logs_tool(ctx: ArtemisContext) -> BaseTool:
    """Exports read_logs as a LangChain BaseTool."""
    return trace_langchain_tool(read_logs.to_langchain_tool(ctx), ctx)
