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

"""Checker process visibility: every visible step of the Checker (checkpoint
spawn/harvest, final review, run outcome) is published as a ``checker_event``
on the DataEngine bus, and the persisted ledger carries enough context
(subgoal text) for the admin console to rebuild the same view offline."""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from artemis.agents.checker.checker import CheckReport, CheckVerdict
from artemis.context import ArtemisContext, ExecutionSetup
from artemis.graph.checkpoints import (
    CHECKER_EVENT,
    CheckpointRun,
    PendingCheckpoint,
    harvest_run,
    publish_checker_event,
    read_ledger,
    spawn_pending_checkpoints,
)
from artemis.graph.graph import exit_settlement_node
from artemis.graph.state import State
from artemis.utils.plan_grammar import CheckItem, subgoal_hash

GOAL_TEXT = "Create the alarm"
GOAL_KEY = subgoal_hash(GOAL_TEXT)
PLAN = (
    f"- [x] {GOAL_TEXT}\n  - verify: the alarm list shows 7:30 AM\n  - assert: a toast appeared\n"
)


def _make_ctx(tmp_path, **setup_kwargs):
    setup_kwargs.setdefault("disable_checker", False)
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
    return ctx


def _make_state(**overrides):
    state = MagicMock(spec=State)
    state.initial_goal = "the user goal"
    state.user_stop_requested = False
    for k, v in overrides.items():
        setattr(state, k, v)
    return state


def _write_plan(tmp_path, content=PLAN):
    notes = tmp_path / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "task_plan.md").write_text(content, encoding="utf-8")


def _pending():
    return PendingCheckpoint(
        checkpoint_id=GOAL_KEY,
        subgoal_text=GOAL_TEXT,
        check_items=(
            CheckItem(
                kind="verify",
                text="the alarm list shows 7:30 AM",
                when="on_complete",
                parent_key=GOAL_KEY,
            ),
            CheckItem(
                kind="assert", text="a toast appeared", when="on_complete", parent_key=GOAL_KEY
            ),
        ),
        plan_text=PLAN,
        trigger_ts=time.time(),
    )


def _events(ctx, event: str | None = None) -> list[dict]:
    out = []
    for call in ctx.data_engine._publish.call_args_list:
        if call.args[0] != CHECKER_EVENT:
            continue
        payload = call.args[1]
        if event is None or payload.get("event") == event:
            out.append(payload)
    return out


def test_publish_is_best_effort_without_engine():
    ctx = MagicMock(spec=ArtemisContext)
    ctx.data_engine = None
    publish_checker_event(ctx, {"event": "attempt_started"})  # must not raise

    ctx.data_engine = MagicMock()
    ctx.data_engine._publish.side_effect = RuntimeError("bus down")
    publish_checker_event(ctx, {"event": "attempt_started"})  # swallowed


@pytest.mark.asyncio
async def test_spawn_passes_attempt_id_to_checker(tmp_path):
    """The announcement lives inside the checker (it needs the trace id), so
    spawn must hand the attempt id over."""
    _write_plan(tmp_path)
    ctx = _make_ctx(tmp_path)
    ctx.pending_checkpoints.append(_pending())
    never = asyncio.Event()
    seen: dict = {}

    async def hang(*a, **k):
        seen.update(k)
        await never.wait()

    with patch("artemis.agents.checker.checker.run_checkpoint_check", side_effect=hang):
        await spawn_pending_checkpoints(ctx, _make_state(), anchor_step_id="step-7")
        await asyncio.sleep(0)

    assert seen.get("attempt_id") == f"{GOAL_KEY}#1"
    assert _events(ctx, "attempt_started") == []

    for run in ctx.checkpoint_tasks.values():
        run.task.cancel()
        try:
            await run.task
        except (asyncio.CancelledError, Exception):
            pass


@pytest.mark.asyncio
async def test_checker_entry_announces_attempt_with_trace_id(tmp_path):
    """attempt_started is published from inside the traced checker scope and
    carries the Checker's own trace id (UI routing key for its stream/tools),
    the anchor and the declared items."""
    from artemis.agents.checker.checker import run_checkpoint_check, run_final_check
    from artemis.graph.checkpoints import EvidenceAnchor

    _write_plan(tmp_path)
    ctx = _make_ctx(tmp_path)
    ctx.data_engine.get_step_number.return_value = 7
    empty_report = CheckReport(verdicts=[])
    anchor = EvidenceAnchor(anchor_step_id="step-7", trigger_ts=time.time(), plan_text=PLAN)

    with (
        patch(
            "artemis.agents.checker.checker._run_check_loop", AsyncMock(return_value=empty_report)
        ),
        patch("artemis.agents.checker.checker.build_checker_tools", return_value=[]),
        patch("artemis.agents.checker.checker._format_history", return_value=""),
        patch(
            "artemis.agents.checker.checker._capture_final_screen",
            AsyncMock(return_value=(None, "")),
        ),
    ):
        await run_checkpoint_check(
            ctx,
            check_items=_pending().check_items,
            anchor=anchor,
            goal="g",
            subgoal_text=GOAL_TEXT,
            attempt_id=f"{GOAL_KEY}#1",
        )
        await run_final_check(
            ctx,
            goal="g",
            plan_text=PLAN,
            ledger=[],
            check_items=_pending().check_items,
            attempt_id="final#1",
        )

    started = _events(ctx, "attempt_started")
    assert [e["phase"] for e in started] == ["checkpoint", "final"]
    cp, fin = started
    assert cp["attempt_id"] == f"{GOAL_KEY}#1"
    assert cp["checkpoint_id"] == GOAL_KEY
    assert cp["subgoal_text"] == GOAL_TEXT
    assert cp["anchor_step_id"] == "step-7"
    assert [i["kind"] for i in cp["items"]] == ["verify", "assert"]
    assert cp["trace_id"]  # set by the @trace decorator scope
    assert "timestamp" in cp and "ts" in cp
    assert fin["attempt_id"] == "final#1"
    assert fin["checkpoint_id"] == "final"
    assert fin["trace_id"]


@pytest.mark.asyncio
async def test_harvest_publishes_verdicts_findings_and_ledger_carries_subgoal(tmp_path):
    _write_plan(tmp_path)
    ctx = _make_ctx(tmp_path)
    report = CheckReport(
        verdicts=[
            CheckVerdict(
                item_text="the alarm list shows 7:30 AM",
                kind="verify",
                status="failed",
                evidence="list shows 7:00 AM",
                suggestion="open the alarm and set 7:30",
            ),
            CheckVerdict(
                item_text="a toast appeared", kind="assert", status="passed", evidence="toast seen"
            ),
        ]
    )
    task = asyncio.get_event_loop().create_future()
    task.set_result(report)
    run = CheckpointRun(attempt_id=f"{GOAL_KEY}#1", task=task, checkpoint=_pending())

    findings = harvest_run(ctx, _make_state(), run, allow_side_effects=True, anchor_step_id="s1")

    finished = _events(ctx, "attempt_finished")
    assert len(finished) == 1
    ev = finished[0]
    assert ev["status"] == "done"
    assert ev["phase"] == "checkpoint"
    assert ev["subgoal_text"] == GOAL_TEXT
    assert [v["status"] for v in ev["verdicts"]] == ["failed", "passed"]
    assert ev["verdicts"][0]["suggestion"] == "open the alarm and set 7:30"
    assert ev["findings"] == findings and findings  # the same strings the Operator sees
    assert ev["reverted"] is True
    assert ev["applicable"] is True
    assert ev["repairs_used"] == 1

    # The ledger (offline source for the UI backfill) names the subgoal too.
    for rec in read_ledger(tmp_path):
        assert rec["subgoal_text"] == GOAL_TEXT


@pytest.mark.asyncio
async def test_harvest_superseded_and_error_publish_terminal_status(tmp_path):
    _write_plan(tmp_path)
    ctx = _make_ctx(tmp_path)

    cancelled = asyncio.get_event_loop().create_future()
    cancelled.cancel()
    harvest_run(
        ctx,
        _make_state(),
        CheckpointRun(attempt_id=f"{GOAL_KEY}#1", task=cancelled, checkpoint=_pending()),
        allow_side_effects=False,
    )
    errored = asyncio.get_event_loop().create_future()
    errored.set_exception(TimeoutError())
    harvest_run(
        ctx,
        _make_state(),
        CheckpointRun(attempt_id=f"{GOAL_KEY}#2", task=errored, checkpoint=_pending()),
        allow_side_effects=True,
    )

    finished = _events(ctx, "attempt_finished")
    assert [e["status"] for e in finished] == ["superseded", "error"]
    assert all(v["status"] == "superseded" for v in finished[0]["verdicts"])
    assert all(v["status"] == "inconclusive" for v in finished[1]["verdicts"])
    assert "timed out" in finished[1]["error"]


@pytest.mark.asyncio
async def test_final_review_and_run_outcome_are_published(tmp_path):
    _write_plan(tmp_path)
    ctx = _make_ctx(tmp_path, disable_midway_checks=True)
    report = CheckReport(
        verdicts=[
            CheckVerdict(
                item_text="the alarm list shows 7:30 AM",
                kind="verify",
                status="passed",
                evidence="final screen shows 7:30 AM",
            ),
            CheckVerdict(
                item_text="a toast appeared",
                kind="assert",
                status="inconclusive",
                evidence="no history evidence",
            ),
        ]
    )
    with patch("artemis.graph.graph.run_final_check", AsyncMock(return_value=report)):
        update = await exit_settlement_node(_make_state(), ctx)

    assert update["exit_settlement_route"] == "end"
    finished = _events(ctx, "attempt_finished")
    assert finished and finished[0]["phase"] == "final"
    assert finished[0]["attempt_id"] == "final#1"
    assert finished[0]["status"] == "done"
    assert finished[0]["route"] == "end"
    assert [v["status"] for v in finished[0]["verdicts"]] == ["passed", "inconclusive"]

    outcome = _events(ctx, "run_outcome")
    assert len(outcome) == 1
    assert outcome[0]["task_status"] == "completed"
    assert outcome[0]["tests"]["passed"] == 1
    assert outcome[0]["tests"]["inconclusive"] == 1
    assert outcome[0]["tests"]["failed"] == 0
    assert outcome[0]["last_findings"] == []

    # Persisted mirror of the same event for the UI backfill endpoint.
    persisted = json.loads((tmp_path / "run_outcome.json").read_text(encoding="utf-8"))
    assert persisted["task_status"] == "completed"
    assert all(r["subgoal_text"] for r in read_ledger(tmp_path))


@pytest.mark.asyncio
async def test_final_review_bounce_back_publishes_continue_route(tmp_path):
    _write_plan(tmp_path)
    ctx = _make_ctx(tmp_path, disable_midway_checks=True, final_check_max_attempts=3)
    report = CheckReport(
        verdicts=[
            CheckVerdict(
                item_text="the alarm list shows 7:30 AM",
                kind="verify",
                status="failed",
                evidence="list is empty",
                suggestion="create it",
            )
        ],
        unmet_subgoals=[GOAL_TEXT],
    )
    with patch("artemis.graph.graph.run_final_check", AsyncMock(return_value=report)):
        update = await exit_settlement_node(_make_state(), ctx)

    assert update["exit_settlement_route"] == "continue"
    finished = _events(ctx, "attempt_finished")
    assert finished[0]["route"] == "continue"
    assert finished[0]["unmet_subgoals"] == [GOAL_TEXT]
    assert finished[0]["findings"] == update["operator_feedback"]
    # No run outcome while the loop continues.
    assert _events(ctx, "run_outcome") == []
