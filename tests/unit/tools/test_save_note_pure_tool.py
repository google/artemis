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
from artemis.tools.base import ArtemisTool, ToolRegistry
from artemis.tools.scratchpad import (
    SaveNoteArgs,
    SaveNotePure,
    SaveNotePureTool,
    get_save_note_tool_pure,
    save_note_pure,
)
import pytest


@pytest.fixture
def mock_ctx(tmp_path):
    ctx = MagicMock(spec=ArtemisContext)
    ctx.data_engine = MagicMock()
    ctx.data_engine.base_dir = str(tmp_path)
    return ctx


def test_save_note_pure_tool_subclass_and_registry():
    """Verify SaveNotePureTool is a subclass of ArtemisTool and properly registered."""
    assert issubclass(SaveNotePureTool, ArtemisTool)
    assert issubclass(SaveNotePure, ArtemisTool)
    assert isinstance(save_note_pure, ArtemisTool)
    assert isinstance(save_note_pure, SaveNotePureTool)

    assert save_note_pure.name == "save_note_pure"
    assert save_note_pure.category == "memory"
    assert save_note_pure.args_schema == SaveNoteArgs

    # Registry lookup
    reg_tool = ToolRegistry.get("save_note_pure")
    assert reg_tool is not None
    assert isinstance(reg_tool, SaveNotePureTool)

    # GenAI FunctionDeclaration export
    declaration = save_note_pure.to_genai_declaration()
    assert declaration.name == "save_note_pure"
    assert "key" in declaration.parameters.properties
    assert "content" in declaration.parameters.properties


@pytest.mark.asyncio
async def test_save_note_pure_direct_execution_success(mock_ctx, tmp_path):
    """Verify direct execution of SaveNotePureTool.execute creates note file."""
    result = await save_note_pure.execute(
        ctx=mock_ctx,
        key="test_pure_save",
        content="Pure saved content",
    )
    assert result == "Successfully saved note to test_pure_save.md."

    note_path = Path(tmp_path) / "notes" / "test_pure_save.md"
    assert note_path.exists()
    assert note_path.read_text(encoding="utf-8") == "Pure saved content"


@pytest.mark.asyncio
async def test_save_note_pure_callable_execution(mock_ctx, tmp_path):
    """Verify invoking save_note_pure directly as a callable."""
    result = await save_note_pure(
        ctx=mock_ctx,
        key="callable_pure_save",
        content="Callable pure content",
    )
    assert result == "Successfully saved note to callable_pure_save.md."

    note_path = Path(tmp_path) / "notes" / "callable_pure_save.md"
    assert note_path.exists()
    assert note_path.read_text(encoding="utf-8") == "Callable pure content"


@pytest.mark.asyncio
async def test_save_note_pure_execution_failure(mock_ctx):
    """Verify error handling when save_note_content fails."""
    with patch(
        "artemis.tools.scratchpad.save_note_content",
        side_effect=PermissionError("Permission denied"),
    ):
        result = await save_note_pure.execute(
            ctx=mock_ctx,
            key="fail_save",
            content="Some content",
        )
        assert "Failed to save note fail_save.md: Permission denied" in result


@pytest.mark.asyncio
async def test_get_save_note_tool_pure_langchain_ainvoke(mock_ctx, tmp_path):
    """Verify get_save_note_tool_pure exports a LangChain tool named 'save_note'."""
    pure_tool = get_save_note_tool_pure(mock_ctx)
    assert pure_tool.name == "save_note"

    result = await pure_tool.ainvoke(
        {
            "key": "lc_pure_save",
            "content": "Saved via LangChain pure tool",
        }
    )
    assert result == "Successfully saved note to lc_pure_save.md."

    note_path = Path(tmp_path) / "notes" / "lc_pure_save.md"
    assert note_path.exists()
    assert note_path.read_text(encoding="utf-8") == "Saved via LangChain pure tool"
