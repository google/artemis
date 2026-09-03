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
    ReadNoteArgs,
    ReadNotePure,
    ReadNotePureTool,
    get_read_note_tool_pure,
    read_note_pure,
)
from artemis.utils.notes import save_note_content
import pytest


@pytest.fixture
def mock_ctx(tmp_path):
    ctx = MagicMock(spec=ArtemisContext)
    ctx.data_engine = MagicMock()
    ctx.data_engine.base_dir = str(tmp_path)
    return ctx


def test_read_note_pure_tool_subclass():
    """Verify ReadNotePureTool is a subclass of ArtemisTool."""
    assert issubclass(ReadNotePureTool, ArtemisTool)
    assert issubclass(ReadNotePure, ArtemisTool)
    assert isinstance(read_note_pure, ArtemisTool)
    assert isinstance(read_note_pure, ReadNotePureTool)

    assert read_note_pure.name == "read_note_pure"
    assert read_note_pure.category == "memory"
    assert read_note_pure.args_schema == ReadNoteArgs

    # GenAI FunctionDeclaration export
    declaration = read_note_pure.to_genai_declaration()
    assert declaration.name == "read_note_pure"
    assert "key" in declaration.parameters.properties
    assert "start_line" in declaration.parameters.properties
    assert "end_line" in declaration.parameters.properties


@pytest.mark.asyncio
async def test_read_note_pure_direct_execution_success(mock_ctx, tmp_path):
    """Verify direct execution of ReadNotePureTool.execute reads saved note."""
    save_note_content(tmp_path, "sample_pure", "line 1\nline 2\nline 3")

    result = await read_note_pure.execute(ctx=mock_ctx, key="sample_pure")
    assert "Successfully read note 'sample_pure'." in result
    assert "line 1\nline 2\nline 3" in result


@pytest.mark.asyncio
async def test_read_note_pure_slice(mock_ctx, tmp_path):
    """Verify reading a line slice with start_line and end_line via ReadNotePureTool."""
    save_note_content(tmp_path, "slice_pure", "line 1\nline 2\nline 3\nline 4")

    result = await read_note_pure.execute(
        ctx=mock_ctx,
        key="slice_pure",
        start_line=2,
        end_line=3,
    )
    assert "Successfully read note 'slice_pure' (lines 2 to 3)." in result
    assert "line 1" not in result
    assert "line 2\nline 3" in result
    assert "line 4" not in result


@pytest.mark.asyncio
async def test_read_note_pure_callable_execution(mock_ctx, tmp_path):
    """Verify invoking read_note_pure directly as a callable."""
    save_note_content(tmp_path, "callable_pure", "hello from pure callable")

    result = await read_note_pure(ctx=mock_ctx, key="callable_pure")
    assert "Successfully read note 'callable_pure'." in result
    assert "hello from pure callable" in result


@pytest.mark.asyncio
async def test_read_note_pure_not_found(mock_ctx):
    """Verify error handling when note is missing."""
    result = await read_note_pure.execute(ctx=mock_ctx, key="nonexistent_pure")
    assert "Note 'nonexistent_pure' not found" in result


@pytest.mark.asyncio
async def test_read_note_pure_other_error(mock_ctx):
    """Verify error handling when read_note_content raises an exception."""
    with patch(
        "artemis.tools.scratchpad.read_note_content",
        side_effect=OSError("Disk error"),
    ):
        result = await read_note_pure.execute(ctx=mock_ctx, key="err_note")
        assert "Failed to read note 'err_note': Disk error" in result


@pytest.mark.asyncio
async def test_get_read_note_tool_pure_langchain_ainvoke(mock_ctx, tmp_path):
    """Verify get_read_note_tool_pure exports a LangChain tool named 'read_note'."""
    save_note_content(tmp_path, "pure_export", "exported pure note")

    pure_tool = get_read_note_tool_pure(mock_ctx)
    assert pure_tool.name == "read_note"

    result = await pure_tool.ainvoke({"key": "pure_export"})
    assert "Successfully read note 'pure_export'." in result
    assert "exported pure note" in result
