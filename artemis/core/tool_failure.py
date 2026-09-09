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

"""Typed failures for text-returning helper tools.

Tools return ``ToolFailure`` on failure; callers use ``is_tool_failure`` to
check the result. The str subclass keeps results compatible with text consumers
without treating words such as "Error" in tool output as a failure signal.

Pydantic converts str subclasses to plain strings in ``ToolMessage.content``
and ``ToolExecutionResult.text_summary``. Check the raw result before wrapping
it and pass ``status="error"`` to preserve the failure state.
"""

from typing import Any

from langchain_core.messages import ToolMessage


class ToolFailure(str):
    """A helper tool's failure report, carried as the text the agent reads."""

    __slots__ = ()


def is_tool_failure(result: Any) -> bool:
    """Whether a tool result reports failure.

    True for a ``ToolMessage`` whose ``status`` is ``"error"``, for a
    :class:`ToolFailure` text, and for a content-block list whose text is a
    :class:`ToolFailure`. Plain strings are never failures, whatever they say.
    """
    if isinstance(result, ToolMessage):
        if result.status == "error":
            return True
        result = result.content
    if isinstance(result, ToolFailure):
        return True
    if isinstance(result, list):
        for block in result:
            if isinstance(block, ToolFailure):
                return True
            if isinstance(block, dict) and isinstance(block.get("text"), ToolFailure):
                return True
    return False
