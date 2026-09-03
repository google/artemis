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

import inspect
from typing import Any

from langchain_core.tools import BaseTool
from pydantic import BaseModel

from artemis.context import ArtemisContext
from artemis.data_engine.trace import trace_langchain_tool
from artemis.drivers.base import BaseDeviceDriver
from artemis.graph.state import State
from artemis.tools.base import ArtemisTool, ToolCategory
from artemis.tools.tool_wrapper import ToolWrapper
from artemis.tools.types import CyFunctionDetector
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


class GetUiHierarchyArgs(BaseModel):
    """Arguments schema for getting UI hierarchy."""

    model_config = {"ignored_types": (CyFunctionDetector,)}


GET_UI_HIERARCHY_DOCSTRING = (
    "[DIAGNOSTIC] Retrieves the current screen UI hierarchy XML using a persistent"
    " UI Automator client.\n\nUse this instead of running 'uiautomator dump'"
    " command via adb shell, as it is much faster and more stable."
)


class GetUiHierarchyTool(ArtemisTool):
    """Universal tool for retrieving screen UI hierarchy XML."""

    def __init__(self, category: ToolCategory = "diagnostic"):
        super().__init__(
            name="get_ui_hierarchy",
            description=GET_UI_HIERARCHY_DOCSTRING,
            args_schema=GetUiHierarchyArgs,
            category=category,
        )

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    async def execute(
        self,
        driver: BaseDeviceDriver | None = None,
        ctx: ArtemisContext | None = None,
        tool_call_id: str | None = None,  # pylint: disable=unused-argument
        state: State | None = None,  # pylint: disable=unused-argument
        **kwargs: Any,
    ) -> str:
        try:
            logger.info("get_ui_hierarchy tool called.")
            if ctx is not None and getattr(ctx, "ui_adb_client", None) is not None:
                xml_hierarchy = ctx.ui_adb_client.get_hierarchy()
                if inspect.iscoroutine(xml_hierarchy):
                    xml_hierarchy = await xml_hierarchy
                return str(xml_hierarchy)
            if driver is not None and hasattr(driver, "get_screen_data"):
                screen_data = await driver.get_screen_data()
                xml_hierarchy = getattr(screen_data, "ui_hierarchy_xml", None)
                if xml_hierarchy:
                    return str(xml_hierarchy)
                return "Error retrieving UI hierarchy: No UI hierarchy in screen data."
            if driver is not None and hasattr(driver, "get_ui_hierarchy"):
                xml_hierarchy = await driver.get_ui_hierarchy()
                if inspect.iscoroutine(xml_hierarchy):
                    xml_hierarchy = await xml_hierarchy
                return str(xml_hierarchy)
            return "Error retrieving UI hierarchy: No UI Automator client or driver provided."
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error(f"Failed to get UI hierarchy: {e}")
            return f"Error retrieving UI hierarchy: {e}"


# Universal tool instance & aliases
get_ui_hierarchy = GetUiHierarchyTool()
GetUiHierarchy = GetUiHierarchyTool
GetUIHierarchy = GetUiHierarchyTool


def get_ui_hierarchy_tool(ctx: ArtemisContext) -> BaseTool:
    """Exports get_ui_hierarchy as a LangChain BaseTool."""
    return trace_langchain_tool(get_ui_hierarchy.to_langchain_tool(ctx), ctx)


ui_hierarchy_wrapper = ToolWrapper(
    tool_fn_getter=get_ui_hierarchy_tool,
    on_success_fn=lambda output: output,
    on_failure_fn=lambda error: f"Failed to retrieve UI hierarchy: {error}",
)
