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
    harvest_finished_checkpoints,
    harvest_run,
    queue_checkpoints,
    read_ledger,
    spawn_pending_checkpoints,
)
from artemis.graph.graph import execution_check_node, wrap_note_tool
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
    ctx.task_plan_content_before = None
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

    async def fake_check(ctx_arg, check_items, anchor, goal, subgoal_text):
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
async def test_record_step_on_planner_rejected_turn(tmp_path):
    """Every operator turn leaves a step record — the planner-rejected turn is
    recorded too, tagged planner_rejected."""
    _write_plan(tmp_path)
    ctx = _make_ctx(tmp_path)
    ctx.data_engine.record_step.return_value = "rejected-step"
    rejection = asyncio.get_event_loop().create_future()
    rejection.set_result({"status": "failed", "feedback": "bad plan"})
    ctx.planner_task = rejection
    state = _make_state(operator_raw_data=_raw_data())

    with patch("artemis.graph.graph._get_active_subgoal_hashes", return_value=("h", None)):
        update = await execution_check_node(state, ctx)

    assert update["checker_success"] is False
    assert update["current_step_id"] == "rejected-step"
    extra = ctx.data_engine.record_step.call_args.kwargs["extra_metadata"]
    assert extra.get("planner_rejected") is True


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
