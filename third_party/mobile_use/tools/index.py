# Copyright 2025-2026 Minitap, Inc.
# Modifications Copyright 2026 Google LLC
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
#
# Originally from mobile-use (https://github.com/minitap-ai/mobile-use).
# See third_party/mobile_use/METADATA for the upstream source and local modifications.

"""Instantiate LangChain tools from tool wrappers."""

from collections.abc import Callable

from langchain_core.tools import BaseTool

from artemis.context import ArtemisContext
from third_party.mobile_use.tools.tool_wrapper import CompositeToolWrapper, ToolWrapper


def get_tools_from_wrappers(
    ctx: "ArtemisContext",
    wrappers: list[ToolWrapper],
    wrap_tool: Callable[[BaseTool], BaseTool] | None = None,
) -> list[BaseTool]:
    """Instantiate and wrap LangChain tools from a list of ToolWrappers."""
    tools: list[BaseTool] = []
    for wrapper in wrappers:
        if wrapper.is_available_fn is not None and not wrapper.is_available_fn(ctx):
            continue
        if isinstance(wrapper, CompositeToolWrapper):
            new_tools = wrapper.composite_tools_fn_getter(ctx)
        else:
            new_tools = [wrapper.tool_fn_getter(ctx)]
        tools.extend(wrap_tool(t) if wrap_tool else t for t in new_tools)
    return tools
