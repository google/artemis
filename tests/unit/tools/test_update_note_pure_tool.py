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
    UpdateNoteArgs,
    UpdateNotePure,
    UpdateNotePureTool,
    get_update_note_tool_pure,
    update_note_pure,
)
from artemis.utils.notes import save_note_content
import pytest


@pytest.fixture
def mock_ctx(tmp_path):
    ctx = MagicMock(spec=ArtemisContext)
    ctx.data_engine = MagicMock()
    ctx.data_engine.base_dir = str(tmp_path)
    return ctx


def test_update_note_pure_tool_subclass_and_registry():
    """Verify UpdateNotePureTool is a subclass of ArtemisTool and properly registered."""
    assert issubclass(UpdateNotePureTool, ArtemisTool)
    assert issubclass(UpdateNotePure, ArtemisTool)
    assert isinstance(update_note_pure, ArtemisTool)
    assert isinstance(update_note_pure, UpdateNotePureTool)

    assert update_note_pure.name == "update_note_pure"
    assert update_note_pure.category == "memory"
    assert update_note_pure.args_schema == UpdateNoteArgs

    # Registry lookup
    reg_tool = ToolRegistry.get("update_note_pure")
    assert reg_tool is not None
    assert isinstance(reg_tool, UpdateNotePureTool)

    # GenAI FunctionDeclaration export
    declaration = update_note_pure.to_genai_declaration()
    assert declaration.name == "update_note_pure"
    assert "key" in declaration.parameters.properties
    assert "target" in declaration.parameters.properties
    assert "replacement" in declaration.parameters.properties


@pytest.mark.asyncio
async def test_update_note_pure_direct_execution_success(mock_ctx, tmp_path):
    """Verify direct execution of UpdateNotePureTool.execute updates existing note."""
    save_note_content(tmp_path, "plan", "Step 1: Pending\nStep 2: Done")

    result = await update_note_pure.execute(
        ctx=mock_ctx,
        key="plan",
        target="Step 1: Pending",
        replacement="Step 1: Completed",
    )
    assert result == "Successfully updated note 'plan'."

    note_path = Path(tmp_path) / "notes" / "plan.md"
    assert note_path.read_text(encoding="utf-8") == "Step 1: Completed\nStep 2: Done"


@pytest.mark.asyncio
async def test_update_note_pure_with_warning(mock_ctx, tmp_path):
    """Verify update note pure handles warning from fuzzy matching."""
    save_note_content(tmp_path, "fuzzy_note", "Target String")

    with patch(
        "artemis.tools.scratchpad.update_note_content",
        return_value="Fuzzy match applied",
    ):
        result = await update_note_pure.execute(
            ctx=mock_ctx,
            key="fuzzy_note",
            target="target string",
            replacement="New String",
        )
        assert "Successfully updated note 'fuzzy_note'." in result
        assert "WARNING: Fuzzy match applied" in result


@pytest.mark.asyncio
async def test_update_note_pure_callable_execution(mock_ctx, tmp_path):
    """Verify invoking update_note_pure directly as a callable."""
    save_note_content(tmp_path, "callable_plan", "alpha beta")

    result = await update_note_pure(
        ctx=mock_ctx,
        key="callable_plan",
        target="beta",
        replacement="gamma",
    )
    assert result == "Successfully updated note 'callable_plan'."

    note_path = Path(tmp_path) / "notes" / "callable_plan.md"
    assert note_path.read_text(encoding="utf-8") == "alpha gamma"


@pytest.mark.asyncio
async def test_update_note_pure_execution_failure(mock_ctx):
    """Verify error handling when note update fails."""
    result = await update_note_pure.execute(
        ctx=mock_ctx,
        key="missing_note",
        target="old",
        replacement="new",
    )
    assert "Failed to update note 'missing_note':" in result


@pytest.mark.asyncio
async def test_get_update_note_tool_pure_langchain_ainvoke(mock_ctx, tmp_path):
    """Verify get_update_note_tool_pure exports a LangChain tool named 'update_note'."""
    save_note_content(tmp_path, "export_plan", "initial value")

    pure_tool = get_update_note_tool_pure(mock_ctx)
    assert pure_tool.name == "update_note"

    result = await pure_tool.ainvoke(
        {
            "key": "export_plan",
            "target": "initial",
            "replacement": "updated",
        }
    )
    assert result == "Successfully updated note 'export_plan'."

    note_path = Path(tmp_path) / "notes" / "export_plan.md"
    assert note_path.read_text(encoding="utf-8") == "updated value"
