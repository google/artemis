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

"""Universal Object Detection tool for visual element detection and localization."""

import json
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from artemis.agents.object_detector.object_detector import (
    _create_error_command,
    _create_success_command,
    _run_object_detection,
)
from artemis.context import ArtemisContext
from artemis.data_engine.trace import (
    TraceSpan,
    trace_langchain_tool,
)
from artemis.drivers.base import BaseDeviceDriver
from artemis.graph.state import State
from artemis.tools.base import ArtemisTool, ToolCategory
from artemis.tools.tool_wrapper import ToolWrapper
from artemis.tools.types import CyFunctionDetector
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


# pylint: disable=too-few-public-methods
class ObjectDetectionArgs(BaseModel):
    """Arguments schema for object detection with explicit screenshot path."""

    model_config = {"ignored_types": (CyFunctionDetector,)}
    image_path: str = Field(..., description="Absolute path to the screenshot file.")
    queries: list[str] = Field(..., description="A list of single query strings to find.")


# pylint: disable=too-few-public-methods
class OperatorObjectDetectionArgs(BaseModel):
    """Arguments schema for operator object detection using latest screenshot from state."""

    model_config = {"ignored_types": (CyFunctionDetector,)}
    queries: list[str] = Field(..., description="A list of single query strings to find.")


def _load_prompt_templates() -> list[str]:
    """Loads prompt templates and instructions from object_detector.json config."""
    prompt_path = Path(__file__).parent.parent.joinpath(
        "agents", "object_detector", "object_detector.json"
    )
    try:
        with open(prompt_path, encoding="utf-8") as f:
            prompt_config = json.load(f)
        base_templates = prompt_config.get("templates", [])
        instructions = prompt_config.get("instructions", "")
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.warning(f"Failed to load prompt config from JSON: {e}. Using fallbacks.")
        base_templates = ["Point to the following objects in the provided image: {labels_str}."]
        instructions = ""

    return [f"{t}\n\n{instructions}" for t in base_templates]


# pylint: disable=too-many-arguments,too-many-positional-arguments,broad-exception-caught
async def _run_object_detection_logic(
    ctx: ArtemisContext | None,
    image_path: str | None,
    queries: list[str] | None,
    state: State | None = None,
    tool_call_id: str | None = None,
    wrapper: ToolWrapper | None = None,
) -> Any:
    """Core async execution logic for object detection."""
    logger.info("Object detection tool called.")
    active_wrapper = wrapper or object_detection_wrapper
    templates = _load_prompt_templates()
    target_image = image_path
    if not target_image and state and hasattr(state, "latest_screenshot"):
        target_image = state.latest_screenshot

    if not target_image:
        error_msg = "No image path provided and no screenshot available in state."
        if state is not None:
            return await _create_error_command(ctx, state, tool_call_id, error_msg, active_wrapper)
        return f"Object Detection failed: {error_msg}"

    target_queries = queries or []

    try:
        with TraceSpan(name="run_object_detection", ctx=ctx) as span:
            result = await _run_object_detection(ctx, target_image, target_queries, templates)
            span.result = result
            output = json.dumps(result) if not isinstance(result, str) else result

        if state is not None:
            return await _create_success_command(ctx, state, tool_call_id, output, active_wrapper)
        return output
    except Exception as e:
        logger.error(f"Object detection execution failed: {e}")
        if state is not None:
            return await _create_error_command(ctx, state, tool_call_id, str(e), active_wrapper)
        return f"Object Detection failed: {e}"


class ObjectDetectionTool(ArtemisTool):
    """Universal tool for detecting visual elements or icons in a mobile screen image."""

    def __init__(
        self,
        name: str = "object_detection",
        description: str | None = None,
        args_schema: type[BaseModel] = ObjectDetectionArgs,
        category: ToolCategory = "perception",
    ):
        super().__init__(
            name=name,
            description=description
            or (
                "[EXPLORER] Detects specific visual elements or icons in a mobile screen image. "
                "Use this tool when you need to find the coordinates of UI elements like "
                "buttons, icons, or text fields."
            ),
            args_schema=args_schema,
            category=category,
        )

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    async def execute(
        self,
        driver: BaseDeviceDriver | None = None,  # pylint: disable=unused-argument
        ctx: ArtemisContext | None = None,
        image_path: str | None = None,
        queries: list[str] | None = None,
        state: State | None = None,
        tool_call_id: str | None = None,
        **kwargs: Any,
    ) -> Any:
        img_path = (
            image_path
            if image_path is not None
            else (kwargs.get("image_path") or kwargs.get("ImagePath"))
        )
        qs = (
            queries
            if queries is not None
            else (kwargs.get("queries") or kwargs.get("Queries") or [])
        )
        st = state if state is not None else kwargs.get("state")
        tcid = tool_call_id if tool_call_id is not None else kwargs.get("tool_call_id")
        wrapper = kwargs.get("wrapper") or object_detection_wrapper
        return await _run_object_detection_logic(
            ctx=ctx,
            image_path=img_path,
            queries=qs,
            state=st,
            tool_call_id=tcid,
            wrapper=wrapper,
        )


class OperatorObjectDetectionTool(ObjectDetectionTool):
    """Universal tool for operator detecting visual elements using screenshot from state."""

    def __init__(self):
        super().__init__(
            name="object_detection",
            description=(
                "[EXPLORER] Detects specific visual elements or icons in a mobile screen image. "
                "Use this tool when you need to find the coordinates of UI elements like "
                "buttons, icons, or text fields."
            ),
            args_schema=OperatorObjectDetectionArgs,
            category="perception",
        )

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    async def execute(
        self,
        driver: BaseDeviceDriver | None = None,  # pylint: disable=unused-argument
        ctx: ArtemisContext | None = None,
        image_path: str | None = None,
        queries: list[str] | None = None,
        state: State | None = None,
        tool_call_id: str | None = None,
        **kwargs: Any,
    ) -> Any:
        return await super().execute(
            driver=driver,
            ctx=ctx,
            image_path=image_path,
            queries=queries,
            state=state,
            tool_call_id=tool_call_id,
            wrapper=operator_object_detection_wrapper,
            **kwargs,
        )


# Universal tool instances & aliases
object_detection = ObjectDetectionTool()
ObjectDetection = ObjectDetectionTool
ObjectDetectorTool = ObjectDetectionTool

operator_object_detection = OperatorObjectDetectionTool()
OperatorObjectDetection = OperatorObjectDetectionTool
OperatorObjectDetectorTool = OperatorObjectDetectionTool


def get_object_detector_tool(ctx: ArtemisContext) -> BaseTool:
    """Exports object_detection as a LangChain BaseTool."""
    return trace_langchain_tool(object_detection.to_langchain_tool(ctx), ctx)


def get_operator_object_detector_tool(ctx: ArtemisContext) -> BaseTool:
    """Exports operator_object_detection as a LangChain BaseTool."""
    return trace_langchain_tool(operator_object_detection.to_langchain_tool(ctx), ctx)


object_detection_wrapper = ToolWrapper(
    tool_fn_getter=get_object_detector_tool,
    on_success_fn=lambda output: f"Object Detection successful. Results:\n{output}",
    on_failure_fn=lambda error: f"Object Detection failed: {error}",
)

operator_object_detection_wrapper = ToolWrapper(
    tool_fn_getter=get_operator_object_detector_tool,
    on_success_fn=lambda output: f"Object Detection successful. Results:\n{output}",
    on_failure_fn=lambda error: f"Object Detection failed: {error}",
)
