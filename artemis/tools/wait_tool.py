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

import asyncio
from typing import Any

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from artemis.context import ArtemisContext
from artemis.data_engine.trace import trace_langchain_tool
from artemis.drivers.base import BaseDeviceDriver
from artemis.tools.base import ArtemisTool, ToolCategory, ToolRegistry
from artemis.tools.tool_wrapper import ToolWrapper
from artemis.tools.types import CyFunctionDetector
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


class WaitArgs(BaseModel):
    """Arguments schema for wait tool."""

    model_config = {"ignored_types": (CyFunctionDetector,)}
    seconds: int = Field(
        ...,
        description=("The duration to wait/sleep in seconds. Minimum is 1, maximum is 60."),
    )


WAIT_DOCSTRING = (
    "[SHELL] Pauses execution for a specified duration in seconds.\n\n"
    "Use this to wait for active background operations to progress."
)


class WaitTool(ArtemisTool):
    """Universal tool for pausing execution for a specified duration in seconds."""

    def __init__(self, category: ToolCategory = "system"):
        super().__init__(
            name="wait",
            description=WAIT_DOCSTRING,
            args_schema=WaitArgs,
            category=category,
        )

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    async def execute(
        self,
        driver: BaseDeviceDriver | None = None,  # pylint: disable=unused-argument
        ctx: ArtemisContext | None = None,  # pylint: disable=unused-argument
        seconds: int | None = None,
        **kwargs: Any,
    ) -> str:
        sec = (
            seconds
            if seconds is not None
            else (kwargs.get("seconds") or kwargs.get("Seconds") or 1)
        )
        if sec < 1:
            sec = 1
        elif sec > 60:
            sec = 60

        logger.info(f"Diagnoser waiting for {sec} seconds...")
        await asyncio.sleep(sec)
        return f"Successfully waited for {sec} seconds."


# Universal tool instance & aliases
wait = WaitTool()
Wait = WaitTool
ToolRegistry.register(wait)


def get_wait_tool(ctx: ArtemisContext) -> BaseTool:
    """Exports wait as a LangChain BaseTool."""
    return trace_langchain_tool(wait.to_langchain_tool(ctx), ctx)


wait_wrapper = ToolWrapper(
    tool_fn_getter=get_wait_tool,
    on_success_fn=lambda seconds: f"Waited {seconds}s",
    on_failure_fn=lambda err: f"Wait failed: {err}",
)
export_wait_tool = get_wait_tool
