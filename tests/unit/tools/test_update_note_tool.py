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
from artemis.graph.state import State
from artemis.tools.base import ArtemisTool, ToolRegistry
from artemis.tools.scratchpad import (
    UpdateNote,
    UpdateNoteArgs,
    UpdateNoteTool,
    get_update_note_tool,
    get_update_note_tool_pure,
    update_note,
    update_note_wrapper,
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


def test_update_note_tool_subclass_and_registry():
    """Verify UpdateNoteTool is a subclass of ArtemisTool and properly registered."""
    assert issubclass(UpdateNoteTool, ArtemisTool)
    assert issubclass(UpdateNote, ArtemisTool)
    assert isinstance(update_note, ArtemisTool)
    assert isinstance(update_note, UpdateNoteTool)

    assert update_note.name == "update_note"
    assert update_note.category == "memory"
    assert update_note.args_schema == UpdateNoteArgs

    # Registry lookup
    reg_tool = ToolRegistry.get("update_note")
    assert reg_tool is not None
    assert isinstance(reg_tool, UpdateNoteTool)

    # GenAI FunctionDeclaration export
    declaration = update_note.to_genai_declaration()
    assert declaration.name == "update_note"
    assert "key" in declaration.parameters.properties
    assert "target" in declaration.parameters.properties
    assert "replacement" in declaration.parameters.properties

    # Wrapper check
    assert update_note_wrapper is not None
    assert update_note_wrapper.tool_fn_getter == get_update_note_tool


@pytest.mark.asyncio
async def test_update_note_direct_execution_success(mock_ctx, tmp_path):
    """Verify direct execution of UpdateNoteTool.execute updates existing note."""
    save_note_content(tmp_path, "plan", "- [ ] Step 1\n- [ ] Step 2")

    result = await update_note.execute(
        ctx=mock_ctx,
        key="plan",
        target="- [ ] Step 1",
        replacement="- [x] Step 1",
    )
    assert result == "Successfully updated note 'plan'."

    note_path = Path(tmp_path) / "notes" / "plan.md"
    assert note_path.read_text(encoding="utf-8") == "- [x] Step 1\n- [ ] Step 2"


@pytest.mark.asyncio
async def test_update_note_with_warning(mock_ctx, tmp_path):
    """Verify update note handles warning from fuzzy/relaxed matching."""
    save_note_content(tmp_path, "fuzzy_plan", "- [ ] Step 1")

    with patch(
        "artemis.tools.scratchpad.update_note_content",
        return_value="Fuzzy match applied",
    ):
        result = await update_note.execute(
            ctx=mock_ctx,
            key="fuzzy_plan",
            target="Step 1",
            replacement="Step 1 Done",
        )
        assert "Successfully updated note 'fuzzy_plan'." in result
        assert "WARNING: Fuzzy match applied" in result


@pytest.mark.asyncio
async def test_update_note_callable_execution(mock_ctx, tmp_path):
    """Verify invoking update_note directly as a callable."""
    save_note_content(tmp_path, "callable_plan", "- [ ] Task 1\n- [ ] Task 2")

    result = await update_note(
        ctx=mock_ctx,
        key="callable_plan",
        target="- [ ] Task 2",
        replacement="- [x] Task 2",
    )
    assert result == "Successfully updated note 'callable_plan'."

    note_path = Path(tmp_path) / "notes" / "callable_plan.md"
    assert note_path.read_text(encoding="utf-8") == "- [ ] Task 1\n- [x] Task 2"


@pytest.mark.asyncio
async def test_update_note_with_state_tool_message(mock_ctx, tmp_path):
    """Verify UpdateNoteTool.execute with state returns a ToolMessage directly."""
    save_note_content(tmp_path, "state_plan", "- [ ] Step 1")

    mock_state = MagicMock(spec=State)

    result = await update_note.execute(
        ctx=mock_ctx,
        key="state_plan",
        target="- [ ] Step 1",
        replacement="- [x] Step 1",
        state=mock_state,
        tool_call_id="call_update_999",
    )

    assert isinstance(result, ToolMessage)
    assert result.tool_call_id == "call_update_999"
    assert result.content == "Successfully updated note 'state_plan'."
    assert result.status == "success"

    note_path = Path(tmp_path) / "notes" / "state_plan.md"
    assert note_path.read_text(encoding="utf-8") == "- [x] Step 1"


@pytest.mark.asyncio
async def test_update_note_execution_failure(mock_ctx):
    """Verify error handling when updating a note fails."""
    result = await update_note.execute(
        ctx=mock_ctx,
        key="non_existent_note",
        target="old",
        replacement="new",
    )
    assert "Failed to update note 'non_existent_note':" in result


@pytest.mark.asyncio
async def test_get_update_note_tool_langchain_ainvoke(mock_ctx, tmp_path):
    """Verify get_update_note_tool exports a LangChain tool that works with ainvoke."""
    save_note_content(tmp_path, "lc_plan", "alpha beta gamma")

    lc_tool = get_update_note_tool(mock_ctx)
    assert lc_tool.name == "update_note"

    result = await lc_tool.ainvoke(
        {
            "key": "lc_plan",
            "target": "beta",
            "replacement": "delta",
        }
    )
    assert result == "Successfully updated note 'lc_plan'."

    note_path = Path(tmp_path) / "notes" / "lc_plan.md"
    assert note_path.read_text(encoding="utf-8") == "alpha delta gamma"


@pytest.mark.asyncio
async def test_get_update_note_tool_pure(mock_ctx, tmp_path):
    """Verify get_update_note_tool_pure returns a pure LangChain tool."""
    save_note_content(tmp_path, "pure_plan", "hello world")

    pure_tool = get_update_note_tool_pure(mock_ctx)
    assert pure_tool.name == "update_note"

    result = await pure_tool.ainvoke(
        {
            "key": "pure_plan",
            "target": "world",
            "replacement": "Artemis",
        }
    )
    assert result == "Successfully updated note 'pure_plan'."

    note_path = Path(tmp_path) / "notes" / "pure_plan.md"
    assert note_path.read_text(encoding="utf-8") == "hello Artemis"
