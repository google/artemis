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

from langchain_core.messages import ToolMessage

from artemis.context import ArtemisContext
from artemis.graph.graph import wrap_note_tool, wrap_update_note_tool
import pytest

ORIGINAL_PLAN = """# Test Plan
- [ ] Milestone 1
  - [ ] Substep 1.1
- [ ] Milestone 2
"""


def _make_ctx(base_dir, *, disable_checker=True, disable_planner_validation=False):
    ctx = MagicMock(spec=ArtemisContext)
    ctx.data_engine = MagicMock()
    ctx.data_engine.base_dir = str(base_dir)
    ctx.execution_setup = MagicMock()
    ctx.execution_setup.disable_checker = disable_checker
    ctx.execution_setup.disable_planner_validation = disable_planner_validation
    ctx.planner_task = None
    ctx.last_validated_plan = None
    ctx.pending_checkpoints = []
    ctx.checkpoint_tasks = {}
    ctx.pending_validated_plan = None
    return ctx


def _setup_plan(tmp_path, content=ORIGINAL_PLAN):
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    task_plan_path = notes_dir / "task_plan.md"
    task_plan_path.write_text(content, encoding="utf-8")
    return task_plan_path


def _mock_tool(name):
    tool = MagicMock()
    tool.name = name
    tool.description = f"{name} tool"
    tool.ainvoke = AsyncMock(return_value="Success")
    return tool


def _fake_update_invoke(task_plan_path):
    async def fake_invoke_tool(tool, args, tool_call_id, state, record_trace=None):
        content = task_plan_path.read_text(encoding="utf-8")
        updated = content.replace(args["target"], args["replacement"])
        task_plan_path.write_text(updated, encoding="utf-8")
        return "Success"

    return fake_invoke_tool


def _fake_save_invoke(task_plan_path):
    async def fake_invoke_tool(tool, args, tool_call_id, state, record_trace=None):
        task_plan_path.write_text(args["content"], encoding="utf-8")
        return "Success"

    return fake_invoke_tool


@pytest.mark.asyncio
async def test_wrap_update_note_tool_valid_status_change(tmp_path):
    task_plan_path = _setup_plan(tmp_path)
    ctx = _make_ctx(tmp_path)

    with patch(
        "artemis.graph.graph.invoke_tool_with_injection",
        side_effect=_fake_update_invoke(task_plan_path),
    ):
        wrapped_tool = wrap_update_note_tool(ctx, _mock_tool("update_note"))
        result = await wrapped_tool.ainvoke(
            {
                "key": "task_plan",
                "target": "- [ ] Milestone 1",
                "replacement": "- [x] Milestone 1",
                "tool_call_id": "test_call_id",
            }
        )

    assert result == "Success"
    updated_content = task_plan_path.read_text(encoding="utf-8")
    assert "- [x] Milestone 1" in updated_content
    assert "- [ ] Milestone 2" in updated_content
    # Status flips never disturb the milestone texts: no validation spawned
    assert ctx.planner_task is None


@pytest.mark.asyncio
async def test_wrap_update_note_tool_intentional_rename_triggers_validation(tmp_path):
    task_plan_path = _setup_plan(tmp_path)
    ctx = _make_ctx(tmp_path)

    mock_validation = AsyncMock(return_value={"status": "success", "feedback": ""})
    with (
        patch(
            "artemis.graph.graph.invoke_tool_with_injection",
            side_effect=_fake_update_invoke(task_plan_path),
        ),
        patch("artemis.graph.graph.run_async_planner_validation", mock_validation),
    ):
        wrapped_tool = wrap_update_note_tool(ctx, _mock_tool("update_note"))
        result = await wrapped_tool.ainvoke(
            {
                "key": "task_plan",
                "target": "- [ ] Milestone 1",
                "replacement": "- [ ] Milestone Renamed",
                "tool_call_id": "test_call_id",
            }
        )
        assert result == "Success"
        # Declared update_note edit is applied, but ratchet validation fires
        assert ctx.planner_task is not None
        await ctx.planner_task

    assert "Milestone Renamed" in task_plan_path.read_text(encoding="utf-8")
    mock_validation.assert_called_once()
    # Validation compares against the ratchet baseline (the original plan)
    assert mock_validation.call_args.args[2] == ORIGINAL_PLAN


@pytest.mark.asyncio
async def test_ratchet_baseline_survives_consecutive_small_edits(tmp_path):
    """Salami-slicing: every small rename is judged against the ORIGINAL
    validated baseline, not the immediately preceding write."""
    import asyncio

    task_plan_path = _setup_plan(tmp_path)
    ctx = _make_ctx(tmp_path)

    gate = asyncio.Event()

    async def blocking_validation(*args, **kwargs):
        await gate.wait()
        return {"status": "success", "feedback": ""}

    mock_validation = AsyncMock(side_effect=blocking_validation)
    with (
        patch(
            "artemis.graph.graph.invoke_tool_with_injection",
            side_effect=_fake_update_invoke(task_plan_path),
        ),
        patch("artemis.graph.graph.run_async_planner_validation", mock_validation),
    ):
        wrapped_tool = wrap_update_note_tool(ctx, _mock_tool("update_note"))
        await wrapped_tool.ainvoke(
            {
                "key": "task_plan",
                "target": "- [ ] Milestone 1",
                "replacement": "- [ ] Milestone 1a",
                "tool_call_id": "call_1",
            }
        )
        first_task = ctx.planner_task
        assert first_task is not None

        await wrapped_tool.ainvoke(
            {
                "key": "task_plan",
                "target": "- [ ] Milestone 1a",
                "replacement": "- [ ] Milestone 1b",
                "tool_call_id": "call_2",
            }
        )
        second_task = ctx.planner_task
        assert second_task is not first_task

        # The superseded in-flight validation is cancelled
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await first_task
        await second_task

    # Both validation runs were anchored to the original baseline
    for call in mock_validation.call_args_list:
        assert call.args[2] == ORIGINAL_PLAN


@pytest.mark.asyncio
async def test_wrap_update_note_tool_allow_subgoal_changes(tmp_path):
    task_plan_path = _setup_plan(tmp_path)
    ctx = _make_ctx(tmp_path)

    with patch(
        "artemis.graph.graph.invoke_tool_with_injection",
        side_effect=_fake_update_invoke(task_plan_path),
    ):
        wrapped_tool = wrap_update_note_tool(ctx, _mock_tool("update_note"))
        result = await wrapped_tool.ainvoke(
            {
                "key": "task_plan",
                "target": "  - [ ] Substep 1.1",
                "replacement": "  - [x] Substep 1.1 Completed",
                "tool_call_id": "test_call_id",
            }
        )

    assert result == "Success"
    updated_content = task_plan_path.read_text(encoding="utf-8")
    assert "Substep 1.1 Completed" in updated_content
    assert "- [ ] Milestone 1" in updated_content
    # Sub-task edits never touch the top-level milestone texts
    assert ctx.planner_task is None


@pytest.mark.asyncio
async def test_wrap_note_tool_full_rewrite_hand_slip_rejected(tmp_path):
    """A save_note full rewrite that rewords status-unchanged milestones is the
    hand-slip signature: rejected and rolled back, no validation spawned."""
    task_plan_path = _setup_plan(tmp_path)
    ctx = _make_ctx(tmp_path)

    corrupted_plan = """# Test Plan
- [ ] Milestone 1 (Altered)
- [ ] New Milestone Added
"""

    with patch(
        "artemis.graph.graph.invoke_tool_with_injection",
        side_effect=_fake_save_invoke(task_plan_path),
    ):
        wrapped_tool = wrap_note_tool(ctx, _mock_tool("save_note"))
        result = await wrapped_tool.ainvoke(
            {
                "key": "task_plan",
                "content": corrupted_plan,
                "tool_call_id": "test_call_id",
            }
        )

    assert isinstance(result, ToolMessage)
    assert task_plan_path.read_text(encoding="utf-8") == ORIGINAL_PLAN
    assert ctx.planner_task is None


@pytest.mark.asyncio
async def test_wrap_note_tool_lexical_drift_rejected(tmp_path):
    """Even a tiny same-status rewording in a full rewrite bounces back with
    guidance — this is exactly the drift that similarity thresholds missed."""
    original_plan = """# Test Plan
- [ ] Open the Chrome browser and navigate to youtube.com
"""
    task_plan_path = _setup_plan(tmp_path, original_plan)
    ctx = _make_ctx(tmp_path)

    minor_drift_plan = """# Test Plan
- [ ] Open Chrome browser and navigate to youtube.com
"""

    with patch(
        "artemis.graph.graph.invoke_tool_with_injection",
        side_effect=_fake_save_invoke(task_plan_path),
    ):
        wrapped_tool = wrap_note_tool(ctx, _mock_tool("save_note"))
        result = await wrapped_tool.ainvoke(
            {
                "key": "task_plan",
                "content": minor_drift_plan,
                "tool_call_id": "test_call_id",
            }
        )

    assert isinstance(result, ToolMessage)
    assert "update_note" in str(result.content)
    assert task_plan_path.read_text(encoding="utf-8") == original_plan
    assert ctx.planner_task is None


@pytest.mark.asyncio
async def test_wrap_note_tool_structural_replan_triggers_validation(tmp_path):
    """Adding a milestone via full rewrite is a declared replan: applied, then
    judged asynchronously by the planner against the ratchet baseline."""
    task_plan_path = _setup_plan(tmp_path)
    ctx = _make_ctx(tmp_path)

    extended_plan = """# Test Plan
- [ ] Milestone 1
  - [ ] Substep 1.1
- [ ] Milestone 2
- [ ] Milestone 3 (new phase)
"""

    mock_validation = AsyncMock(return_value={"status": "success", "feedback": ""})
    with (
        patch(
            "artemis.graph.graph.invoke_tool_with_injection",
            side_effect=_fake_save_invoke(task_plan_path),
        ),
        patch("artemis.graph.graph.run_async_planner_validation", mock_validation),
    ):
        wrapped_tool = wrap_note_tool(ctx, _mock_tool("save_note"))
        result = await wrapped_tool.ainvoke(
            {
                "key": "task_plan",
                "content": extended_plan,
                "tool_call_id": "test_call_id",
            }
        )
        assert result == "Success"
        assert ctx.planner_task is not None
        await ctx.planner_task

    assert "Milestone 3 (new phase)" in task_plan_path.read_text(encoding="utf-8")
    mock_validation.assert_called_once()
