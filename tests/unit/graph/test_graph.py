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

from unittest.mock import AsyncMock, MagicMock, patch

from artemis.context import ArtemisContext
from artemis.graph.graph import wrap_note_tool, wrap_update_note_tool
import pytest


@pytest.mark.asyncio
async def test_wrap_update_note_tool_valid_status_change(tmp_path):
    base_dir = tmp_path
    notes_dir = base_dir / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    task_plan_path = notes_dir / "task_plan.md"

    original_plan = """# Test Plan
- [ ] Milestone 1
  - [ ] Substep 1.1
- [ ] Milestone 2
"""
    task_plan_path.write_text(original_plan, encoding="utf-8")

    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = MagicMock()
    mock_ctx.data_engine.base_dir = str(base_dir)
    mock_ctx.execution_setup = None

    mock_original_tool = MagicMock()
    mock_original_tool.name = "update_note"
    mock_original_tool.description = "Update a note"
    mock_original_tool.ainvoke = AsyncMock(return_value="Success")

    async def fake_invoke_tool(tool, args, tool_call_id, state, record_trace=None):
        content = task_plan_path.read_text(encoding="utf-8")
        updated = content.replace(args["target"], args["replacement"])
        task_plan_path.write_text(updated, encoding="utf-8")
        return "Success"

    target = "- [ ] Milestone 1"
    replacement = "- [x] Milestone 1"

    with patch(
        "artemis.graph.graph.invoke_tool_with_injection",
        side_effect=fake_invoke_tool,
    ):
        # Instantiate inside patch context to capture the mocked import locally
        wrapped_tool = wrap_update_note_tool(mock_ctx, mock_original_tool)
        result = await wrapped_tool.ainvoke(
            {
                "key": "task_plan",
                "target": target,
                "replacement": replacement,
                "tool_call_id": "test_call_id",
            }
        )

        assert result == "Success"
        updated_content = task_plan_path.read_text(encoding="utf-8")
        assert "- [x] Milestone 1" in updated_content
        assert "- [ ] Milestone 2" in updated_content


@pytest.mark.asyncio
async def test_wrap_update_note_tool_invalid_milestone_text_change(tmp_path):
    base_dir = tmp_path
    notes_dir = base_dir / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    task_plan_path = notes_dir / "task_plan.md"

    original_plan = """# Test Plan
- [ ] Milestone 1
  - [ ] Substep 1.1
- [ ] Milestone 2
"""
    task_plan_path.write_text(original_plan, encoding="utf-8")

    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = MagicMock()
    mock_ctx.data_engine.base_dir = str(base_dir)
    mock_ctx.execution_setup = None

    mock_original_tool = MagicMock()
    mock_original_tool.name = "update_note"
    mock_original_tool.ainvoke = AsyncMock(return_value="Success")

    async def fake_invoke_tool(tool, args, tool_call_id, state, record_trace=None):
        content = task_plan_path.read_text(encoding="utf-8")
        updated = content.replace(args["target"], args["replacement"])
        task_plan_path.write_text(updated, encoding="utf-8")
        return "Success"

    target = "- [ ] Milestone 1"
    replacement = "- [ ] Milestone Renamed"

    mock_validation = AsyncMock(return_value={"status": "failed", "feedback": "rejected"})
    with (
        patch(
            "artemis.graph.graph.invoke_tool_with_injection",
            side_effect=fake_invoke_tool,
        ),
        patch(
            "artemis.agents.planner.planner.run_async_planner_validation",
            mock_validation,
        ),
    ):
        wrapped_tool = wrap_update_note_tool(mock_ctx, mock_original_tool)
        result = await wrapped_tool.ainvoke(
            {
                "key": "task_plan",
                "target": target,
                "replacement": replacement,
                "tool_call_id": "test_call_id",
            }
        )

        assert result == "Success"
        assert mock_ctx.planner_task is not None


@pytest.mark.asyncio
async def test_wrap_update_note_tool_allow_subgoal_changes(tmp_path):
    base_dir = tmp_path
    notes_dir = base_dir / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    task_plan_path = notes_dir / "task_plan.md"

    original_plan = """# Test Plan
- [ ] Milestone 1
  - [ ] Substep 1.1
- [ ] Milestone 2
"""
    task_plan_path.write_text(original_plan, encoding="utf-8")

    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = MagicMock()
    mock_ctx.data_engine.base_dir = str(base_dir)
    mock_ctx.execution_setup = None

    mock_original_tool = MagicMock()
    mock_original_tool.name = "update_note"
    mock_original_tool.ainvoke = AsyncMock(return_value="Success")

    async def fake_invoke_tool(tool, args, tool_call_id, state, record_trace=None):
        content = task_plan_path.read_text(encoding="utf-8")
        updated = content.replace(args["target"], args["replacement"])
        task_plan_path.write_text(updated, encoding="utf-8")
        return "Success"

    target = "  - [ ] Substep 1.1"
    replacement = "  - [x] Substep 1.1 Completed"

    with patch(
        "artemis.graph.graph.invoke_tool_with_injection",
        side_effect=fake_invoke_tool,
    ):
        wrapped_tool = wrap_update_note_tool(mock_ctx, mock_original_tool)
        result = await wrapped_tool.ainvoke(
            {
                "key": "task_plan",
                "target": target,
                "replacement": replacement,
                "tool_call_id": "test_call_id",
            }
        )

        assert result == "Success"
        updated_content = task_plan_path.read_text(encoding="utf-8")
        assert "Substep 1.1 Completed" in updated_content
        assert "- [ ] Milestone 1" in updated_content


@pytest.mark.asyncio
async def test_wrap_note_tool_invalid_rewrite(tmp_path):
    base_dir = tmp_path
    notes_dir = base_dir / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    task_plan_path = notes_dir / "task_plan.md"

    original_plan = """# Test Plan
- [ ] Milestone 1
  - [ ] Substep 1.1
- [ ] Milestone 2
"""
    task_plan_path.write_text(original_plan, encoding="utf-8")

    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = MagicMock()
    mock_ctx.data_engine.base_dir = str(base_dir)
    mock_ctx.execution_setup = None

    mock_original_tool = MagicMock()
    mock_original_tool.name = "save_note"
    mock_original_tool.ainvoke = AsyncMock(return_value="Success")

    async def fake_invoke_tool(tool, args, tool_call_id, state, record_trace=None):
        task_plan_path.write_text(args["content"], encoding="utf-8")
        return "Success"

    corrupted_plan = """# Test Plan
- [ ] Milestone 1 (Altered)
- [ ] New Milestone Added
"""

    mock_validation = AsyncMock(return_value={"status": "failed", "feedback": "rejected"})
    with (
        patch(
            "artemis.graph.graph.invoke_tool_with_injection",
            side_effect=fake_invoke_tool,
        ),
        patch(
            "artemis.agents.planner.planner.run_async_planner_validation",
            mock_validation,
        ),
    ):
        wrapped_tool = wrap_note_tool(mock_ctx, mock_original_tool)
        result = await wrapped_tool.ainvoke(
            {
                "key": "task_plan",
                "content": corrupted_plan,
                "tool_call_id": "test_call_id",
            }
        )

        assert result == "Success"
        assert mock_ctx.planner_task is not None


@pytest.mark.asyncio
async def test_wrap_note_tool_valid_lexical_drift(tmp_path):
    from langgraph.types import Command
    from langchain_core.messages import ToolMessage
    from artemis.constants import VALIDATOR_MESSAGES_KEY

    base_dir = tmp_path
    notes_dir = base_dir / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    task_plan_path = notes_dir / "task_plan.md"

    original_plan = """# Test Plan
- [ ] Open the Chrome browser and navigate to youtube.com
"""
    task_plan_path.write_text(original_plan, encoding="utf-8")

    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = MagicMock()
    mock_ctx.data_engine.base_dir = str(base_dir)
    mock_ctx.execution_setup = None

    # Create mock Command result
    tool_msg = ToolMessage(tool_call_id="test_call_id", content="Success outcome", status="success")
    mock_command = Command(update={VALIDATOR_MESSAGES_KEY: [tool_msg]})

    mock_original_tool = MagicMock()
    mock_original_tool.name = "save_note"
    mock_original_tool.ainvoke = AsyncMock(return_value=mock_command)

    async def fake_invoke_tool(tool, args, tool_call_id, state, record_trace=None):
        task_plan_path.write_text(args["content"], encoding="utf-8")
        return mock_command

    # Minor wording change that is semantically valid (similarity ~92%)
    minor_drift_plan = """# Test Plan
- [ ] Open Chrome browser and navigate to youtube.com
"""

    with patch(
        "artemis.graph.graph.invoke_tool_with_injection",
        side_effect=fake_invoke_tool,
    ):
        wrapped_tool = wrap_note_tool(mock_ctx, mock_original_tool)
        result = await wrapped_tool.ainvoke(
            {
                "key": "task_plan",
                "content": minor_drift_plan,
                "tool_call_id": "test_call_id",
            }
        )

        assert isinstance(result, Command)
        assert not hasattr(mock_ctx, "planner_task") or mock_ctx.planner_task is None

        # Verify that the drift plan is actually saved
        saved_content = task_plan_path.read_text(encoding="utf-8")
        assert "Open Chrome browser" in saved_content
