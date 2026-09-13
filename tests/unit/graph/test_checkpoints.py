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

"""Checkpoint scheduling and harvest tests: queue/spawn separation, supersede
without verdict loss, applicability gating, repair quota, halt policy,
timeouts, unconditional record_step, and fail-open axis separation."""

import asyncio
import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from artemis.agents.checker.checker import CheckReport, CheckVerdict
from artemis.context import ArtemisContext, ExecutionSetup
from artemis.graph.checkpoints import (
    CheckpointRun,
    PendingCheckpoint,
    checker_note_key,
    harvest_finished_checkpoints,
    harvest_run,
    is_checker_note_key,
    queue_checkpoints,
    read_ledger,
    spawn_pending_checkpoints,
)
from artemis.graph.graph import execution_check_node, wrap_note_tool, wrap_update_note_tool
from artemis.graph.state import State
from artemis.utils.plan_grammar import CheckItem, parse_plan, subgoal_hash

GOAL_TEXT = "Create the alarm"
GOAL_KEY = subgoal_hash(GOAL_TEXT)

PLAN_WITH_CHECKS = (
    f"- [x] {GOAL_TEXT}\n"
    "  - verify: the alarm list shows 7:30 AM\n"
    "  - assert: a toast appeared\n"
    "- [ ] Next milestone\n"
)


def _make_ctx(tmp_path, **setup_kwargs):
    setup_kwargs.setdefault("disable_checker", False)
    # These tests exercise the midway checkpoint machinery, which is off in
    # the factory layering — pin it on unless a test overrides it.
    setup_kwargs.setdefault("disable_midway_checks", False)
    ctx = MagicMock(spec=ArtemisContext)
    ctx.execution_setup = ExecutionSetup(**setup_kwargs)
    ctx.data_engine = MagicMock()
    ctx.data_engine.base_dir = tmp_path
    ctx.pending_checkpoints = []
    ctx.checkpoint_tasks = {}
    ctx.checkpoint_attempt_seq = {}
    ctx.checkpoint_repairs = {}
    ctx.assert_halt = False
    ctx.final_check_attempts = 0
    ctx.planner_task = None
    ctx.last_validated_plan = None
    ctx.pending_validated_plan = None
    return ctx


def _make_state(**overrides):
    state = MagicMock(spec=State)
    state.initial_goal = "the user goal"
    state.user_stop_requested = False
    state.operator_feedback = None
    state.operator_raw_data = None
    state.structured_decisions = None
    defaults = {
        "operator_raw_thinking": None,
        "operator_native_thinking": None,
    }
    for k, v in {**defaults, **overrides}.items():
        setattr(state, k, v)
    return state


def _write_plan(tmp_path, content=PLAN_WITH_CHECKS):
    notes = tmp_path / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    path = notes / "task_plan.md"
    path.write_text(content, encoding="utf-8")
    return path


def _pending(plan=PLAN_WITH_CHECKS, kinds=("verify", "assert")):
    snapshot = parse_plan(plan)
    item = next(i for i in snapshot.top_level if i.key == GOAL_KEY)
    items = tuple(
        ci for ci in snapshot.check_items_of(item) if ci.when == "on_complete" and ci.kind in kinds
    )
    return PendingCheckpoint(
        checkpoint_id=GOAL_KEY,
        subgoal_text=GOAL_TEXT,
        check_items=items,
        plan_text=plan,
        trigger_ts=1.0,
    )


def _done_run(ctx, report_or_exc, attempt_id=f"{GOAL_KEY}#1", pending=None):
    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    if isinstance(report_or_exc, Exception):
        fut.set_exception(report_or_exc)
    else:
        fut.set_result(report_or_exc)
    return CheckpointRun(attempt_id=attempt_id, task=fut, checkpoint=pending or _pending())


def _verdict(kind="verify", status="failed", text=None, evidence="concrete evidence"):
    default_text = "the alarm list shows 7:30 AM" if kind == "verify" else "a toast appeared"
    return CheckVerdict(
        item_text=text or default_text,
        kind=kind,
        status=status,
        evidence=evidence,
        suggestion="fix it" if (kind == "verify" and status == "failed") else "",
    )


# --- §8.1 / §8.2: queueing ------------------------------------------------------------


def test_queue_checkpoints_enqueues_every_completion_with_items(tmp_path):
    ctx = _make_ctx(tmp_path)
    state = _make_state()
    plan = (
        "- [x] A\n  - verify: VA\n"
        "- [x] B\n  - verify: VB\n"
        "- [x] C\n"  # no check items -> never queued
    )
    after = parse_plan(plan)
    queue_checkpoints(ctx, state, after, ["A", "B", "C"], plan)
    assert [p.checkpoint_id for p in ctx.pending_checkpoints] == [
        subgoal_hash("A"),
        subgoal_hash("B"),
    ]
    # Only queued, never spawned here
    assert ctx.checkpoint_tasks == {}


def test_queue_checkpoints_ignores_at_end_items(tmp_path):
    ctx = _make_ctx(tmp_path)
    plan = "- [x] A\n  - assert@end: final only\n"
    queue_checkpoints(ctx, _make_state(), parse_plan(plan), ["A"], plan)
    assert ctx.pending_checkpoints == []


def test_queue_checkpoints_respects_midway_gate_and_user_stop(tmp_path):
    plan = "- [x] A\n  - verify: VA\n"
    after = parse_plan(plan)

    ctx = _make_ctx(tmp_path, disable_midway_checks=True)
    queue_checkpoints(ctx, _make_state(), after, ["A"], plan)
    assert ctx.pending_checkpoints == []

    # Legacy master alias also gates
    ctx2 = _make_ctx(tmp_path, disable_checker=True)
    queue_checkpoints(ctx2, _make_state(), after, ["A"], plan)
    assert ctx2.pending_checkpoints == []

    ctx3 = _make_ctx(tmp_path)
    queue_checkpoints(ctx3, _make_state(user_stop_requested=True), after, ["A"], plan)
    assert ctx3.pending_checkpoints == []


@pytest.mark.asyncio
async def test_process_plan_write_only_queues(tmp_path):
    """The plan-write pipeline enqueues but never spawns: spawning waits for the
    turn's step to be recorded."""
    plan_before = PLAN_WITH_CHECKS.replace(f"- [x] {GOAL_TEXT}", f"- [/] {GOAL_TEXT}")
    task_plan_path = _write_plan(tmp_path, plan_before)
    ctx = _make_ctx(tmp_path)
    state = _make_state()

    async def fake_invoke(tool, args, tool_call_id, state, record_trace=None):
        task_plan_path.write_text(args["content"], encoding="utf-8")
        return "Success"

    mock_tool = MagicMock()
    mock_tool.name = "save_note"
    mock_tool.description = "save"

    with patch("artemis.graph.graph.invoke_tool_with_injection", side_effect=fake_invoke):
        wrapped = wrap_note_tool(ctx, mock_tool)
        await wrapped.ainvoke(
            {"key": "task_plan", "content": PLAN_WITH_CHECKS, "tool_call_id": "t1"}
        )

    assert len(ctx.pending_checkpoints) == 1
    pc = ctx.pending_checkpoints[0]
    assert pc.checkpoint_id == GOAL_KEY
    assert [ci.kind for ci in pc.check_items] == ["verify", "assert"]
    assert pc.plan_text == PLAN_WITH_CHECKS
    assert ctx.checkpoint_tasks == {}


VERIFY_LINE = "  - verify: the alarm list shows 7:30 AM\n"
ASSERT_LINE = "  - assert: a toast appeared\n"


def _guidance_arrives(ctx, plan_text):
    """What perception_node does when a non-empty user instruction lands: the
    check lines that exist at that moment lose their machine restoration."""
    from artemis.utils.plan_grammar import parse_plan

    current = getattr(ctx, "guidance_unprotected_checks", None)
    if not isinstance(current, set):
        current = set()
    ctx.guidance_unprotected_checks = current | {
        (ci.kind, ci.text) for ci in parse_plan(plan_text).check_items
    }


async def _plan_write(tmp_path, ctx, before, after):
    """One accepted task_plan write through the shared post-write pipeline;
    returns the plan text as it stands on disk afterwards."""
    from artemis.graph.graph import _process_plan_write

    task_plan_path = _write_plan(tmp_path, after)
    await _process_plan_write(ctx, _make_state(), task_plan_path, before, after, "ok", True)
    return task_plan_path.read_text(encoding="utf-8")


NEW_VERIFY_LINE = "  - verify: the build number is recorded\n"


@pytest.mark.asyncio
async def test_pre_guidance_check_lines_may_be_dropped_at_any_later_write(tmp_path):
    """Without guidance a deleted check line grows back. Once user guidance has
    arrived, the lines that existed at that moment stay editable for the rest
    of the run: the Operator re-plans over several writes, turns later, and
    none of them is undone."""
    without_verify = PLAN_WITH_CHECKS.replace(VERIFY_LINE, "")
    without_both = without_verify.replace(ASSERT_LINE, "")
    ctx = _make_ctx(tmp_path, disable_planner_validation=True)
    ctx.guidance_unprotected_checks = set()

    # Ordinary write (no guidance): the deleted line grows back.
    assert VERIFY_LINE in await _plan_write(tmp_path, ctx, PLAN_WITH_CHECKS, without_verify)

    # Turn N: guidance arrives; the Operator only advances a milestone.
    _guidance_arrives(ctx, PLAN_WITH_CHECKS)
    started_next = PLAN_WITH_CHECKS.replace("- [ ] Next milestone", "- [/] Next milestone")
    assert await _plan_write(tmp_path, ctx, PLAN_WITH_CHECKS, started_next) == started_next

    # Turn N+2: the first check line goes, and stands.
    dropped_verify = started_next.replace(VERIFY_LINE, "")
    assert await _plan_write(tmp_path, ctx, started_next, dropped_verify) == dropped_verify
    # Turn N+3: the second one goes too, and also stands (no single-write window).
    dropped_both = dropped_verify.replace(ASSERT_LINE, "")
    assert await _plan_write(tmp_path, ctx, dropped_verify, dropped_both) == dropped_both


@pytest.mark.asyncio
async def test_check_lines_added_after_guidance_stay_protected(tmp_path):
    """A check line the Operator adds after the instruction is a new standard:
    deleting it later is restored, while the pre-guidance line still may go."""
    ctx = _make_ctx(tmp_path, disable_planner_validation=True)
    ctx.guidance_unprotected_checks = set()
    _guidance_arrives(ctx, PLAN_WITH_CHECKS)

    with_new = PLAN_WITH_CHECKS.replace(ASSERT_LINE, ASSERT_LINE + NEW_VERIFY_LINE)
    assert await _plan_write(tmp_path, ctx, PLAN_WITH_CHECKS, with_new) == with_new

    dropped_new = with_new.replace(NEW_VERIFY_LINE, "")
    on_disk = await _plan_write(tmp_path, ctx, with_new, dropped_new)
    assert NEW_VERIFY_LINE in on_disk  # restored: it postdates the guidance

    dropped_old = with_new.replace(VERIFY_LINE, "")
    on_disk = await _plan_write(tmp_path, ctx, with_new, dropped_old)
    assert VERIFY_LINE not in on_disk  # stands: it predates the guidance
    assert NEW_VERIFY_LINE in on_disk


@pytest.mark.asyncio
async def test_second_guidance_waives_the_lines_that_exist_by_then(tmp_path):
    ctx = _make_ctx(tmp_path, disable_planner_validation=True)
    ctx.guidance_unprotected_checks = set()
    _guidance_arrives(ctx, PLAN_WITH_CHECKS)
    with_new = PLAN_WITH_CHECKS.replace(ASSERT_LINE, ASSERT_LINE + NEW_VERIFY_LINE)
    assert await _plan_write(tmp_path, ctx, PLAN_WITH_CHECKS, with_new) == with_new
    dropped_new = with_new.replace(NEW_VERIFY_LINE, "")
    assert NEW_VERIFY_LINE in await _plan_write(tmp_path, ctx, with_new, dropped_new)

    _guidance_arrives(ctx, with_new)  # the new line exists now: it is waived too
    assert NEW_VERIFY_LINE not in await _plan_write(tmp_path, ctx, with_new, dropped_new)


@pytest.mark.asyncio
async def test_mixed_write_restores_only_the_protected_lines(tmp_path):
    """One write drops a pre-guidance line and a post-guidance line: only the
    protected one is merged back."""
    ctx = _make_ctx(tmp_path, disable_planner_validation=True)
    ctx.guidance_unprotected_checks = set()
    _guidance_arrives(ctx, PLAN_WITH_CHECKS)
    with_new = PLAN_WITH_CHECKS.replace(ASSERT_LINE, ASSERT_LINE + NEW_VERIFY_LINE)
    assert await _plan_write(tmp_path, ctx, PLAN_WITH_CHECKS, with_new) == with_new

    dropped_both = with_new.replace(VERIFY_LINE, "").replace(NEW_VERIFY_LINE, "")
    on_disk = await _plan_write(tmp_path, ctx, with_new, dropped_both)
    assert VERIFY_LINE not in on_disk
    assert NEW_VERIFY_LINE in on_disk


@pytest.mark.asyncio
async def test_guided_drop_records_the_retired_lines_and_the_outcome_excludes_them(tmp_path):
    """A line dropped under guidance is remembered as retired; a verdict the
    ledger recorded for it earlier is reported as retired, not as a failure,
    while a line still declared keeps its verdict."""
    from artemis.graph.checkpoints import compute_test_summary
    from artemis.utils.plan_grammar import parse_plan

    ctx = _make_ctx(tmp_path, disable_planner_validation=True)
    ctx.guidance_unprotected_checks = set()
    ctx.guidance_retired_checks = set()
    _guidance_arrives(ctx, PLAN_WITH_CHECKS)
    without_verify = PLAN_WITH_CHECKS.replace(VERIFY_LINE, "")
    assert await _plan_write(tmp_path, ctx, PLAN_WITH_CHECKS, without_verify) == without_verify
    assert ctx.guidance_retired_checks == {("verify", "the alarm list shows 7:30 AM")}

    records = [
        {"kind": "verify", "item_text": "the alarm list shows 7:30 AM", "status": "failed"},
        {"kind": "assert", "item_text": "a toast appeared", "status": "passed"},
    ]
    summary = compute_test_summary(
        list(parse_plan(without_verify).all_check_items),
        records,
        retired=ctx.guidance_retired_checks,
    )
    assert (summary.passed, summary.failed, summary.retired) == (1, 0, 1)
    assert summary.retired_items == [
        {"item_text": "the alarm list shows 7:30 AM", "kind": "verify", "last_status": "failed"}
    ]
    # A retired line that is declared again (the Operator re-added it) counts normally.
    summary = compute_test_summary(
        list(parse_plan(PLAN_WITH_CHECKS).all_check_items),
        records,
        retired=ctx.guidance_retired_checks,
    )
    assert (summary.passed, summary.failed, summary.retired) == (1, 1, 0)


REWORDED_MILESTONE = PLAN_WITH_CHECKS.replace(f"- [x] {GOAL_TEXT}", "- [x] Create the 7:30 alarm")


@pytest.mark.asyncio
async def test_rewording_a_milestone_keeps_its_check_lines_in_place(tmp_path):
    """The check lines under a reworded milestone change signature (parent
    hash) but stay declared: they are neither duplicated as task-level @end
    orphans nor, under guidance, retired."""
    ctx = _make_ctx(tmp_path, disable_planner_validation=True)
    ctx.guidance_unprotected_checks = set()
    ctx.guidance_retired_checks = set()

    # No guidance: nothing to restore, the plan stands as written.
    on_disk = await _plan_write(tmp_path, ctx, PLAN_WITH_CHECKS, REWORDED_MILESTONE)
    assert on_disk == REWORDED_MILESTONE
    assert "@end" not in on_disk

    # Guidance armed: the same edit retires nothing.
    _guidance_arrives(ctx, PLAN_WITH_CHECKS)
    on_disk = await _plan_write(tmp_path, ctx, PLAN_WITH_CHECKS, REWORDED_MILESTONE)
    assert on_disk == REWORDED_MILESTONE
    assert ctx.guidance_retired_checks == set()


@pytest.mark.asyncio
async def test_redeclaring_a_retired_line_reinstates_it(tmp_path):
    """Dropped under guidance, then added back: the harvest gate must see the
    line as live again, matching what compute_test_summary reports."""
    from artemis.graph.checkpoints import reinstate_check_items

    ctx = _make_ctx(tmp_path, disable_planner_validation=True)
    ctx.guidance_unprotected_checks = set()
    ctx.guidance_retired_checks = set()
    _guidance_arrives(ctx, PLAN_WITH_CHECKS)
    without_verify = PLAN_WITH_CHECKS.replace(VERIFY_LINE, "")
    await _plan_write(tmp_path, ctx, PLAN_WITH_CHECKS, without_verify)
    assert ctx.guidance_retired_checks == {RETIRED_VERIFY}

    await _plan_write(tmp_path, ctx, without_verify, PLAN_WITH_CHECKS)
    assert ctx.guidance_retired_checks == set()

    # Direct helper: unrelated items leave the set untouched.
    ctx.guidance_retired_checks = {RETIRED_VERIFY}
    reinstate_check_items(ctx, [RETIRED_ASSERT])
    assert ctx.guidance_retired_checks == {RETIRED_VERIFY}


@pytest.mark.asyncio
async def test_rejected_plan_write_leaves_the_waiver_untouched(tmp_path):
    from langchain_core.messages import ToolMessage

    from artemis.graph.graph import _process_plan_write

    ctx = _make_ctx(tmp_path, disable_planner_validation=True)
    ctx.guidance_unprotected_checks = set()
    _guidance_arrives(ctx, PLAN_WITH_CHECKS)
    waived = set(ctx.guidance_unprotected_checks)
    reworded = PLAN_WITH_CHECKS.replace("- [ ] Next milestone", "- [ ] The next milestone")
    task_plan_path = _write_plan(tmp_path, reworded)
    result = await _process_plan_write(
        ctx, _make_state(), task_plan_path, PLAN_WITH_CHECKS, reworded, "ok", False
    )
    assert isinstance(result, ToolMessage)
    assert task_plan_path.read_text(encoding="utf-8") == PLAN_WITH_CHECKS
    assert ctx.guidance_unprotected_checks == waived


# --- §8.1 / §8.10: spawn after record_step; record_step unconditional ----------------


def _raw_data():
    return {
        "screenshot_b64": base64.b64encode(b"img").decode(),
        "xml_hierarchy": [],
        "ocr_results": [],
        "width": 1080,
        "height": 2400,
    }


@pytest.mark.asyncio
async def test_execution_check_spawns_after_record_step_with_correct_anchor(tmp_path):
    _write_plan(tmp_path)
    ctx = _make_ctx(tmp_path)
    ctx.data_engine.record_step.return_value = "step-uuid-42"
    ctx.pending_checkpoints.append(_pending())
    state = _make_state(operator_raw_data=_raw_data())

    captured = {}

    async def fake_check(ctx_arg, check_items, anchor, goal, subgoal_text, attempt_id=None):
        captured["anchor"] = anchor
        captured["goal"] = goal
        return CheckReport(verdicts=[])

    with (
        patch(
            "artemis.agents.checker.checker.run_checkpoint_check",
            side_effect=fake_check,
        ),
        patch("artemis.graph.graph._get_active_subgoal_hashes", return_value=("h", None)),
    ):
        update = await execution_check_node(state, ctx)
        assert ctx.data_engine.record_step.called
        assert update["current_step_id"] == "step-uuid-42"
        assert GOAL_KEY in ctx.checkpoint_tasks
        # Let the spawned task run
        await ctx.checkpoint_tasks[GOAL_KEY].task

    assert captured["anchor"].anchor_step_id == "step-uuid-42"
    assert captured["anchor"].plan_text == PLAN_WITH_CHECKS
    assert captured["goal"] == "the user goal"


@pytest.mark.asyncio
async def test_execution_check_no_spawn_after_user_stop(tmp_path):
    _write_plan(tmp_path)
    ctx = _make_ctx(tmp_path)
    ctx.data_engine.record_step.return_value = "sid"
    ctx.pending_checkpoints.append(_pending())
    state = _make_state(operator_raw_data=_raw_data(), user_stop_requested=True)

    with patch("artemis.graph.graph._get_active_subgoal_hashes", return_value=("h", None)):
        await execution_check_node(state, ctx)

    assert ctx.checkpoint_tasks == {}
    # Step is still recorded regardless
    assert ctx.data_engine.record_step.called


@pytest.mark.asyncio
async def test_planner_flag_is_advisory_hint_only(tmp_path):
    """A flagged plan change is advisory: no rollback, the turn's actions
    still run, the Operator gets the concern + reason as feedback, the turn
    is recorded (tagged planner_flagged) and the ratchet baseline advances so
    the same change is not re-flagged on every later write."""
    plan_path = _write_plan(tmp_path)
    plan_on_disk = plan_path.read_text(encoding="utf-8")
    ctx = _make_ctx(tmp_path)
    ctx.data_engine.record_step.return_value = "flagged-step"
    ctx.last_validated_plan = "- [ ] old baseline\n"
    ctx.pending_validated_plan = plan_on_disk
    flagged = asyncio.get_event_loop().create_future()
    flagged.set_result({"status": "failed", "feedback": "milestone 2 drifts from the goal"})
    ctx.planner_task = flagged
    state = _make_state(operator_raw_data=_raw_data())

    with patch("artemis.graph.graph._get_active_subgoal_hashes", return_value=("h", None)):
        update = await execution_check_node(state, ctx)

    # Actions are not suppressed and the plan file is untouched.
    assert update["checker_success"] is True
    assert "structured_decisions" not in update
    assert plan_path.read_text(encoding="utf-8") == plan_on_disk
    assert update["current_step_id"] == "flagged-step"
    extra = ctx.data_engine.record_step.call_args.kwargs["extra_metadata"]
    assert extra.get("planner_flagged") is True
    # Hint + reason reach the Operator through operator_feedback.
    feedback = update["operator_feedback"]
    assert feedback and feedback[0].startswith("[planner]")
    assert "milestone 2 drifts from the goal" in feedback[0]
    assert "NOT rolled back" in feedback[0]
    # Baseline advanced to the reviewed content.
    assert ctx.last_validated_plan == plan_on_disk
    assert ctx.pending_validated_plan is None
    assert ctx.planner_task is None


@pytest.mark.asyncio
async def test_concurrency_cap_leaves_excess_pending(tmp_path):
    _write_plan(tmp_path)
    ctx = _make_ctx(tmp_path, max_concurrent_checkpoints=1)
    plans = []
    for name in ("A", "B"):
        plan = f"- [x] {name}\n  - verify: V{name}\n"
        plans.append(plan)
        snapshot = parse_plan(plan)
        item = snapshot.top_level[0]
        ctx.pending_checkpoints.append(
            PendingCheckpoint(
                checkpoint_id=item.key,
                subgoal_text=name,
                check_items=snapshot.check_items_of(item),
                plan_text=plan,
                trigger_ts=0.0,
            )
        )

    async def fake_check(*a, **k):
        return CheckReport(verdicts=[])

    with patch("artemis.agents.checker.checker.run_checkpoint_check", side_effect=fake_check):
        await spawn_pending_checkpoints(ctx, _make_state(), "sid")
        assert len(ctx.checkpoint_tasks) == 1
        assert len(ctx.pending_checkpoints) == 1
        for run in ctx.checkpoint_tasks.values():
            await run.task


# --- §8.3: supersede never loses verdicts --------------------------------------------


@pytest.mark.asyncio
async def test_supersede_books_finished_unharvested_attempt_first(tmp_path):
    _write_plan(tmp_path)
    ctx = _make_ctx(tmp_path)
    failed_report = CheckReport(verdicts=[_verdict("verify", "failed")])
    ctx.checkpoint_tasks[GOAL_KEY] = _done_run(ctx, failed_report)
    ctx.checkpoint_attempt_seq[GOAL_KEY] = 1
    ctx.pending_checkpoints.append(_pending())

    async def fake_check(*a, **k):
        return CheckReport(verdicts=[])

    with patch("artemis.agents.checker.checker.run_checkpoint_check", side_effect=fake_check):
        await spawn_pending_checkpoints(ctx, _make_state(), "sid")
        new_run = ctx.checkpoint_tasks[GOAL_KEY]
        await new_run.task

    # The failed verdict was booked before replacement — never dropped
    records = read_ledger(tmp_path)
    failed = [r for r in records if r["status"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["attempt_id"] == f"{GOAL_KEY}#1"
    # And a fresh attempt id was allocated
    assert new_run.attempt_id == f"{GOAL_KEY}#2"
    # Superseded harvest is ledger-only: no plan revert happened
    assert "- [x]" in (tmp_path / "notes" / "task_plan.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_supersede_cancels_running_attempt_and_books_superseded(tmp_path):
    _write_plan(tmp_path)
    ctx = _make_ctx(tmp_path)

    async def hang():
        await asyncio.sleep(3600)

    running = asyncio.create_task(hang())
    await asyncio.sleep(0)  # let it start
    ctx.checkpoint_tasks[GOAL_KEY] = CheckpointRun(
        attempt_id=f"{GOAL_KEY}#1", task=running, checkpoint=_pending()
    )
    ctx.checkpoint_attempt_seq[GOAL_KEY] = 1
    ctx.pending_checkpoints.append(_pending())

    async def fake_check(*a, **k):
        return CheckReport(verdicts=[])

    with patch("artemis.agents.checker.checker.run_checkpoint_check", side_effect=fake_check):
        await spawn_pending_checkpoints(ctx, _make_state(), "sid")
        await ctx.checkpoint_tasks[GOAL_KEY].task

    assert running.cancelled()
    superseded = [r for r in read_ledger(tmp_path) if r["status"] == "superseded"]
    assert superseded and superseded[0]["attempt_id"] == f"{GOAL_KEY}#1"


# --- §8.4: harvest only takes done(); applicability gates side effects ---------------


@pytest.mark.asyncio
async def test_harvest_only_takes_done_tasks(tmp_path):
    _write_plan(tmp_path)
    ctx = _make_ctx(tmp_path)

    async def hang():
        await asyncio.sleep(3600)

    running = asyncio.create_task(hang())
    await asyncio.sleep(0)
    ctx.checkpoint_tasks["running"] = CheckpointRun(
        attempt_id="running#1", task=running, checkpoint=_pending()
    )
    ctx.checkpoint_tasks[GOAL_KEY] = _done_run(
        ctx, CheckReport(verdicts=[_verdict("verify", "passed")])
    )

    findings = harvest_finished_checkpoints(ctx, _make_state())
    assert findings == []
    # The running attempt is untouched (never awaited, never removed)
    assert "running" in ctx.checkpoint_tasks
    assert GOAL_KEY not in ctx.checkpoint_tasks
    assert not running.done()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running


@pytest.mark.asyncio
async def test_stale_verdict_is_ledger_only_when_subgoal_text_changed(tmp_path):
    # Plan no longer contains the anchored subgoal text
    _write_plan(tmp_path, "- [x] Create the alarm (rephrased)\n")
    ctx = _make_ctx(tmp_path)
    run = _done_run(ctx, CheckReport(verdicts=[_verdict("verify", "failed")]))

    findings = harvest_run(ctx, _make_state(), run, allow_side_effects=True)

    assert findings == []  # no repair injection
    assert ctx.checkpoint_repairs == {}
    records = read_ledger(tmp_path)
    assert records and records[0]["status"] == "failed"  # but still booked


# --- §8.5: verify FAIL repairs, bounded by quota -------------------------------------


@pytest.mark.asyncio
async def test_verify_fail_reverts_subgoal_and_injects_finding(tmp_path):
    plan_path = _write_plan(tmp_path)
    ctx = _make_ctx(tmp_path)
    run = _done_run(ctx, CheckReport(verdicts=[_verdict("verify", "failed")]))

    findings = harvest_run(ctx, _make_state(), run, allow_side_effects=True)

    content = plan_path.read_text(encoding="utf-8")
    assert f"- [/] {GOAL_TEXT}" in content  # [x] -> [/], forward-looking
    assert any("verify failed" in f for f in findings)
    assert any("fix it" in f for f in findings)
    assert ctx.checkpoint_repairs[GOAL_KEY] == 1


@pytest.mark.asyncio
async def test_verify_fail_beyond_repair_quota_keeps_failed_without_revert(tmp_path):
    plan_path = _write_plan(tmp_path)
    ctx = _make_ctx(tmp_path, checkpoint_max_repairs=2)
    ctx.checkpoint_repairs[GOAL_KEY] = 2
    run = _done_run(ctx, CheckReport(verdicts=[_verdict("verify", "failed")]))

    findings = harvest_run(ctx, _make_state(), run, allow_side_effects=True)

    assert findings == []
    assert f"- [x] {GOAL_TEXT}" in plan_path.read_text(encoding="utf-8")
    # The verdict is NOT rewritten: it stays failed in the ledger
    assert read_ledger(tmp_path)[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_verify_fail_after_user_stop_never_reverts(tmp_path):
    plan_path = _write_plan(tmp_path)
    ctx = _make_ctx(tmp_path)
    run = _done_run(ctx, CheckReport(verdicts=[_verdict("verify", "failed")]))

    harvest_run(ctx, _make_state(user_stop_requested=True), run, allow_side_effects=True)
    assert f"- [x] {GOAL_TEXT}" in plan_path.read_text(encoding="utf-8")


# --- §8.6: assert FAIL is a result, never a repair -----------------------------------


@pytest.mark.asyncio
async def test_assert_fail_is_ledger_only(tmp_path):
    plan_path = _write_plan(tmp_path)
    ctx = _make_ctx(tmp_path)
    run = _done_run(ctx, CheckReport(verdicts=[_verdict("assert", "failed")]))

    findings = harvest_run(ctx, _make_state(), run, allow_side_effects=True)

    assert findings == []
    assert f"- [x] {GOAL_TEXT}" in plan_path.read_text(encoding="utf-8")
    assert ctx.assert_halt is False
    assert read_ledger(tmp_path)[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_assert_fail_halt_policy_latches_halt(tmp_path):
    _write_plan(tmp_path)
    ctx = _make_ctx(tmp_path, assert_failure_policy="halt")
    run = _done_run(ctx, CheckReport(verdicts=[_verdict("assert", "failed")]))

    harvest_run(ctx, _make_state(), run, allow_side_effects=True)
    assert ctx.assert_halt is True


# --- Retired check items: late verdicts are ledger-only ------------------------------


RETIRED_VERIFY = ("verify", "the alarm list shows 7:30 AM")
RETIRED_ASSERT = ("assert", "a toast appeared")


def _last_done_event(ctx):
    calls = [
        c for c in ctx.data_engine._publish.call_args_list if c.args[1].get("status") == "done"
    ]
    return calls[-1].args[1]


@pytest.mark.asyncio
async def test_late_failing_verify_for_retired_line_is_ledger_only(tmp_path):
    """The user retired the verify line but kept the milestone: a late failing
    verdict for it neither reopens the subgoal nor produces findings/headline,
    yet the ledger still books it and the done event reports it as retired."""
    plan_path = _write_plan(tmp_path, PLAN_WITH_CHECKS.replace(VERIFY_LINE, ""))
    ctx = _make_ctx(tmp_path)
    ctx.guidance_retired_checks = {RETIRED_VERIFY}
    run = _done_run(ctx, CheckReport(verdicts=[_verdict("verify", "failed")]))

    findings = harvest_run(ctx, _make_state(), run, allow_side_effects=True)

    assert findings == []
    assert f"- [x] {GOAL_TEXT}" in plan_path.read_text(encoding="utf-8")
    assert "- finding:" not in plan_path.read_text(encoding="utf-8")
    assert ctx.checkpoint_repairs == {}
    assert getattr(ctx, "checker_findings", {}) == {}
    records = read_ledger(tmp_path)
    assert [(r["kind"], r["status"]) for r in records] == [("verify", "failed")]
    event = _last_done_event(ctx)
    assert event["applicable"] is False
    assert event["reverted"] is False
    assert event["retired"] == [{"kind": "verify", "item_text": RETIRED_VERIFY[1]}]
    assert [v["status"] for v in event["verdicts"]] == ["failed"]


@pytest.mark.asyncio
async def test_failed_assert_for_retired_line_never_latches_halt(tmp_path):
    _write_plan(tmp_path, PLAN_WITH_CHECKS.replace(ASSERT_LINE, ""))
    ctx = _make_ctx(tmp_path, assert_failure_policy="halt")
    ctx.guidance_retired_checks = {RETIRED_ASSERT}
    run = _done_run(ctx, CheckReport(verdicts=[_verdict("assert", "failed")]))

    harvest_run(ctx, _make_state(), run, allow_side_effects=True)

    assert ctx.assert_halt is False
    assert read_ledger(tmp_path)[0]["status"] == "failed"  # still booked
    assert _last_done_event(ctx)["applicable"] is False


@pytest.mark.asyncio
async def test_non_retired_sibling_verdict_still_takes_effect(tmp_path):
    """Same report: the retired verify is dropped, the live assert still halts
    and the live verify sibling still repairs."""
    plan_path = _write_plan(tmp_path, PLAN_WITH_CHECKS.replace(VERIFY_LINE, ""))
    ctx = _make_ctx(tmp_path, assert_failure_policy="halt")
    ctx.guidance_retired_checks = {RETIRED_VERIFY}
    run = _done_run(
        ctx,
        CheckReport(verdicts=[_verdict("verify", "failed"), _verdict("assert", "failed")]),
    )

    findings = harvest_run(ctx, _make_state(), run, allow_side_effects=True)

    assert findings == []  # the retired verify produces no repair finding
    assert f"- [x] {GOAL_TEXT}" in plan_path.read_text(encoding="utf-8")
    assert ctx.assert_halt is True  # the live assert sibling still counts
    event = _last_done_event(ctx)
    assert event["applicable"] is True
    assert event["retired"] == [{"kind": "verify", "item_text": RETIRED_VERIFY[1]}]

    # And a live verify sibling next to a retired one still drives the repair,
    # with the headline naming only the live criterion.
    live_line = "  - verify: the alarm is enabled\n"
    plan_path = _write_plan(tmp_path, PLAN_WITH_CHECKS.replace(VERIFY_LINE, live_line))
    ctx = _make_ctx(tmp_path)
    ctx.guidance_retired_checks = {RETIRED_VERIFY}
    run = _done_run(
        ctx,
        CheckReport(
            verdicts=[
                _verdict("verify", "failed"),
                _verdict("verify", "failed", text="the alarm is enabled", evidence="toggle off"),
            ]
        ),
    )
    findings = harvest_run(ctx, _make_state(), run, allow_side_effects=True)
    content = plan_path.read_text(encoding="utf-8")
    assert f"- [/] {GOAL_TEXT}" in content
    assert "- finding: verify failed — 'the alarm is enabled'" in content
    assert RETIRED_VERIFY[1] not in " ".join(findings)
    assert any("the alarm is enabled" in f for f in findings)
    assert ctx.checker_finding_items[GOAL_KEY] == {("verify", "the alarm is enabled")}


@pytest.mark.asyncio
async def test_retiring_an_item_withdraws_its_registered_headline(tmp_path):
    """A headline registered by an earlier failing attempt disappears from the
    plan when the user guidance retires the item that produced it; a headline
    that still names a live item stays."""
    from artemis.graph.checkpoints import retire_check_items

    plan_path = _write_plan(tmp_path)
    ctx = _make_ctx(tmp_path)
    run = _done_run(ctx, CheckReport(verdicts=[_verdict("verify", "failed")]))
    harvest_run(ctx, _make_state(), run, allow_side_effects=True)
    assert "- finding: verify failed" in plan_path.read_text(encoding="utf-8")

    retire_check_items(ctx, [RETIRED_ASSERT])  # unrelated item: headline stays
    assert "- finding: verify failed" in plan_path.read_text(encoding="utf-8")
    assert ctx.guidance_retired_checks == {RETIRED_ASSERT}

    retire_check_items(ctx, [RETIRED_VERIFY])
    assert "- finding:" not in plan_path.read_text(encoding="utf-8")
    assert ctx.checker_findings == {}
    assert ctx.checker_finding_items == {}
    assert ctx.guidance_retired_checks == {RETIRED_ASSERT, RETIRED_VERIFY}

    # The plan-write path reaches the same helper: dropping a waived line
    # after a headline was registered removes the headline on that write.
    plan_path = _write_plan(tmp_path)
    ctx = _make_ctx(tmp_path, disable_planner_validation=True)
    ctx.guidance_unprotected_checks = set()
    ctx.guidance_retired_checks = set()
    run = _done_run(ctx, CheckReport(verdicts=[_verdict("verify", "failed")]))
    harvest_run(ctx, _make_state(), run, allow_side_effects=True)
    with_finding = plan_path.read_text(encoding="utf-8")
    assert "- finding: verify failed" in with_finding
    _guidance_arrives(ctx, with_finding)
    without_verify = with_finding.replace(VERIFY_LINE, "")
    on_disk = await _plan_write(tmp_path, ctx, with_finding, without_verify)
    assert VERIFY_LINE not in on_disk
    assert "- finding:" not in on_disk
    assert ctx.guidance_retired_checks == {RETIRED_VERIFY}


@pytest.mark.asyncio
async def test_ledger_is_append_only_fail_then_pass_both_present(tmp_path):
    _write_plan(tmp_path)
    ctx = _make_ctx(tmp_path)
    run1 = _done_run(ctx, CheckReport(verdicts=[_verdict("assert", "failed")]))
    harvest_run(ctx, _make_state(), run1, allow_side_effects=True)
    run2 = _done_run(
        ctx,
        CheckReport(verdicts=[_verdict("assert", "passed")]),
        attempt_id=f"{GOAL_KEY}#2",
    )
    harvest_run(ctx, _make_state(), run2, allow_side_effects=True)

    statuses = [r["status"] for r in read_ledger(tmp_path)]
    assert statuses == ["failed", "passed"]  # first failure permanently retained


# --- §8.7 / §8.13: timeouts and fail-open axis separation ----------------------------


@pytest.mark.asyncio
async def test_timeout_records_inconclusive_not_passed(tmp_path):
    _write_plan(tmp_path)
    ctx = _make_ctx(tmp_path)
    run = _done_run(ctx, TimeoutError())

    findings = harvest_run(ctx, _make_state(), run, allow_side_effects=True)

    assert findings == []  # released (fail-open): no repair loop
    records = read_ledger(tmp_path)
    assert {r["status"] for r in records} == {"inconclusive"}
    assert all(r["status"] != "passed" for r in records)


# --- Verify-finding four-layer persistence -------------------------------------------


def _checker_note_path(tmp_path):
    return tmp_path / "notes" / f"{checker_note_key(GOAL_KEY)}.md"


@pytest.mark.asyncio
async def test_verify_fail_writes_finding_line_and_checker_note(tmp_path):
    plan_path = _write_plan(tmp_path)
    ctx = _make_ctx(tmp_path)
    run = _done_run(ctx, CheckReport(verdicts=[_verdict("verify", "failed")]))

    harvest_run(ctx, _make_state(), run, allow_side_effects=True)

    content = plan_path.read_text(encoding="utf-8")
    lines = content.splitlines()
    # Standing headline pinned directly under the (now reverted) subgoal,
    # naming the failed criterion and pointing at the detail note.
    idx = lines.index(f"- [/] {GOAL_TEXT}")
    assert lines[idx + 1].startswith("  - finding: verify failed")
    assert checker_note_key(GOAL_KEY) in lines[idx + 1]

    # Layer 3: the system note carries the full criterion/evidence/suggestion.
    note = _checker_note_path(tmp_path).read_text(encoding="utf-8")
    assert "the alarm list shows 7:30 AM" in note
    assert "concrete evidence" in note
    assert "fix it" in note


@pytest.mark.asyncio
async def test_finding_line_regrows_on_model_plan_write(tmp_path):
    plan_path = _write_plan(tmp_path)
    ctx = _make_ctx(tmp_path)
    run = _done_run(ctx, CheckReport(verdicts=[_verdict("verify", "failed")]))
    harvest_run(ctx, _make_state(), run, allow_side_effects=True)
    assert "- finding:" in plan_path.read_text(encoding="utf-8")

    # The model rewrites the plan WITHOUT the finding line...
    clean_rewrite = PLAN_WITH_CHECKS.replace(f"- [x] {GOAL_TEXT}", f"- [/] {GOAL_TEXT}")

    async def fake_invoke(tool, args, tool_call_id, state, record_trace=None):
        plan_path.write_text(args["content"], encoding="utf-8")
        return "Success"

    mock_tool = MagicMock()
    mock_tool.name = "save_note"
    mock_tool.description = "save"

    with patch("artemis.graph.graph.invoke_tool_with_injection", side_effect=fake_invoke):
        wrapped = wrap_note_tool(ctx, mock_tool)
        await wrapped.ainvoke({"key": "task_plan", "content": clean_rewrite, "tool_call_id": "t1"})

    # ...and the deterministic projection grows it back on the write path.
    assert "- finding: verify failed" in plan_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_finding_line_removed_after_verify_passes_note_kept(tmp_path):
    plan_path = _write_plan(tmp_path)
    ctx = _make_ctx(tmp_path)
    run1 = _done_run(ctx, CheckReport(verdicts=[_verdict("verify", "failed")]))
    harvest_run(ctx, _make_state(), run1, allow_side_effects=True)
    assert "- finding:" in plan_path.read_text(encoding="utf-8")

    run2 = _done_run(
        ctx,
        CheckReport(verdicts=[_verdict("verify", "passed")]),
        attempt_id=f"{GOAL_KEY}#2",
    )
    harvest_run(ctx, _make_state(), run2, allow_side_effects=True)

    # Layer 2 headline retired; layer 3 note stays on disk with the full
    # per-attempt repair log (fail then pass).
    assert "- finding:" not in plan_path.read_text(encoding="utf-8")
    note = _checker_note_path(tmp_path).read_text(encoding="utf-8")
    assert f"attempt {GOAL_KEY}#1 — verify failed" in note
    assert f"attempt {GOAL_KEY}#2 — verify passed" in note


@pytest.mark.asyncio
async def test_finding_line_removed_on_repair_quota_exhaustion(tmp_path):
    plan_path = _write_plan(tmp_path)
    ctx = _make_ctx(tmp_path, checkpoint_max_repairs=1)
    run1 = _done_run(ctx, CheckReport(verdicts=[_verdict("verify", "failed")]))
    harvest_run(ctx, _make_state(), run1, allow_side_effects=True)
    assert "- finding:" in plan_path.read_text(encoding="utf-8")

    # Re-complete, fail again: quota (1) is exhausted -> finding settled.
    plan_path.write_text(
        plan_path.read_text(encoding="utf-8").replace(f"- [/] {GOAL_TEXT}", f"- [x] {GOAL_TEXT}"),
        encoding="utf-8",
    )
    run2 = _done_run(
        ctx,
        CheckReport(verdicts=[_verdict("verify", "failed")]),
        attempt_id=f"{GOAL_KEY}#2",
    )
    harvest_run(ctx, _make_state(), run2, allow_side_effects=True)

    content = plan_path.read_text(encoding="utf-8")
    assert "- finding:" not in content
    assert f"- [x] {GOAL_TEXT}" in content  # no further revert either


@pytest.mark.asyncio
async def test_checker_note_prefix_writes_rejected_by_wrappers(tmp_path):
    ctx = _make_ctx(tmp_path)
    save_tool = MagicMock()
    save_tool.name = "save_note"
    save_tool.description = "save"
    update_tool = MagicMock()
    update_tool.name = "update_note"
    update_tool.description = "update"

    with patch("artemis.graph.graph.invoke_tool_with_injection", new_callable=AsyncMock) as inv:
        wrapped_save = wrap_note_tool(ctx, save_tool)
        result = await wrapped_save.ainvoke(
            {"key": checker_note_key(GOAL_KEY), "content": "spoof", "tool_call_id": "t1"}
        )
        assert "reserved" in str(result.content)

        wrapped_update = wrap_update_note_tool(ctx, update_tool)
        result2 = await wrapped_update.ainvoke(
            {
                "key": "checker:abc",
                "target": "a",
                "replacement": "b",
                "tool_call_id": "t2",
            }
        )
        assert "reserved" in str(result2.content)

    inv.assert_not_awaited()
    assert is_checker_note_key("checker-123") and is_checker_note_key("checker:123")
    assert not is_checker_note_key("checkerboard_notes") and not is_checker_note_key("notes")


@pytest.mark.asyncio
async def test_exception_fail_open_releases_but_books_inconclusive(tmp_path):
    plan_path = _write_plan(tmp_path)
    ctx = _make_ctx(tmp_path)
    run = _done_run(ctx, RuntimeError("checker blew up"))

    findings = harvest_run(ctx, _make_state(), run, allow_side_effects=True)

    # Release: no revert, no finding
    assert findings == []
    assert f"- [x] {GOAL_TEXT}" in plan_path.read_text(encoding="utf-8")
    # Verdict axis: inconclusive, never rewritten to passed
    for r in read_ledger(tmp_path):
        assert r["status"] == "inconclusive"
        assert "checker blew up" in r["evidence"]


# --- Attempt stream transcripts: bounded, append-only, replayable ------------------------


def _seg(exec_id, text, role="answer", when=1.0):
    return {"execution_id": exec_id, "role": role, "when": when, "text": text}


def test_clamp_keeps_short_transcripts_verbatim():
    from artemis.graph.checkpoints import clamp_stream_segments

    segs = [_seg("a", "hello", "thought"), _seg("b", "world")]
    out, dropped = clamp_stream_segments(segs, limit=20)
    assert out == segs and dropped == 0
    assert out[0] is not segs[0]  # copies, the input is never mutated


def test_clamp_cuts_the_middle_once_and_keeps_head_and_tail():
    from artemis.graph.checkpoints import clamp_stream_segments

    segs = [
        _seg("a", "A" * 40, "thought", when=1.0),
        _seg("b", "B" * 40, when=2.0),
        _seg("c", "C" * 40, when=3.0),
        _seg("d", "D" * 40, when=4.0),
    ]
    out, dropped = clamp_stream_segments(segs, limit=100)

    assert dropped == 60
    # head: 50 chars = all of a + 10 of b; tail: 50 chars = 10 of c + all of d
    assert [s["execution_id"] for s in out] == ["a", "b", "c", "d"]
    assert out[0]["text"] == "A" * 40 and out[0]["role"] == "thought"
    assert out[1]["text"] == "B" * 10 + "\n…[60 chars truncated]…\n"
    assert out[2]["text"] == "C" * 10
    assert out[3]["text"] == "D" * 40
    joined = "".join(s["text"] for s in out)
    assert joined.count("chars truncated") == 1
    assert len(joined) == 100 + len("\n…[60 chars truncated]…\n")
    # Timestamps and the other fields ride along untouched.
    assert [s["when"] for s in out] == [1.0, 2.0, 3.0, 4.0]


def test_clamp_drops_segments_that_fall_entirely_into_the_cut():
    from artemis.graph.checkpoints import clamp_stream_segments

    segs = [_seg("a", "A" * 10), _seg("mid", "M" * 100), _seg("z", "Z" * 10)]
    out, dropped = clamp_stream_segments(segs, limit=20)
    assert dropped == 100
    assert [s["execution_id"] for s in out] == ["a", "z"]
    assert out[0]["text"] == "A" * 10
    assert out[1]["text"] == "\n…[100 chars truncated]…\n" + "Z" * 10


def test_clamp_single_huge_segment_keeps_both_ends():
    from artemis.graph.checkpoints import clamp_stream_segments

    out, dropped = clamp_stream_segments([_seg("a", "H" * 30 + "M" * 100 + "T" * 30)], limit=60)
    assert dropped == 100
    assert len(out) == 1
    assert out[0]["text"] == "H" * 30 + "\n…[100 chars truncated]…\n" + "T" * 30


def test_stream_records_round_trip_and_carry_truncation_flags(tmp_path):
    from artemis.graph.checkpoints import (
        STREAM_TEXT_LIMIT,
        append_attempt_stream_record,
        read_attempt_streams,
        streams_path,
    )

    assert STREAM_TEXT_LIMIT == 20_000
    append_attempt_stream_record(
        tmp_path,
        {"attempt_id": "abc#1", "trace_id": "t1", "segments": [_seg("a", "short")]},
    )
    append_attempt_stream_record(
        tmp_path,
        {"attempt_id": "abc#2", "trace_id": "t2", "segments": [_seg("b", "x" * 50)]},
        limit=10,
    )
    with streams_path(tmp_path).open("a", encoding="utf-8") as f:
        f.write("not json\n\n")

    records = read_attempt_streams(tmp_path)
    assert [r["attempt_id"] for r in records] == ["abc#1", "abc#2"]
    assert records[0]["truncated"] is False and records[0]["dropped_chars"] == 0
    assert records[0]["segments"] == [_seg("a", "short")]
    assert isinstance(records[0]["ts"], float)
    assert records[1]["truncated"] is True and records[1]["dropped_chars"] == 40
    assert records[1]["segments"][0]["text"] == "x" * 5 + "\n…[40 chars truncated]…\n" + "x" * 5
    assert read_attempt_streams(tmp_path / "nowhere") == []
