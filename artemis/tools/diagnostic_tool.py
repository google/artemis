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

"""Universal diagnostic agent tool for deep system logs and video inspection."""

from typing import Any

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from artemis.agents.diagnoser.diagnoser import Diagnoser
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
class AskDiagnoserArgs(BaseModel):
    """Arguments schema for invoking the diagnostic subagent."""

    model_config = {"ignored_types": (CyFunctionDetector,)}
    query: str = Field(
        ...,
        description=(
            "The diagnostic query or what you want to investigate. Please"
            " provide a specific, narrow time range if applicable."
        ),
    )


async def _run_diagnoser_logic(
    ctx: ArtemisContext | None,
    state: State | None,
    query: str,
) -> str:
    logger.info(f"ask_diagnoser tool called with query: {query}")
    if ctx is None:
        return "Error: ArtemisContext is required for Diagnoser."
    agent = Diagnoser(ctx)
    try:
        result = await agent.run(query, state)
        return f"Diagnoser replied:\n{result}"
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error(f"Failed to run diagnoser: {e}")
        return f"Diagnoser failed: {e}"


class AskDiagnoserTool(ArtemisTool):
    """Universal tool for invoking the Diagnoser subagent to inspect logs and videos."""

    def __init__(self):
        super().__init__(
            name="ask_diagnoser",
            description=(
                "[DIAGNOSTIC] Analyzes system logs (ADB) and video recordings to "
                "pinpoint the root cause of a step failure, screen freeze, or "
                "unexpected UI drift. Use it when the live screen does not explain "
                "why an action is not taking effect or why progress is stalled, "
                "before committing to further physical actions. Note: When querying "
                "video files, specify a narrow timeframe (e.g., '[10s, 20s]') for "
                "faster analysis."
            ),
            args_schema=AskDiagnoserArgs,
            category="custom",
        )

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    async def execute(
        self,
        driver: BaseDeviceDriver | None = None,  # pylint: disable=unused-argument
        ctx: ArtemisContext | None = None,
        query: str | None = None,
        state: State | None = None,
        **kwargs: Any,
    ) -> str:
        q = query or kwargs.get("Query") or ""
        st = state if state is not None else kwargs.get("state")
        return await _run_diagnoser_logic(ctx, st, q)


# Universal tool instance & aliases
ask_diagnoser = AskDiagnoserTool()
AskDiagnoser = AskDiagnoserTool
DiagnosticTool = AskDiagnoserTool
ToolRegistry.register(ask_diagnoser)


def get_ask_diagnostic_agent_tool(ctx: ArtemisContext) -> BaseTool:
    """Exports ask_diagnoser as a LangChain BaseTool."""
    return trace_langchain_tool(ask_diagnoser.to_langchain_tool(ctx), ctx)


def get_ask_diagnoser_tool(ctx: ArtemisContext) -> BaseTool:
    """Exports ask_diagnoser as a LangChain BaseTool (standard naming alias)."""
    return trace_langchain_tool(ask_diagnoser.to_langchain_tool(ctx), ctx)


ask_diagnoser_wrapper = ToolWrapper(
    tool_fn_getter=get_ask_diagnostic_agent_tool,
    on_success_fn=lambda output: f"Diagnoser replied:\n{output}",
    on_failure_fn=lambda error: f"Diagnoser failed: {error}",
)
