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
    AppendNote,
    AppendNoteArgs,
    AppendNoteTool,
    get_append_note_tool,
    get_append_note_tool_pure,
    append_note,
    append_note_wrapper,
)
from langgraph.types import Command
import pytest


@pytest.fixture
def mock_ctx(tmp_path):
    ctx = MagicMock(spec=ArtemisContext)
    ctx.data_engine = MagicMock()
    ctx.data_engine.base_dir = str(tmp_path)
    return ctx


def test_append_note_tool_subclass_and_registry():
    """Verify AppendNoteTool is a subclass of ArtemisTool and properly registered."""
    assert issubclass(AppendNoteTool, ArtemisTool)
    assert issubclass(AppendNote, ArtemisTool)
    assert isinstance(append_note, ArtemisTool)
    assert isinstance(append_note, AppendNoteTool)

    assert append_note.name == "append_note"
    assert append_note.category == "memory"
    assert append_note.args_schema == AppendNoteArgs

    # Registry lookup
    reg_tool = ToolRegistry.get("append_note")
    assert reg_tool is not None
    assert isinstance(reg_tool, AppendNoteTool)

    # GenAI FunctionDeclaration export
    declaration = append_note.to_genai_declaration()
    assert declaration.name == "append_note"
    assert "key" in declaration.parameters.properties
    assert "content" in declaration.parameters.properties

    # Wrapper check
    assert append_note_wrapper is not None
    assert append_note_wrapper.tool_fn_getter == get_append_note_tool


@pytest.mark.asyncio
async def test_append_note_direct_execution_success(mock_ctx, tmp_path):
    """Verify direct execution of AppendNoteTool.execute appends to note file."""
    # First append creates the note
    result1 = await append_note.execute(
        ctx=mock_ctx,
        key="test_append",
        content="First line",
    )
    assert result1 == "Successfully appended note to test_append.md."

    note_path = Path(tmp_path) / "notes" / "test_append.md"
    assert note_path.exists()
    assert note_path.read_text(encoding="utf-8") == "First line\n"

    # Second append appends with newline
    result2 = await append_note.execute(
        ctx=mock_ctx,
        key="test_append",
        content="Second line",
    )
    assert result2 == "Successfully appended note to test_append.md."
    assert note_path.read_text(encoding="utf-8") == "First line\nSecond line\n"


@pytest.mark.asyncio
async def test_append_note_callable_execution(mock_ctx, tmp_path):
    """Verify invoking append_note directly as a callable."""
    result = await append_note(
        ctx=mock_ctx,
        key="callable_append",
        content="Callable append test",
    )
    assert result == "Successfully appended note to callable_append.md."

    note_path = Path(tmp_path) / "notes" / "callable_append.md"
    assert note_path.exists()
    assert note_path.read_text(encoding="utf-8") == "Callable append test\n"


@pytest.mark.asyncio
async def test_append_note_with_state_command(mock_ctx, tmp_path):
    """Verify AppendNoteTool.execute with state returns Command updating VALIDATOR_MESSAGES_KEY."""
    mock_state = MagicMock(spec=State)
    mock_state.current_agent = "operator"
    mock_state.asanitize_update = AsyncMock(side_effect=lambda ctx, update, agent: update)

    result = await append_note.execute(
        ctx=mock_ctx,
        key="state_append_note",
        content="Appended with state",
        state=mock_state,
        tool_call_id="call_append_999",
    )

    assert isinstance(result, Command)
    assert VALIDATOR_MESSAGES_KEY in result.update
    tool_messages = result.update[VALIDATOR_MESSAGES_KEY]
    assert len(tool_messages) == 1
    assert tool_messages[0].tool_call_id == "call_append_999"
    assert tool_messages[0].content == "Successfully appended note to state_append_note.md."
    assert tool_messages[0].status == "success"

    mock_state.asanitize_update.assert_called_once()

    note_path = Path(tmp_path) / "notes" / "state_append_note.md"
    assert note_path.exists()
    assert note_path.read_text(encoding="utf-8") == "Appended with state\n"


@pytest.mark.asyncio
async def test_append_note_execution_failure(mock_ctx):
    """Verify error handling when appending note fails."""
    with patch(
        "artemis.tools.scratchpad.append_note_content",
        side_effect=PermissionError("Permission denied"),
    ):
        result = await append_note.execute(
            ctx=mock_ctx,
            key="fail_append_note",
            content="Fail content",
        )
        assert "Failed to append note fail_append_note.md: Permission denied" in result


@pytest.mark.asyncio
async def test_get_append_note_tool_langchain_ainvoke(mock_ctx, tmp_path):
    """Verify get_append_note_tool exports a LangChain tool that works with ainvoke."""
    lc_tool = get_append_note_tool(mock_ctx)
    assert lc_tool.name == "append_note"

    result = await lc_tool.ainvoke(
        {
            "key": "lc_append_note",
            "content": "Appended via LangChain BaseTool",
        }
    )
    assert result == "Successfully appended note to lc_append_note.md."

    note_path = Path(tmp_path) / "notes" / "lc_append_note.md"
    assert note_path.exists()
    assert note_path.read_text(encoding="utf-8") == "Appended via LangChain BaseTool\n"


@pytest.mark.asyncio
async def test_get_append_note_tool_pure(mock_ctx, tmp_path):
    """Verify get_append_note_tool_pure returns a pure LangChain tool."""
    pure_tool = get_append_note_tool_pure(mock_ctx)
    assert pure_tool.name == "append_note"

    result = await pure_tool.ainvoke(
        {
            "key": "pure_append_note",
            "content": "Appended via pure tool",
        }
    )
    assert result == "Successfully appended note to pure_append_note.md."

    note_path = Path(tmp_path) / "notes" / "pure_append_note.md"
    assert note_path.exists()
    assert note_path.read_text(encoding="utf-8") == "Appended via pure tool\n"
