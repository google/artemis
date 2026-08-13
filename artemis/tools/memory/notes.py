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

"""Memory and Scratchpad Note Management tools."""

from typing import Any

from pydantic import BaseModel, Field

from artemis.tools.base import artemis_tool

_IN_MEMORY_NOTES: dict[str, str] = {}


class ReadNoteArgs(BaseModel):
    """Arguments schema for reading a note."""

    title: str = Field(..., description="Note title to read.")


class SaveNoteArgs(BaseModel):
    """Arguments schema for saving a note."""

    title: str = Field(..., description="Note title to create/save.")
    content: str = Field(..., description="Content text of the note.")


@artemis_tool(
    name="read_note",
    description="[MEMORY] Reads content of an existing scratchpad note.",
    args_schema=ReadNoteArgs,
    category="memory",
)
async def read_note(
    title: str,
    ctx: Any = None,  # pylint: disable=unused-argument
) -> str:
    """Reads content of an existing scratchpad note."""
    return _IN_MEMORY_NOTES.get(title, f"Note '{title}' not found.")


@artemis_tool(
    name="save_note",
    description="[MEMORY] Saves content into the scratchpad notes system.",
    args_schema=SaveNoteArgs,
    category="memory",
)
async def save_note(
    title: str,
    content: str,
    ctx: Any = None,  # pylint: disable=unused-argument
) -> str:
    """Saves content into the scratchpad notes system."""
    _IN_MEMORY_NOTES[title] = content
    return f"Note '{title}' saved successfully."
