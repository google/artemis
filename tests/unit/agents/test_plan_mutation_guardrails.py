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

import pytest

from artemis.context import ArtemisContext, ExecutionSetup
from artemis.graph.graph import (
    check_plan_mutation_rejections,
    check_unintended_rewrite,
    wrap_note_tool,
)
from artemis.graph.state import State

CONTINUOUS_PLAN = (
    "- [x] Open app\n"
    "- [/] [Loop:continuous] Periodically monitor for new emails "
    "(Interval: every 5 minutes)\n"
    "  - [x] Polling Check #1: Done"
)


def test_check_plan_mutation_incomplete_nested():
    content_before = "- [/] Main task\n  - [ ] Subtask 1"
    content_after = "- [x] Main task\n  - [ ] Subtask 1"
    res = check_plan_mutation_rejections(content_before, content_after)
    assert res is not None
    assert "goals in the task plan that have not been marked as completed" in res


def test_check_plan_mutation_delete_continuous_loop():
    content_after = (
        "- [x] Open app\n"
        "- [x] Monitor for new emails\n"
        "  - [x] Baseline established\n"
        "  - [x] Polling Check #1: Done"
    )
    res = check_plan_mutation_rejections(CONTINUOUS_PLAN, content_after)
    assert res is not None
    assert "cannot delete an active [Loop:continuous]" in res


def test_check_plan_mutation_mark_continuous_loop_completed():
    content_after = CONTINUOUS_PLAN.replace("- [/] [Loop:continuous]", "- [x] [Loop:continuous]")
    res = check_plan_mutation_rejections(CONTINUOUS_PLAN, content_after)
    assert res is not None
    assert "cannot be" in res and "marked as completed [x]" in res


def test_check_plan_mutation_user_stopped_allowed():
    content_after = CONTINUOUS_PLAN.replace("- [/] [Loop:continuous]", "- [x] [Loop:continuous]")
    state = MagicMock(spec=State)
    state.user_stop_requested = True
    res = check_plan_mutation_rejections(CONTINUOUS_PLAN, content_after, state=state)
    assert res is None


def test_check_plan_mutation_stop_wording_is_not_a_signal():
    """Natural-language 'stop' phrasing must NOT unlock the continuous loop —
    only the explicit release_loop signal (user_stop_requested) does."""
    content_after = CONTINUOUS_PLAN.replace("- [/] [Loop:continuous]", "- [x] [Loop:continuous]")
    state = MagicMock(spec=State)
    state.user_stop_requested = False
    state.injected_instruction = "Please stop the task now."
    res = check_plan_mutation_rejections(CONTINUOUS_PLAN, content_after, state=state)
    assert res is not None


def test_check_plan_mutation_bounded_loop_completion_allowed():
    """Plain [Loop] milestones are bounded: the model may complete them."""
    content_before = (
        "- [x] Open app\n"
        "- [/] [Loop] Inspect candidate emails (Exit: all 5 candidates inspected)\n"
        "  - [x] Candidate #5: Done"
    )
    content_after = content_before.replace("- [/] [Loop]", "- [x] [Loop]")
    res = check_plan_mutation_rejections(content_before, content_after)
    assert res is None


def test_check_plan_mutation_normal_bounded_task():
    content_before = "- [ ] Open Settings\n- [ ] Toggle WiFi"
    content_after = "- [x] Open Settings\n- [x] Toggle WiFi"
    res = check_plan_mutation_rejections(content_before, content_after)
    assert res is None


def test_check_unintended_rewrite_detects_hand_slip():
    content_before = "- [x] Open Settings app\n- [/] Toggle the WiFi switch"
    # Full rewrite: milestone 1's status unchanged but text drifted
    content_after = "- [x] Open the Settings application\n- [/] Toggle the WiFi switch"
    res = check_unintended_rewrite(content_before, content_after)
    assert res is not None
    assert "Open Settings app" in res
    assert "update_note" in res


def test_check_unintended_rewrite_allows_status_flips_and_appends():
    content_before = "- [x] Open Settings\n- [/] Toggle WiFi"
    content_after = "- [x] Open Settings\n- [x] Toggle WiFi\n  - [x] Confirmed enabled"
    assert check_unintended_rewrite(content_before, content_after) is None


def test_check_unintended_rewrite_ignores_structural_replans():
    # Milestone count changes are declared replans, judged by planner validation
    content_before = "- [x] Open Settings\n- [/] Toggle WiFi"
    content_after = "- [x] Open Settings\n- [/] Toggle WiFi\n- [ ] Verify connectivity"
    assert check_unintended_rewrite(content_before, content_after) is None


# --- §4.3 deterministic check-line guard (independent of planner validation) ---------

GUARDED_PLAN = (
    "- [/] Create the alarm\n"
    "  - verify: alarm exists in the list\n"
    "  - assert: a toast appeared\n"
    "- [ ] Next milestone\n"
)


def _guard_ctx(tmp_path, **setup_kwargs):
    # Planner validation OFF by default: the guard must hold on its own.
    setup_kwargs.setdefault("disable_planner_validation", True)
    setup_kwargs.setdefault("disable_checker", False)
    ctx = MagicMock(spec=ArtemisContext)
    ctx.execution_setup = ExecutionSetup(**setup_kwargs)
    ctx.data_engine = MagicMock()
    ctx.data_engine.base_dir = str(tmp_path)
    ctx.planner_task = None
    ctx.last_validated_plan = None
    ctx.pending_validated_plan = None
    ctx.pending_checkpoints = []
    ctx.checkpoint_tasks = {}
    return ctx


def _guard_plan_path(tmp_path, content=GUARDED_PLAN):
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    path = notes_dir / "task_plan.md"
    path.write_text(content, encoding="utf-8")
    return path


def _save_invoke(path):
    async def fake(tool, args, tool_call_id, state, record_trace=None):
        path.write_text(args["content"], encoding="utf-8")
        return "Success"

    return fake


def _save_tool():
    tool = MagicMock()
    tool.name = "save_note"
    tool.description = "save_note tool"
    return tool


@pytest.mark.asyncio
async def test_operator_deleting_check_line_is_restored(tmp_path):
    path = _guard_plan_path(tmp_path)
    ctx = _guard_ctx(tmp_path)

    rewritten = "- [/] Create the alarm\n  - assert: a toast appeared\n- [ ] Next milestone\n"
    with patch(
        "artemis.graph.graph.invoke_tool_with_injection",
        side_effect=_save_invoke(path),
    ):
        wrapped = wrap_note_tool(ctx, _save_tool())
        await wrapped.ainvoke({"key": "task_plan", "content": rewritten, "tool_call_id": "t"})

    content = path.read_text(encoding="utf-8")
    assert "- verify: alarm exists in the list" in content
    assert "- assert: a toast appeared" in content
    # No planner validation was needed or spawned (guard is deterministic)
    assert ctx.planner_task is None


@pytest.mark.asyncio
async def test_operator_rewording_check_line_restores_original_keeps_new(tmp_path):
    path = _guard_plan_path(tmp_path)
    ctx = _guard_ctx(tmp_path)

    rewritten = GUARDED_PLAN.replace(
        "  - verify: alarm exists in the list\n",
        "  - verify: alarm roughly configured\n",
    )
    with patch(
        "artemis.graph.graph.invoke_tool_with_injection",
        side_effect=_save_invoke(path),
    ):
        wrapped = wrap_note_tool(ctx, _save_tool())
        await wrapped.ainvoke({"key": "task_plan", "content": rewritten, "tool_call_id": "t"})

    content = path.read_text(encoding="utf-8")
    # Original standard restored; the Operator's new line survives as an addition
    assert "- verify: alarm exists in the list" in content
    assert "- verify: alarm roughly configured" in content


@pytest.mark.asyncio
async def test_deleted_parent_turns_check_into_task_level_at_end(tmp_path):
    path = _guard_plan_path(tmp_path)
    ctx = _guard_ctx(tmp_path)

    # Whole parent subgoal removed
    rewritten = "- [ ] Next milestone\n"
    with patch(
        "artemis.graph.graph.invoke_tool_with_injection",
        side_effect=_save_invoke(path),
    ):
        wrapped = wrap_note_tool(ctx, _save_tool())
        await wrapped.ainvoke({"key": "task_plan", "content": rewritten, "tool_call_id": "t"})

    content = path.read_text(encoding="utf-8")
    assert "- verify@end: alarm exists in the list" in content
    assert "- assert@end: a toast appeared" in content


@pytest.mark.asyncio
async def test_operator_adding_check_lines_is_allowed(tmp_path):
    path = _guard_plan_path(tmp_path)
    ctx = _guard_ctx(tmp_path)

    extended = GUARDED_PLAN + "  - verify: extra criterion\n"
    with patch(
        "artemis.graph.graph.invoke_tool_with_injection",
        side_effect=_save_invoke(path),
    ):
        wrapped = wrap_note_tool(ctx, _save_tool())
        result = await wrapped.ainvoke(
            {"key": "task_plan", "content": extended, "tool_call_id": "t"}
        )

    assert result == "Success"
    assert "- verify: extra criterion" in path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_guard_holds_with_planner_validation_disabled_and_checks_off(tmp_path):
    """The guard is content-driven: it protects check lines even when every
    check switch AND planner validation are disabled."""
    path = _guard_plan_path(tmp_path)
    ctx = _guard_ctx(tmp_path, disable_checker=True, disable_planner_validation=True)

    with patch(
        "artemis.graph.graph.invoke_tool_with_injection",
        side_effect=_save_invoke(path),
    ):
        wrapped = wrap_note_tool(ctx, _save_tool())
        await wrapped.ainvoke(
            {
                "key": "task_plan",
                "content": "- [/] Create the alarm\n- [ ] Next milestone\n",
                "tool_call_id": "t",
            }
        )

    content = path.read_text(encoding="utf-8")
    assert "- verify: alarm exists in the list" in content
    assert "- assert: a toast appeared" in content


@pytest.mark.asyncio
async def test_planner_node_writes_are_not_guarded():
    """The Planner keeps revision authority over check standards: its node is
    wired with the UNWRAPPED note tools, so the guard never applies to it."""
    from artemis.context import DeviceContext, DevicePlatform
    from artemis.graph.graph import get_graph

    device = DeviceContext(
        host_platform="LINUX",
        mobile_platform=DevicePlatform.ANDROID,
        device_id="dummy",
        device_width=1080,
        device_height=2400,
    )
    ctx = ArtemisContext(device=device, execution_setup=ExecutionSetup())
    graph = await get_graph(ctx)

    planner_tools = {t.name: t for t in graph.nodes["planner"].bound.afunc.tools}
    operator_tools = {t.name: t for t in graph.nodes["operator"].bound.afunc.tools}

    # Operator's plan-writing tools go through the guard wrapper...
    assert operator_tools["save_note"].coroutine.__name__ == "wrapped_note_tool"
    assert operator_tools["update_note"].coroutine.__name__ == "wrapped_update_note"
    # ...the Planner's do not.
    assert getattr(planner_tools["save_note"].coroutine, "__name__", "") != "wrapped_note_tool"
    assert getattr(planner_tools["update_note"].coroutine, "__name__", "") != "wrapped_update_note"
