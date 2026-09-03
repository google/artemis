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

from unittest.mock import MagicMock, patch

from artemis.context import ArtemisContext
from artemis.tools.base import ArtemisTool
from artemis.tools.scratchpad import (
    ListNotesArgs,
    ListNotesPure,
    ListNotesPureTool,
    get_list_notes_tool_pure,
    list_notes_pure,
)
from artemis.utils.notes import save_note_content
import pytest


@pytest.fixture
def mock_ctx(tmp_path):
    ctx = MagicMock(spec=ArtemisContext)
    ctx.data_engine = MagicMock()
    ctx.data_engine.base_dir = str(tmp_path)
    return ctx


def test_list_notes_pure_tool_subclass():
    """Verify ListNotesPureTool is a subclass of ArtemisTool."""
    assert issubclass(ListNotesPureTool, ArtemisTool)
    assert issubclass(ListNotesPure, ArtemisTool)
    assert isinstance(list_notes_pure, ArtemisTool)
    assert isinstance(list_notes_pure, ListNotesPureTool)

    assert list_notes_pure.name == "list_notes_pure"
    assert list_notes_pure.category == "memory"
    assert list_notes_pure.args_schema == ListNotesArgs

    # GenAI FunctionDeclaration export
    declaration = list_notes_pure.to_genai_declaration()
    assert declaration.name == "list_notes_pure"


@pytest.mark.asyncio
async def test_list_notes_pure_direct_execution_empty(mock_ctx):
    """Verify listing notes when no notes are stored."""
    result = await list_notes_pure.execute(ctx=mock_ctx)
    assert "No notes saved yet." in result


@pytest.mark.asyncio
async def test_list_notes_pure_direct_execution_success(mock_ctx, tmp_path):
    """Verify direct execution of ListNotesPureTool.execute lists stored notes."""
    save_note_content(tmp_path, "note_1", "line 1\nline 2")
    save_note_content(tmp_path, "note_2", "single line")

    result = await list_notes_pure.execute(ctx=mock_ctx)
    assert "Here are all the notes:" in result
    assert "- note_1 (2 lines)" in result
    assert "- note_2 (1 lines)" in result


@pytest.mark.asyncio
async def test_list_notes_pure_callable_execution(mock_ctx, tmp_path):
    """Verify invoking list_notes_pure directly as a callable."""
    save_note_content(tmp_path, "callable_note", "line A\nline B")

    result = await list_notes_pure(ctx=mock_ctx)
    assert "Here are all the notes:" in result
    assert "- callable_note (2 lines)" in result


@pytest.mark.asyncio
async def test_list_notes_pure_execution_failure(mock_ctx):
    """Verify error handling when list_notes_pure fails."""
    with patch(
        "artemis.tools.scratchpad.list_notes_info",
        side_effect=RuntimeError("Disk failure"),
    ):
        result = await list_notes_pure.execute(ctx=mock_ctx)
        assert "Failed to list notes: Disk failure" in result


@pytest.mark.asyncio
async def test_get_list_notes_tool_pure_langchain_ainvoke(mock_ctx, tmp_path):
    """Verify get_list_notes_tool_pure exports a LangChain tool named 'list_notes'."""
    save_note_content(tmp_path, "pure_export_note", "content")

    pure_tool = get_list_notes_tool_pure(mock_ctx)
    assert pure_tool.name == "list_notes"

    result = await pure_tool.ainvoke({})
    assert "Here are all the notes:" in result
    assert "- pure_export_note (1 lines)" in result
