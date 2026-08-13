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

"""Universal Log Analyzer tool for inspecting and analyzing Android system logs."""

from typing import Any

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from artemis.agents.log_analyzer.log_analyzer import LogAnalyzerNode
from artemis.context import ArtemisContext
from artemis.data_engine.trace import trace_langchain_tool
from artemis.drivers.base import BaseDeviceDriver
from artemis.graph.state import State
from artemis.tools.base import ArtemisTool, ToolRegistry
from artemis.tools.tool_wrapper import ToolWrapper
from artemis.tools.types import CyFunctionDetector
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


# pylint: disable=too-few-public-methods
class AnalyzeLogsArgs(BaseModel):
    """Arguments schema for invoking the Log Analyzer subagent."""

    model_config = {"ignored_types": (CyFunctionDetector,)}
    specific_query: str = Field(
        ...,
        description=(
            "Instructions for the log analyzer, specifying what to search for or analyze."
        ),
    )


class AnalyzeLogsTool(ArtemisTool):
    """Universal tool for analyzing Android logs using a dedicated hybrid log analyzer agent."""

    def __init__(self):
        super().__init__(
            name="analyze_logs",
            description=(
                "[DIAGNOSTIC] Analyzes Android logs using a dedicated hybrid log analyzer agent. "
                "Use this when you need to investigate failures, check for specific error "
                "logs, or understand system events."
            ),
            args_schema=AnalyzeLogsArgs,
            category="custom",
        )

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    async def execute(
        self,
        driver: BaseDeviceDriver | None = None,  # pylint: disable=unused-argument
        ctx: ArtemisContext | None = None,
        specific_query: str | None = None,
        state: State | None = None,
        **kwargs: Any,
    ) -> Any:
        query = (
            specific_query
            if specific_query is not None
            else (
                kwargs.get("specific_query")
                or kwargs.get("SpecificQuery")
                or kwargs.get("query")
                or kwargs.get("Query")
                or ""
            )
        )
        st = state if state is not None else kwargs.get("state")
        logger.info(f"analyze_logs tool called with query: '{query}'")
        if ctx is None:
            return "Error: ArtemisContext is required for LogAnalyzer."
        analyst = LogAnalyzerNode(ctx)
        result = await analyst.run(query, st)
        return result


# Universal tool instance & aliases
analyze_logs = AnalyzeLogsTool()
AnalyzeLogs = AnalyzeLogsTool
LogTool = AnalyzeLogsTool
ToolRegistry.register(analyze_logs)


def get_analyze_logs_tool(ctx: ArtemisContext) -> BaseTool:
    """Exports analyze_logs as a LangChain BaseTool."""
    return trace_langchain_tool(analyze_logs.to_langchain_tool(ctx), ctx)


analyze_logs_wrapper = ToolWrapper(
    tool_fn_getter=get_analyze_logs_tool,
    on_success_fn=lambda output: f"Log Analyzer replied:\n{output}",
    on_failure_fn=lambda error: f"Log Analyzer failed: {error}",
)
