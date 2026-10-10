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

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from artemis.context import ArtemisContext
from artemis.data_engine.trace import trace_langchain_tool
from artemis.drivers.base import BaseDeviceDriver
from artemis.tools.base import ArtemisTool, ToolCategory
from artemis.tools.tool_wrapper import ToolWrapper
from artemis.utils.cython_compat import CyFunctionDetector
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)


class MarkDoneArgs(BaseModel):
    """Arguments schema for mark_done tool."""

    model_config = {"ignored_types": (CyFunctionDetector,)}
    reason: str | None = Field(
        None,
        description="One short sentence stating which goal evidence is on screen.",
    )


MARK_DONE_DOCSTRING = (
    "[SHELL] Claims the task goal is complete and requests final verification.\n\n"
    "Call this when the user's goal is already satisfied on screen and no further "
    "device actions are needed — including goals that were already true when the "
    "run started. After this call the checker verifies the screen; if verification "
    "fails the run continues with feedback. Do NOT call this while actions remain "
    "to be performed.\n\n"
    "Prefer this over taking more actions once the goal is visibly achieved."
)


class MarkDoneTool(ArtemisTool):
    """Structural completion claim: routes the run to final verification."""

    def __init__(self, category: ToolCategory = "system"):
        super().__init__(
            name="mark_done",
            description=MARK_DONE_DOCSTRING,
            args_schema=MarkDoneArgs,
            category=category,
        )

    async def execute(
        self,
        driver: BaseDeviceDriver | None = None,  # pylint: disable=unused-argument
        ctx: ArtemisContext | None = None,
        reason: str | None = None,
        **kwargs: Any,
    ) -> str:
        r = reason if reason is not None else (kwargs.get("reason") or "")
        logger.info(f"mark_done called — completion claimed. reason={r!r}")
        if ctx is not None:
            ctx.operator_done_claimed = True
        return (
            "Completion claim recorded. The run now enters final verification: "
            "the checker will confirm the goal on screen. If it cannot verify, "
            "the run continues — do not take further actions in the meantime."
        )


mark_done = MarkDoneTool()
MarkDone = MarkDoneTool


def get_mark_done_tool(ctx: ArtemisContext) -> BaseTool:
    """Exports mark_done as a LangChain BaseTool."""
    return trace_langchain_tool(mark_done.to_langchain_tool(ctx), ctx)


mark_done_wrapper = ToolWrapper(
    tool_fn_getter=get_mark_done_tool,
    on_success_fn=lambda output: output,
    on_failure_fn=lambda err: f"mark_done failed: {err}",
)
