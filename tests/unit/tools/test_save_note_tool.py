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
from unittest.mock import AsyncMock, MagicMock, patch

from artemis.constants import VALIDATOR_MESSAGES_KEY
from artemis.context import ArtemisContext
from artemis.graph.state import State
from artemis.tools.base import ArtemisTool, ToolRegistry
from artemis.tools.scratchpad import (
    SaveNote,
    SaveNoteArgs,
    SaveNoteTool,
    get_save_note_tool,
    get_save_note_tool_pure,
    save_note,
    save_note_wrapper,
)
from langgraph.types import Command
import pytest


@pytest.fixture
def mock_ctx(tmp_path):
    ctx = MagicMock(spec=ArtemisContext)
    ctx.data_engine = MagicMock()
    ctx.data_engine.base_dir = str(tmp_path)
    return ctx


def test_save_note_tool_subclass_and_registry():
    """Verify SaveNoteTool is a subclass of ArtemisTool and properly registered in ToolRegistry."""
    assert issubclass(SaveNoteTool, ArtemisTool)
    assert issubclass(SaveNote, ArtemisTool)
    assert isinstance(save_note, ArtemisTool)
    assert isinstance(save_note, SaveNoteTool)

    assert save_note.name == "save_note"
    assert save_note.category == "memory"
    assert save_note.args_schema == SaveNoteArgs

    # Registry lookup
    reg_tool = ToolRegistry.get("save_note")
    assert reg_tool is not None
    assert isinstance(reg_tool, SaveNoteTool)

    # GenAI FunctionDeclaration export
    declaration = save_note.to_genai_declaration()
    assert declaration.name == "save_note"
    assert "key" in declaration.parameters.properties
    assert "content" in declaration.parameters.properties

    # Wrapper check
    assert save_note_wrapper is not None
    assert save_note_wrapper.tool_fn_getter == get_save_note_tool


@pytest.mark.asyncio
async def test_save_note_direct_execution_success(mock_ctx, tmp_path):
    """Verify direct execution of SaveNoteTool.execute creates note file."""
    result = await save_note.execute(
        ctx=mock_ctx,
        key="test_note",
        content="Hello Artemis memory",
    )
    assert result == "Successfully saved note to test_note.md."

    note_path = Path(tmp_path) / "notes" / "test_note.md"
    assert note_path.exists()
    assert note_path.read_text(encoding="utf-8") == "Hello Artemis memory"


@pytest.mark.asyncio
async def test_save_note_callable_execution(mock_ctx, tmp_path):
    """Verify invoking save_note directly as a callable."""
    result = await save_note(
        ctx=mock_ctx,
        key="callable_note",
        content="Callable invocation test",
    )
    assert result == "Successfully saved note to callable_note.md."

    note_path = Path(tmp_path) / "notes" / "callable_note.md"
    assert note_path.exists()
    assert note_path.read_text(encoding="utf-8") == "Callable invocation test"


@pytest.mark.asyncio
async def test_save_note_with_state_command(mock_ctx, tmp_path):
    """Verify SaveNoteTool.execute with state returns Command updating VALIDATOR_MESSAGES_KEY."""
    mock_state = MagicMock(spec=State)
    mock_state.current_agent = "operator"
    mock_state.asanitize_update = AsyncMock(side_effect=lambda ctx, update, agent: update)

    result = await save_note.execute(
        ctx=mock_ctx,
        key="state_note",
        content="Note with state",
        state=mock_state,
        tool_call_id="call_save_999",
    )

    assert isinstance(result, Command)
    assert VALIDATOR_MESSAGES_KEY in result.update
    tool_messages = result.update[VALIDATOR_MESSAGES_KEY]
    assert len(tool_messages) == 1
    assert tool_messages[0].tool_call_id == "call_save_999"
    assert tool_messages[0].content == "Successfully saved note to state_note.md."
    assert tool_messages[0].status == "success"

    mock_state.asanitize_update.assert_called_once()

    note_path = Path(tmp_path) / "notes" / "state_note.md"
    assert note_path.exists()
    assert note_path.read_text(encoding="utf-8") == "Note with state"


@pytest.mark.asyncio
async def test_save_note_execution_failure(mock_ctx):
    """Verify error handling when saving note fails."""
    with patch(
        "artemis.tools.scratchpad.save_note_content",
        side_effect=PermissionError("Permission denied"),
    ):
        result = await save_note.execute(
            ctx=mock_ctx,
            key="fail_note",
            content="Fail content",
        )
        assert "Failed to save note fail_note.md: Permission denied" in result


@pytest.mark.asyncio
async def test_get_save_note_tool_langchain_ainvoke(mock_ctx, tmp_path):
    """Verify get_save_note_tool exports a LangChain tool that works with ainvoke."""
    lc_tool = get_save_note_tool(mock_ctx)
    assert lc_tool.name == "save_note"

    result = await lc_tool.ainvoke(
        {
            "key": "lc_note",
            "content": "Saved via LangChain BaseTool",
        }
    )
    assert result == "Successfully saved note to lc_note.md."

    note_path = Path(tmp_path) / "notes" / "lc_note.md"
    assert note_path.exists()
    assert note_path.read_text(encoding="utf-8") == "Saved via LangChain BaseTool"


@pytest.mark.asyncio
async def test_get_save_note_tool_pure(mock_ctx, tmp_path):
    """Verify get_save_note_tool_pure returns a pure LangChain tool."""
    pure_tool = get_save_note_tool_pure(mock_ctx)
    assert pure_tool.name == "save_note"

    result = await pure_tool.ainvoke(
        {
            "key": "pure_note",
            "content": "Saved via pure tool",
        }
    )
    assert result == "Successfully saved note to pure_note.md."

    note_path = Path(tmp_path) / "notes" / "pure_note.md"
    assert note_path.exists()
    assert note_path.read_text(encoding="utf-8") == "Saved via pure tool"
