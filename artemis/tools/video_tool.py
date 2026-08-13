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

"""Video Analyzer utility for analyzing video content using Gemini models.

This utility sends video files to video-capable Gemini models for analysis
and returns text descriptions based on the provided prompt.
It also exposes a tool for agents to perform structured video analysis.
"""

from typing import Any, Literal

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langgraph.types import Command
from pydantic import BaseModel, Field

from artemis.agents.video_analyzer.video_analyzer import VideoAnalyzer
from artemis.constants import VALIDATOR_MESSAGES_KEY
from artemis.context import ArtemisContext
from artemis.data_engine.trace import trace_langchain_tool
from artemis.drivers.base import BaseDeviceDriver
from artemis.graph.state import State
from artemis.tools.base import ArtemisTool, ToolCategory, ToolRegistry
from artemis.tools.tool_wrapper import ToolWrapper
from artemis.tools.types import CyFunctionDetector
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


class VideoAnalyzerArgs(BaseModel):
    """Arguments schema for video analyzer tool."""

    model_config = {"ignored_types": (CyFunctionDetector,)}
    time_description: str = Field(
        ...,
        description=(
            "The specific time range to analyze, relative to the start of the"
            " recording (e.g., 'from 5s to 12s', 'from 20s to the end')."
        ),
    )
    purpose: str = Field(
        ...,
        description=(
            "The specific query, intent, or question you want the video"
            " analyzer to answer about the visual or audio content in this"
            " segment."
        ),
    )


OPERATOR_VIDEO_ANALYZER_DOCSTRING = (
    "[VIDEO] Use this tool to analyze any specific time range of the video"
    " recording. This tool delegates to a powerful video-analyzing subagent"
    " with full access to the continuous screen recording of your entire"
    " execution session.\n\n"
    "- This tool overcomes your limitation of only receiving discrete"
    " screenshots. Common use cases include (but are not limited to):\n"
    "  * Watching media files: Play the video on the device screen, use"
    " `wait_for_delay` until the video finishes playing, and then call this"
    " tool to analyze the recording.\n"
    "  * Bridging the gap: Inspecting what happened between your last action"
    " and the current screenshot.\n"
    "  * Memory retrieval: Recalling fast-moving or transient information from"
    " a previous step.\n"
    "  * Catching rapid changes: Investigating on-screen information that"
    " changes too quickly for static screenshots to capture.\n"
    "  - Please specify a narrow time range when possible to ensure faster"
    " processing.\n"
    "  - If only audio analysis is needed, state it clearly in your purpose.\n"
)

DIAGNOSER_VIDEO_ANALYZER_DOCSTRING = (
    "[DIAGNOSTIC] Analyzes a video segment for failure diagnosis and"
    " forensics.\n\n"
    "Use this to verify if an action succeeded, check for error popups, or"
    " investigate visual anomalies when a step fails.\n"
    "Provide a narrow time range (e.g., 'from 10s to 15s') to ensure speed.\n"
)


class VideoAnalyzerTool(ArtemisTool):
    """Universal tool for analyzing video content using Gemini models."""

    def __init__(
        self,
        role: Literal["operator", "diagnoser"] = "operator",
        description: str | None = None,
        category: ToolCategory = "perception",
    ):
        chosen_description = description or (
            OPERATOR_VIDEO_ANALYZER_DOCSTRING
            if role == "operator"
            else DIAGNOSER_VIDEO_ANALYZER_DOCSTRING
        )
        super().__init__(
            name="video_analyzer",
            description=chosen_description,
            args_schema=VideoAnalyzerArgs,
            category=category,
        )
        self.role = role

    # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    async def execute(
        self,
        driver: BaseDeviceDriver | None = None,  # pylint: disable=unused-argument
        ctx: ArtemisContext | None = None,
        time_description: str | None = None,
        purpose: str | None = None,
        tool_call_id: str | None = None,
        state: State | None = None,
        **kwargs: Any,
    ) -> Any:
        time_desc = (
            time_description
            if time_description is not None
            else (kwargs.get("time_description") or kwargs.get("TimeDescription") or "")
        )
        purp = (
            purpose
            if purpose is not None
            else (kwargs.get("purpose") or kwargs.get("Purpose") or "")
        )
        tcid = tool_call_id if tool_call_id is not None else kwargs.get("tool_call_id")
        st = state if state is not None else kwargs.get("state")

        try:
            logger.info(f"video_analyzer called for range: {time_desc}")
            if ctx is None:
                raise ValueError("ArtemisContext is required for VideoAnalyzer.")
            agent = VideoAnalyzer(ctx)
            agent_outcome, status = await agent.run(time_desc, purp)
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error(f"Error running video analyzer: {e}")
            agent_outcome = f"Error running video analyzer: {e}"
            status = "error"

        if st and callable(getattr(st, "asanitize_update", None)):
            tool_message = ToolMessage(
                tool_call_id=tcid or "",
                content=agent_outcome,
                status=status,
            )
            return Command(
                update=await st.asanitize_update(
                    ctx=ctx,
                    update={
                        VALIDATOR_MESSAGES_KEY: [tool_message],
                    },
                    agent="validator",
                ),
            )

        if status == "failed":
            return f"Video analysis failed: {agent_outcome}"
        return agent_outcome


# Universal tool instance & aliases
video_analyzer = VideoAnalyzerTool()
ToolRegistry.register(video_analyzer)


class VideoAnalyzerPureTool(ArtemisTool):
    """Universal pure tool for video analysis returning text results directly."""

    def __init__(self):
        super().__init__(
            name="video_analyzer_pure",
            description=OPERATOR_VIDEO_ANALYZER_DOCSTRING,
            args_schema=VideoAnalyzerArgs,
            category="perception",
        )

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    async def execute(
        self,
        driver: BaseDeviceDriver | None = None,  # pylint: disable=unused-argument
        ctx: ArtemisContext | None = None,
        time_description: str | None = None,
        purpose: str | None = None,
        **kwargs: Any,
    ) -> str:
        time_desc = (
            time_description
            if time_description is not None
            else (kwargs.get("time_description") or kwargs.get("TimeDescription") or "")
        )
        purp = (
            purpose
            if purpose is not None
            else (kwargs.get("purpose") or kwargs.get("Purpose") or "")
        )

        try:
            logger.info(f"video_analyzer (pure) called for range: {time_desc}")
            if ctx is None:
                raise ValueError("ArtemisContext is required for VideoAnalyzer.")
            agent = VideoAnalyzer(ctx)
            agent_outcome, status = await agent.run(time_desc, purp)
            if status == "failed":
                return f"Video analysis failed: {agent_outcome}"
            return agent_outcome
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error(f"Error in pure video_analyzer tool: {e}")
            return f"Error running video analyzer: {e}"


# Universal pure tool instance & aliases
video_analyzer_pure = VideoAnalyzerPureTool()
VideoAnalyzerPure = VideoAnalyzerPureTool
ToolRegistry.register(video_analyzer_pure)


def get_video_analyzer_tool(
    ctx: ArtemisContext, role: Literal["operator", "diagnoser"] = "operator"
) -> BaseTool:
    """Creates a tool that allows agents to perform structured video analysis.

    The tool acts as a Main Agent following the Dynamic Video Analyzer plan.
    """
    tool_inst = VideoAnalyzerTool(role=role)
    return trace_langchain_tool(tool_inst.to_langchain_tool(ctx), ctx)


def get_video_analyzer_tool_pure(ctx: ArtemisContext) -> BaseTool:
    """Creates a pure LangChain tool for video analysis that returns the text result directly.

    Suitable for agents running outside of LangGraph (like the outputter).
    """
    return trace_langchain_tool(
        video_analyzer_pure.to_langchain_tool(ctx, name="video_analyzer"), ctx
    )


video_analyzer_wrapper = ToolWrapper(
    tool_fn_getter=get_video_analyzer_tool,
    on_success_fn=lambda output: f"Video Analyzer replied:\n{output}",
    on_failure_fn=lambda error: f"Video Analyzer failed: {error}",
)
