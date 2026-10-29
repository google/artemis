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

"""Tool wrappers: a LangChain tool factory and the messages shown for its result."""

from collections.abc import Callable

from langchain_core.tools import BaseTool
from pydantic import BaseModel

from artemis.context import ArtemisContext
from artemis.utils.cython_compat import CyFunctionDetector


class ToolWrapper(BaseModel):
    """Wrapper holding a tool factory and lifecycle callbacks."""

    model_config = {"ignored_types": (CyFunctionDetector,)}
    tool_fn_getter: Callable[[ArtemisContext], BaseTool]
    on_success_fn: Callable[..., str]
    on_failure_fn: Callable[..., str]
    is_available_fn: Callable[[ArtemisContext], bool] | None = None


class CompositeToolWrapper(ToolWrapper):
    """Wrapper holding a composite tool factory and lifecycle callbacks."""

    composite_tools_fn_getter: Callable[[ArtemisContext], list[BaseTool]]


def make_save_note_wrapper(tool_fn_getter: Callable[[ArtemisContext], BaseTool]) -> ToolWrapper:
    """Wrapper for the scratchpad ``save_note`` tool with its outcome messages."""
    return ToolWrapper(
        tool_fn_getter=tool_fn_getter,
        on_success_fn=lambda key: f"Saved note '{key}'.",
        on_failure_fn=lambda key: f"Failed to save note '{key}'.",
    )
