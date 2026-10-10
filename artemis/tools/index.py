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

from langchain_core.tools import BaseTool

from artemis.context import ArtemisContext
from artemis.data_engine.trace import trace_langchain_tool
from artemis.tools.tool_wrapper import ToolWrapper
from third_party.mobile_use.tools.index import (
    get_tools_from_wrappers as build_tools_from_wrappers,
)


def get_tools_from_wrappers(
    ctx: "ArtemisContext",
    wrappers: list[ToolWrapper],
) -> list[BaseTool]:
    """Instantiate the available tools of ``wrappers``, traced into the data engine."""
    tools = build_tools_from_wrappers(
        ctx, wrappers, wrap_tool=lambda t: trace_langchain_tool(t, ctx)
    )
    if getattr(getattr(ctx, "device", None), "mobile_platform", None) == "ios":
        tools = [
            t
            for t in tools
            if t.name not in {"run_adb_command", "manage_task", "analyze_task_output"}
        ]
    return tools


def get_tool_by_name(name: str, tools: list[BaseTool]) -> BaseTool | None:
    """Get a tool by name, stripping any prefixes like 'default_api:'."""
    normalized_name = name
    if ":" in normalized_name:
        normalized_name = normalized_name.split(":")[-1]

    for t in tools:
        t_name = t.name
        if ":" in t_name:
            t_name = t_name.split(":")[-1]
        if t_name == normalized_name:
            return t
    return None
