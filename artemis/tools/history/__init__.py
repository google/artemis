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

"""Shared history search, step replay, and screenshot tools.

Each tool's args_schema defines both its LangChain interface for Pro and its
ToolDeclaration for Flash. The MCP trace inspector calls the underlying
functions with an OfflineHistoryReader.
"""

from __future__ import annotations

from typing import Any, Literal

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from artemis.core.tool_declaration import ToolDeclaration
from artemis.data_engine.trace import trace_langchain_tool
from artemis.tools.base import ArtemisTool
from artemis.tools.history.replay import (
    DEFAULT_MAX_STEPS as DEFAULT_REPLAY_MAX_STEPS,
)
from artemis.tools.history.replay import (
    DEFAULT_MAX_TOKENS as DEFAULT_REPLAY_MAX_TOKENS,
)
from artemis.tools.history.replay import replay_steps_text
from artemis.tools.history.screenshot import ScreenshotResult, load_step_screenshot
from artemis.tools.history.search import search_history_text
from artemis.tools.tool_wrapper import ToolWrapper
from artemis.utils.logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "HISTORY_TOOLS",
    "HISTORY_TOOL_DECLARATIONS",
    "HISTORY_TOOL_NAMES",
    "HISTORY_TOOL_WRAPPERS",
    "GET_STEP_SCREENSHOT_TOOL",
    "REPLAY_STEPS_TOOL",
    "SEARCH_HISTORY_TOOL",
    "GetStepScreenshotArgs",
    "GetStepScreenshotTool",
    "ReplayStepsArgs",
    "ReplayStepsTool",
    "ScreenshotResult",
    "SearchHistoryArgs",
    "SearchHistoryTool",
    "get_history_tools",
    "get_replay_steps_tool",
    "get_search_history_tool",
    "get_step_screenshot",
    "get_step_screenshot_tool",
    "history_available",
    "history_tool_by_name",
    "history_tool_declarations",
    "load_step_screenshot",
    "replay_steps",
    "replay_steps_text",
    "search_history",
    "search_history_available",
    "search_history_text",
]


# --- Configuration -----------------------------------------------------------------------


def _memory_config(warn: bool = True) -> Any:
    try:
        from artemis.config import load_agent_config

        return load_agent_config().memory
    except Exception as e:
        if warn:
            logger.debug(f"memory config unavailable, using defaults: {e}")
        return None


def _recall_config(warn: bool = True) -> Any:
    return getattr(_memory_config(warn), "recall", None)


def _replay_config(warn: bool = True) -> Any:
    return getattr(_memory_config(warn), "replay", None)


def history_available(ctx: Any) -> bool:
    """History tools need a stored history to read (a DataEngine session)."""
    return ctx is not None and getattr(ctx, "data_engine", None) is not None


def search_history_available(ctx: Any) -> bool:
    """``search_history`` is additionally gated by ``agent.memory.recall.enabled``."""
    if not history_available(ctx):
        return False
    return bool(getattr(_recall_config(warn=False), "enabled", True))


# --- search_history ----------------------------------------------------------------------


class SearchHistoryArgs(BaseModel):
    """Arguments schema for the search_history tool."""

    query: str = Field(
        default="",
        description=(
            "Keywords to search for across the whole execution history"
            " (screen descriptions, actions, results, reasoning, tool calls,"
            " notes, on-screen text, package/activity names). Case-insensitive;"
            " every whitespace-separated term is matched independently. May be"
            " empty when step_range is given."
        ),
    )
    step_range: list[int] | None = Field(
        default=None,
        description=(
            "Optional [start, end] step-number range (inclusive) to restrict the"
            " search — e.g. the range shown in a compressed-history block. Also"
            " returns the full per-step action ledger of the range."
        ),
    )
    max_results: int = Field(
        default=5,
        ge=1,
        description="Maximum results to return (server-side cap still applies).",
    )


SEARCH_HISTORY_DESCRIPTION = (
    "Deterministic keyword / step-range lookup over the full stored execution"
    " history: step summaries, exact actions and results, reasoning, tool"
    " calls, notes, on-screen text (OCR/UI tree, incl. package/activity names)"
    " and compressed-history ledgers. Every hit carries its step number; use"
    " `replay_steps` for the full record of a step and `get_step_screenshot`"
    " for its image."
)


class SearchHistoryTool(ArtemisTool):
    """Deterministic search over the stored execution history."""

    def __init__(self):
        super().__init__(
            name="search_history",
            description=SEARCH_HISTORY_DESCRIPTION,
            args_schema=SearchHistoryArgs,
            category="memory",
        )

    def is_available(self, ctx: Any = None) -> bool:
        return search_history_available(ctx)

    # pylint: disable=too-many-arguments
    async def execute(
        self,
        driver: Any = None,  # pylint: disable=unused-argument
        ctx: Any = None,
        query: str = "",
        step_range: list[int] | None = None,
        max_results: int = 5,
        **kwargs: Any,
    ) -> str:
        reader = getattr(ctx, "data_engine", None) if ctx else None
        if reader is None:
            return "search_history unavailable: no active execution history."
        if not query and not step_range:
            return (
                "search_history needs a query and/or a step_range — e.g."
                ' search_history(query="login timeout") or'
                ' search_history(query="", step_range=[1, 40]).'
            )
        try:
            return search_history_text(
                reader,
                query=query or "",
                step_range=step_range,
                max_results=int(max_results or 5),
                recall_config=_recall_config(),
            )
        except Exception as e:
            logger.error(f"search_history failed: {e}")
            return f"search_history failed: {e}"


# --- replay_steps ------------------------------------------------------------------------


class ReplayStepsArgs(BaseModel):
    """Arguments schema for the replay_steps tool."""

    start_step: int = Field(..., description="First step to replay (1-based step number).")
    end_step: int | None = Field(
        default=None,
        description=(
            "Last step of the inclusive range to replay; omit to replay the single"
            " start_step. Reversed bounds are swapped."
        ),
    )


REPLAY_STEPS_DESCRIPTION = (
    "Replays recorded steps exactly as the executing agent's own context showed"
    " them: what the screen showed, its reasoning, every tool call (name,"
    " arguments, result), the planned action, any pre-execution interception"
    " and the execution result. Coordinates normalized [x, y]. Up to"
    f" {DEFAULT_REPLAY_MAX_STEPS} steps per call. Screenshots are not included"
    " — fetch them with `get_step_screenshot`."
)


class ReplayStepsTool(ArtemisTool):
    """Full replay of a range of recorded steps."""

    def __init__(self):
        super().__init__(
            name="replay_steps",
            description=REPLAY_STEPS_DESCRIPTION,
            args_schema=ReplayStepsArgs,
            category="memory",
        )

    def is_available(self, ctx: Any = None) -> bool:
        return history_available(ctx)

    async def execute(
        self,
        driver: Any = None,  # pylint: disable=unused-argument
        ctx: Any = None,
        start_step: Any = None,
        end_step: Any = None,
        **kwargs: Any,
    ) -> str:
        reader = getattr(ctx, "data_engine", None) if ctx else None
        if reader is None:
            return "Error: no execution history available."
        cfg = _replay_config()
        try:
            return replay_steps_text(
                reader,
                start_step,
                end_step,
                max_steps=int(getattr(cfg, "max_steps", None) or DEFAULT_REPLAY_MAX_STEPS),
                max_tokens=int(getattr(cfg, "max_tokens", None) or DEFAULT_REPLAY_MAX_TOKENS),
            )
        except Exception as e:
            logger.error(f"replay_steps failed: {e}")
            return f"replay_steps failed: {e}"


# --- get_step_screenshot -----------------------------------------------------------------


class GetStepScreenshotArgs(BaseModel):
    """Arguments schema for the get_step_screenshot tool."""

    step_number: int = Field(..., description="The step whose screenshot to attach (1-based).")
    which: Literal["pre", "post", "overlay"] = Field(
        default="pre",
        description=(
            "'pre' = screen observed at the start of the step; 'post' = screen"
            " after the action (when recorded); 'overlay' = the pre screenshot"
            " with the step's action drawn on it."
        ),
    )


GET_STEP_SCREENSHOT_DESCRIPTION = (
    "Attaches one recorded screenshot of a step: 'pre' (screen observed at the"
    " start of the step), 'post' (screen after the action, when recorded) or"
    " 'overlay' (the pre screenshot with the step's action drawn on it — tap"
    " point / swipe path — to check the action landed on the intended element)."
)


class GetStepScreenshotTool(ArtemisTool):
    """One recorded step screenshot, attached to the conversation."""

    def __init__(self):
        super().__init__(
            name="get_step_screenshot",
            description=GET_STEP_SCREENSHOT_DESCRIPTION,
            args_schema=GetStepScreenshotArgs,
            category="memory",
        )

    def is_available(self, ctx: Any = None) -> bool:
        return history_available(ctx)

    async def execute(
        self,
        driver: Any = None,  # pylint: disable=unused-argument
        ctx: Any = None,
        step_number: Any = None,
        which: str = "pre",
        **kwargs: Any,
    ) -> list[dict[str, Any]] | str:
        reader = getattr(ctx, "data_engine", None) if ctx else None
        if reader is None:
            return "Error: no execution history available."
        try:
            return load_step_screenshot(reader, step_number, which).to_content_blocks()
        except Exception as e:
            logger.error(f"get_step_screenshot failed: {e}")
            return f"get_step_screenshot failed: {e}"


# --- Instances, declarations, exports ------------------------------------------------------

search_history = SearchHistoryTool()
replay_steps = ReplayStepsTool()
get_step_screenshot = GetStepScreenshotTool()

HISTORY_TOOLS: tuple[ArtemisTool, ...] = (search_history, replay_steps, get_step_screenshot)
HISTORY_TOOL_NAMES: frozenset[str] = frozenset(t.name for t in HISTORY_TOOLS)

#: Flash-profile declarations, derived from the very same args schemas.
SEARCH_HISTORY_TOOL: ToolDeclaration = search_history.to_tool_declaration()
REPLAY_STEPS_TOOL: ToolDeclaration = replay_steps.to_tool_declaration()
GET_STEP_SCREENSHOT_TOOL: ToolDeclaration = get_step_screenshot.to_tool_declaration()
HISTORY_TOOL_DECLARATIONS: tuple[ToolDeclaration, ...] = (
    SEARCH_HISTORY_TOOL,
    REPLAY_STEPS_TOOL,
    GET_STEP_SCREENSHOT_TOOL,
)


def history_tool_by_name(name: str) -> ArtemisTool | None:
    """The shared tool instance behind a history tool name (Flash dispatch)."""
    raw = name.split(":")[-1] if ":" in name else name
    return next((t for t in HISTORY_TOOLS if t.name == raw), None)


def history_tool_declarations(ctx: Any) -> list[ToolDeclaration]:
    """Declarations of the history tools available for ``ctx`` (Flash runner)."""
    return [tool.to_tool_declaration() for tool in HISTORY_TOOLS if tool.is_available(ctx)]


def get_search_history_tool(ctx: Any) -> BaseTool:
    """Exports search_history as a traced LangChain BaseTool."""
    return trace_langchain_tool(search_history.to_langchain_tool(ctx), ctx)


def get_replay_steps_tool(ctx: Any) -> BaseTool:
    """Exports replay_steps as a traced LangChain BaseTool."""
    return trace_langchain_tool(replay_steps.to_langchain_tool(ctx), ctx)


def get_step_screenshot_tool(ctx: Any) -> BaseTool:
    """Exports get_step_screenshot as a traced LangChain BaseTool."""
    return trace_langchain_tool(get_step_screenshot.to_langchain_tool(ctx), ctx)


def get_history_tools(ctx: Any) -> list[BaseTool]:
    """All three history tools as LangChain tools (read-only agents mount them as a set)."""
    return [get_search_history_tool(ctx), get_replay_steps_tool(ctx), get_step_screenshot_tool(ctx)]


search_history_wrapper = ToolWrapper(
    tool_fn_getter=get_search_history_tool,
    on_success_fn=lambda *a, **k: "Searched history",
    on_failure_fn=lambda err: f"search_history failed: {err}",
    is_available_fn=search_history_available,
)

replay_steps_wrapper = ToolWrapper(
    tool_fn_getter=get_replay_steps_tool,
    on_success_fn=lambda *a, **k: "Replayed steps",
    on_failure_fn=lambda err: f"replay_steps failed: {err}",
    is_available_fn=history_available,
)

get_step_screenshot_wrapper = ToolWrapper(
    tool_fn_getter=get_step_screenshot_tool,
    on_success_fn=lambda *a, **k: "Fetched step screenshot",
    on_failure_fn=lambda err: f"get_step_screenshot failed: {err}",
    is_available_fn=history_available,
)

HISTORY_TOOL_WRAPPERS: tuple[ToolWrapper, ...] = (
    search_history_wrapper,
    replay_steps_wrapper,
    get_step_screenshot_wrapper,
)
