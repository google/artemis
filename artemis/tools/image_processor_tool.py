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

"""Universal Image Processor tool for executing image transformations."""

from typing import Any

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from artemis.context import ArtemisContext
from artemis.data_engine.trace import trace, trace_langchain_tool
from artemis.drivers.base import BaseDeviceDriver
from artemis.graph.state import State
from artemis.tools.base import ArtemisTool, ToolRegistry
from artemis.tools.tool_wrapper import ToolWrapper
from artemis.tools.types import CyFunctionDetector
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


# pylint: disable=too-few-public-methods
class AskVisionCoderArgs(BaseModel):
    """Arguments schema for invoking the Image Processor subagent."""

    model_config = {"ignored_types": (CyFunctionDetector,)}
    instruction: str = Field(
        ...,
        description=(
            "Step-by-step instructions on what image processing or"
            " modifications should be performed."
        ),
    )
    target_image_id: str = Field(
        ...,
        description=(
            "The ID of the image in the Image Pool to start from (e.g. 'img_0', 'img_1')."
        ),
    )


# Alias for naming consistency
AskImageProcessorArgs = AskVisionCoderArgs


class AskImageProcessorTool(ArtemisTool):
    """Universal tool for executing image transformations via ImageProcessor subagent."""

    def __init__(self):
        super().__init__(
            name="ask_image_processor",
            description=(
                "Call this tool to ask the image processor agent to run"
                " complex image transformations or modifications."
            ),
            args_schema=AskVisionCoderArgs,
            category="custom",
        )

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    async def execute(
        self,
        driver: BaseDeviceDriver | None = None,  # pylint: disable=unused-argument
        ctx: ArtemisContext | None = None,
        instruction: str | None = None,
        target_image_id: str | None = None,
        state: State | None = None,
        **kwargs: Any,
    ) -> Any:
        inst = instruction or kwargs.get("Instruction") or ""
        tid = target_image_id or kwargs.get("TargetImageId") or kwargs.get("target_image_id") or ""
        st = state if state is not None else kwargs.get("state")
        return await _run_image_processor_logic(ctx, st, inst, tid)


# Universal tool instance & aliases
ask_image_processor = AskImageProcessorTool()
AskImageProcessor = AskImageProcessorTool
AskVisionCoderTool = AskImageProcessorTool
ImageProcessorTool = AskImageProcessorTool
ToolRegistry.register(ask_image_processor)


def get_ask_image_processor_tool(ctx: ArtemisContext) -> BaseTool:
    """Exports ask_image_processor as a LangChain BaseTool."""
    return trace_langchain_tool(ask_image_processor.to_langchain_tool(ctx), ctx)


async def _run_image_processor_logic(
    ctx: ArtemisContext | None,
    state: State | None,
    instruction: str,
    target_image_id: str,  # pylint: disable=unused-argument
) -> Any:
    @trace(type="agent", name="image_processor", ctx=ctx)
    async def run_image_processor_agent(inst: str, img_path: str) -> dict:
        from artemis.agents.image_processor.image_processor import (  # pylint: disable=import-outside-toplevel
            ImageProcessor,
        )

        agent = ImageProcessor(ctx)
        return await agent.run(inst, img_path)

    screenshot_path = state.latest_screenshot if state else None
    result = await run_image_processor_agent(instruction, screenshot_path)
    return result


ask_image_processor_wrapper = ToolWrapper(
    tool_fn_getter=get_ask_image_processor_tool,
    on_success_fn=lambda output: f"Image Processor replied:\n{output}",
    on_failure_fn=lambda error: f"Image Processor failed: {error}",
)
