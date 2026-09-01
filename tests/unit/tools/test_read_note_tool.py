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
from artemis.tools.base import ArtemisTool, ToolRegistry
from artemis.tools.scratchpad import (
    ReadNote,
    ReadNoteArgs,
    ReadNoteTool,
    get_read_note_tool,
    get_read_note_tool_pure,
    read_note,
    read_note_wrapper,
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


def test_read_note_tool_subclass_and_registry():
    """Verify ReadNoteTool is a subclass of ArtemisTool and properly registered."""
    assert issubclass(ReadNoteTool, ArtemisTool)
    assert issubclass(ReadNote, ArtemisTool)
    assert isinstance(read_note, ArtemisTool)
    assert isinstance(read_note, ReadNoteTool)

    assert read_note.name == "read_note"
    assert read_note.category == "memory"
    assert read_note.args_schema == ReadNoteArgs

    # Registry lookup
    reg_tool = ToolRegistry.get("read_note")
    assert reg_tool is not None
    assert isinstance(reg_tool, ReadNoteTool)

    # GenAI FunctionDeclaration export
    declaration = read_note.to_genai_declaration()
    assert declaration.name == "read_note"
    assert "key" in declaration.parameters.properties
    assert "start_line" in declaration.parameters.properties
    assert "end_line" in declaration.parameters.properties

    # Wrapper check
    assert read_note_wrapper is not None
    assert read_note_wrapper.tool_fn_getter == get_read_note_tool


@pytest.mark.asyncio
async def test_read_note_direct_execution_success(mock_ctx, tmp_path):
    """Verify direct execution of ReadNoteTool.execute reads saved note."""
    save_note_content(tmp_path, "sample", "line 1\nline 2\nline 3\nline 4\nline 5")

    result = await read_note.execute(ctx=mock_ctx, key="sample")
    assert "Successfully read note 'sample'." in result
    assert "line 1\nline 2\nline 3\nline 4\nline 5" in result


@pytest.mark.asyncio
async def test_read_note_slice(mock_ctx, tmp_path):
    """Verify reading a line slice with start_line and end_line."""
    save_note_content(tmp_path, "sample", "line 1\nline 2\nline 3\nline 4\nline 5")

    result = await read_note.execute(
        ctx=mock_ctx,
        key="sample",
        start_line=2,
        end_line=4,
    )
    assert "Successfully read note 'sample' (lines 2 to 4)." in result
    assert "line 1" not in result
    assert "line 2\nline 3\nline 4" in result
    assert "line 5" not in result


@pytest.mark.asyncio
async def test_read_note_callable_execution(mock_ctx, tmp_path):
    """Verify invoking read_note directly as a callable."""
    save_note_content(tmp_path, "callable_sample", "hello from callable")

    result = await read_note(ctx=mock_ctx, key="callable_sample")
    assert "Successfully read note 'callable_sample'." in result
    assert "hello from callable" in result


@pytest.mark.asyncio
async def test_read_note_with_state_tool_message(mock_ctx, tmp_path):
    """Verify ReadNoteTool.execute with state returns a ToolMessage directly."""
    save_note_content(tmp_path, "state_sample", "state note content")

    mock_state = MagicMock(spec=State)

    result = await read_note.execute(
        ctx=mock_ctx,
        key="state_sample",
        state=mock_state,
        tool_call_id="call_read_999",
    )

    assert isinstance(result, ToolMessage)
    assert result.tool_call_id == "call_read_999"
    assert "Successfully read note 'state_sample'." in result.content
    assert "state note content" in result.content
    assert result.status == "success"


@pytest.mark.asyncio
async def test_read_note_not_found(mock_ctx):
    """Verify error handling when a note is not found."""
    result = await read_note.execute(ctx=mock_ctx, key="nonexistent")
    assert "Note 'nonexistent' not found" in result


@pytest.mark.asyncio
async def test_read_note_other_error(mock_ctx):
    """Verify error handling for unexpected exceptions."""
    with patch(
        "artemis.tools.scratchpad.read_note_content",
        side_effect=ValueError("Corrupted data"),
    ):
        result = await read_note.execute(ctx=mock_ctx, key="bad_note")
        assert "Failed to read note 'bad_note': Corrupted data" in result


@pytest.mark.asyncio
async def test_get_read_note_tool_langchain_ainvoke(mock_ctx, tmp_path):
    """Verify get_read_note_tool exports a LangChain tool that works with ainvoke."""
    save_note_content(tmp_path, "lc_sample", "langchain content")

    lc_tool = get_read_note_tool(mock_ctx)
    assert lc_tool.name == "read_note"

    result = await lc_tool.ainvoke({"key": "lc_sample"})
    assert "Successfully read note 'lc_sample'." in result
    assert "langchain content" in result


@pytest.mark.asyncio
async def test_get_read_note_tool_pure(mock_ctx, tmp_path):
    """Verify get_read_note_tool_pure returns a pure LangChain tool."""
    save_note_content(tmp_path, "pure_sample", "pure content")

    pure_tool = get_read_note_tool_pure(mock_ctx)
    assert pure_tool.name == "read_note"

    result = await pure_tool.ainvoke({"key": "pure_sample"})
    assert "Successfully read note 'pure_sample'." in result
    assert "pure content" in result
