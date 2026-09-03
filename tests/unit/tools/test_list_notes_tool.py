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
from artemis.graph.state import State
from artemis.tools.base import ArtemisTool
from artemis.tools.scratchpad import (
    ListNotes,
    ListNotesArgs,
    ListNotesTool,
    get_list_notes_tool,
    get_list_notes_tool_pure,
    list_notes,
    list_notes_wrapper,
)
from artemis.utils.notes import save_note_content
from langchain_core.messages import ToolMessage
import pytest


@pytest.fixture
def mock_ctx(tmp_path):
    ctx = MagicMock(spec=ArtemisContext)
    ctx.data_engine = MagicMock()
    ctx.data_engine.base_dir = str(tmp_path)
    return ctx


def test_list_notes_tool_subclass():
    """Verify ListNotesTool is a subclass of ArtemisTool."""
    assert issubclass(ListNotesTool, ArtemisTool)
    assert issubclass(ListNotes, ArtemisTool)
    assert isinstance(list_notes, ArtemisTool)
    assert isinstance(list_notes, ListNotesTool)

    assert list_notes.name == "list_notes"
    assert list_notes.category == "memory"
    assert list_notes.args_schema == ListNotesArgs

    # GenAI FunctionDeclaration export
    declaration = list_notes.to_genai_declaration()
    assert declaration.name == "list_notes"

    # Wrapper check
    assert list_notes_wrapper is not None
    assert list_notes_wrapper.tool_fn_getter == get_list_notes_tool


@pytest.mark.asyncio
async def test_list_notes_direct_execution_empty(mock_ctx):
    """Verify listing notes when no notes are stored."""
    result = await list_notes.execute(ctx=mock_ctx)
    assert "No notes saved yet." in result


@pytest.mark.asyncio
async def test_list_notes_direct_execution_success(mock_ctx, tmp_path):
    """Verify direct execution of ListNotesTool.execute lists stored notes."""
    save_note_content(tmp_path, "note_alpha", "line 1\nline 2")
    save_note_content(tmp_path, "note_beta", "single line")

    result = await list_notes.execute(ctx=mock_ctx)
    assert "Here are all the notes:" in result
    assert "- note_alpha (2 lines)" in result
    assert "- note_beta (1 lines)" in result


@pytest.mark.asyncio
async def test_list_notes_callable_execution(mock_ctx, tmp_path):
    """Verify invoking list_notes directly as a callable."""
    save_note_content(tmp_path, "note_gamma", "line A\nline B\nline C")

    result = await list_notes(ctx=mock_ctx)
    assert "Here are all the notes:" in result
    assert "- note_gamma (3 lines)" in result


@pytest.mark.asyncio
async def test_list_notes_with_state_tool_message(mock_ctx, tmp_path):
    """Verify ListNotesTool.execute with state returns a ToolMessage directly."""
    save_note_content(tmp_path, "state_note", "content")

    mock_state = MagicMock(spec=State)

    result = await list_notes.execute(
        ctx=mock_ctx,
        state=mock_state,
        tool_call_id="call_list_999",
    )

    assert isinstance(result, ToolMessage)
    assert result.tool_call_id == "call_list_999"
    assert "Here are all the notes:" in result.content
    assert "- state_note (1 lines)" in result.content
    assert result.status == "success"


@pytest.mark.asyncio
async def test_list_notes_execution_failure(mock_ctx):
    """Verify error handling when list_notes fails."""
    with patch(
        "artemis.tools.scratchpad.list_notes_info",
        side_effect=RuntimeError("Disk failure"),
    ):
        result = await list_notes.execute(ctx=mock_ctx)
        assert "Failed to list notes: Disk failure" in result


@pytest.mark.asyncio
async def test_get_list_notes_tool_langchain_ainvoke(mock_ctx, tmp_path):
    """Verify get_list_notes_tool exports a LangChain tool that works with ainvoke."""
    save_note_content(tmp_path, "lc_note", "hello")

    lc_tool = get_list_notes_tool(mock_ctx)
    assert lc_tool.name == "list_notes"

    result = await lc_tool.ainvoke({})
    assert "Here are all the notes:" in result
    assert "- lc_note (1 lines)" in result


@pytest.mark.asyncio
async def test_get_list_notes_tool_pure(mock_ctx, tmp_path):
    """Verify get_list_notes_tool_pure returns a pure LangChain tool."""
    save_note_content(tmp_path, "pure_note", "world")

    pure_tool = get_list_notes_tool_pure(mock_ctx)
    assert pure_tool.name == "list_notes"

    result = await pure_tool.ainvoke({})
    assert "Here are all the notes:" in result
    assert "- pure_note (1 lines)" in result
