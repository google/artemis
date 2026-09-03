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

from pathlib import Path
from unittest.mock import MagicMock, patch

from artemis.context import ArtemisContext
from artemis.tools.base import ArtemisTool
from artemis.tools.scratchpad import (
    AppendNoteArgs,
    AppendNotePure,
    AppendNotePureTool,
    append_note_pure,
    get_append_note_tool_pure,
)
import pytest


@pytest.fixture
def mock_ctx(tmp_path):
    ctx = MagicMock(spec=ArtemisContext)
    ctx.data_engine = MagicMock()
    ctx.data_engine.base_dir = str(tmp_path)
    return ctx


def test_append_note_pure_tool_subclass():
    """Verify AppendNotePureTool is a subclass of ArtemisTool."""
    assert issubclass(AppendNotePureTool, ArtemisTool)
    assert issubclass(AppendNotePure, ArtemisTool)
    assert isinstance(append_note_pure, ArtemisTool)
    assert isinstance(append_note_pure, AppendNotePureTool)

    assert append_note_pure.name == "append_note_pure"
    assert append_note_pure.category == "memory"
    assert append_note_pure.args_schema == AppendNoteArgs

    # GenAI FunctionDeclaration export
    declaration = append_note_pure.to_genai_declaration()
    assert declaration.name == "append_note_pure"
    assert "key" in declaration.parameters.properties
    assert "content" in declaration.parameters.properties


@pytest.mark.asyncio
async def test_append_note_pure_direct_execution_success(mock_ctx, tmp_path):
    """Verify direct execution of AppendNotePureTool.execute appends to note file."""
    # First append creates note
    result1 = await append_note_pure.execute(
        ctx=mock_ctx,
        key="pure_append",
        content="First line",
    )
    assert result1 == "Successfully appended note to pure_append.md."

    note_path = Path(tmp_path) / "notes" / "pure_append.md"
    assert note_path.exists()
    assert note_path.read_text(encoding="utf-8") == "First line\n"

    # Second append appends line
    result2 = await append_note_pure.execute(
        ctx=mock_ctx,
        key="pure_append",
        content="Second line",
    )
    assert result2 == "Successfully appended note to pure_append.md."
    assert note_path.read_text(encoding="utf-8") == "First line\nSecond line\n"


@pytest.mark.asyncio
async def test_append_note_pure_callable_execution(mock_ctx, tmp_path):
    """Verify invoking append_note_pure directly as a callable."""
    result = await append_note_pure(
        ctx=mock_ctx,
        key="callable_pure_append",
        content="Pure callable append",
    )
    assert result == "Successfully appended note to callable_pure_append.md."

    note_path = Path(tmp_path) / "notes" / "callable_pure_append.md"
    assert note_path.exists()
    assert note_path.read_text(encoding="utf-8") == "Pure callable append\n"


@pytest.mark.asyncio
async def test_append_note_pure_execution_failure(mock_ctx):
    """Verify error handling when append_note_content fails."""
    with patch(
        "artemis.tools.scratchpad.append_note_content",
        side_effect=PermissionError("Permission denied"),
    ):
        result = await append_note_pure.execute(
            ctx=mock_ctx,
            key="fail_append",
            content="Some content",
        )
        assert "Failed to append note fail_append.md: Permission denied" in result


@pytest.mark.asyncio
async def test_get_append_note_tool_pure_langchain_ainvoke(mock_ctx, tmp_path):
    """Verify get_append_note_tool_pure exports a LangChain tool named 'append_note'."""
    pure_tool = get_append_note_tool_pure(mock_ctx)
    assert pure_tool.name == "append_note"

    result = await pure_tool.ainvoke(
        {
            "key": "lc_pure_append",
            "content": "Saved via pure append tool",
        }
    )
    assert result == "Successfully appended note to lc_pure_append.md."

    note_path = Path(tmp_path) / "notes" / "lc_pure_append.md"
    assert note_path.exists()
    assert note_path.read_text(encoding="utf-8") == "Saved via pure append tool\n"
