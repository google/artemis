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

"""Exit settlement tests: gate routing, settlement/final-review decoupling,
settlement timeout, when-semantics at the exit, run outcome, and the four
switch-combination matrix."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from artemis.agents.checker.checker import CheckReport, CheckVerdict
from artemis.context import ArtemisContext, ExecutionSetup
from artemis.graph.checkpoints import (
    CheckpointRun,
    PendingCheckpoint,
    append_ledger_record,
    compute_run_outcome,
    read_ledger,
    resolve_item_status,
    settle_all_checkpoints,
)
from artemis.graph.graph import (
    convergence_gate,
    exit_settlement_gate,
    exit_settlement_node,
)
from artemis.graph.state import State
from artemis.sdk.agent import attach_test_summary, resolve_trace_suffix
from artemis.utils.plan_grammar import CheckItem, parse_plan, subgoal_hash

DONE_PLAN_WITH_CHECKS = (
    "- [x] Create alarm\n"
    "  - verify: alarm exists\n"
    "  - assert: toast appeared\n"
    "- assert@end: no crash dialog\n"
)

DONE_PLAN_NO_CHECKS = "- [x] Create alarm\n"


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
    return ctx


def _make_state(**overrides):
    state = MagicMock(spec=State)
    state.initial_goal = "the user goal"
    state.user_stop_requested = False
    state.checker_success = True
    for k, v in overrides.items():
        setattr(state, k, v)
    return state


def _write_plan(tmp_path, content):
    notes = tmp_path / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "task_plan.md").write_text(content, encoding="utf-8")


def _pending_checkpoint(text="Create alarm"):
    return PendingCheckpoint(
        checkpoint_id=subgoal_hash(text),
        subgoal_text=text,
        check_items=(
            CheckItem(
                kind="verify",
                when="on_complete",
                text="alarm exists",
                parent_key=subgoal_hash(text),
            ),
        ),
        plan_text=f"- [x] {text}\n",
        trigger_ts=0.0,
    )


def _hanging_run(cid="cid"):
    async def hang():
        await asyncio.sleep(3600)

    task = asyncio.create_task(hang())
    return CheckpointRun(attempt_id=f"{cid}#1", task=task, checkpoint=_pending_checkpoint())


# --- §8.8: gate routing — settlement decoupled from final review ---------------------


def test_gate_routes_settlement_when_tasks_pending_even_with_final_off(tmp_path):
    _write_plan(tmp_path, DONE_PLAN_NO_CHECKS)
    ctx = _make_ctx(tmp_path, disable_final_check=True)
    ctx.checkpoint_tasks["cid"] = MagicMock()
    assert convergence_gate(_make_state(), ctx) == "exit_settlement"


def test_gate_routes_empty_plan_to_final_review_when_enabled(tmp_path):
    _write_plan(tmp_path, "just prose, no checkboxes\n")
    ctx = _make_ctx(tmp_path)  # final on (enabled master, default gates)
    assert convergence_gate(_make_state(), ctx) == "exit_settlement"


def test_gate_ends_directly_when_nothing_check_related(tmp_path):
    _write_plan(tmp_path, DONE_PLAN_NO_CHECKS)
    ctx = _make_ctx(tmp_path, disable_checker=True)
    assert convergence_gate(_make_state(), ctx) == "end"


def test_gate_check_items_force_settlement_even_all_switches_off(tmp_path):
    """Combination row 1 (off/off): declared items still get settled (booked as
    unchecked) instead of silently vanishing."""
    _write_plan(tmp_path, DONE_PLAN_WITH_CHECKS)
    ctx = _make_ctx(tmp_path, disable_checker=True)
    assert convergence_gate(_make_state(), ctx) == "exit_settlement"


def test_gate_incomplete_plan_continues(tmp_path):
    _write_plan(tmp_path, "- [/] Create alarm\n  - verify: alarm exists\n")
    ctx = _make_ctx(tmp_path)
    assert convergence_gate(_make_state(), ctx) == "continue"


def test_gate_assert_halt_terminates_mid_plan(tmp_path):
    _write_plan(tmp_path, "- [/] Create alarm\n  - assert: toast appeared\n")
    ctx = _make_ctx(tmp_path)
    ctx.assert_halt = True
    assert convergence_gate(_make_state(), ctx) == "exit_settlement"


@pytest.mark.asyncio
async def test_settlement_runs_phase_one_only_when_final_disabled(tmp_path):
    _write_plan(tmp_path, DONE_PLAN_WITH_CHECKS)
    ctx = _make_ctx(tmp_path, disable_final_check=True)
    report = CheckReport(
        verdicts=[
            CheckVerdict(item_text="alarm exists", kind="verify", status="passed", evidence="e")
        ]
    )
    fut = asyncio.get_event_loop().create_future()
    fut.set_result(report)
    cid = subgoal_hash("Create alarm")
    ctx.checkpoint_tasks[cid] = CheckpointRun(
        attempt_id=f"{cid}#1", task=fut, checkpoint=_pending_checkpoint()
    )

    with patch("artemis.graph.graph.run_final_check", new=AsyncMock()) as final_mock:
        update = await exit_settlement_node(_make_state(), ctx)

    final_mock.assert_not_awaited()
    assert update["exit_settlement_route"] == "end"
    # Phase one still booked the attempt and produced the summary
    assert any(r["status"] == "passed" for r in read_ledger(tmp_path))
    assert update["run_outcome"]["tests"]["passed"] == 1
    assert ctx.checkpoint_tasks == {}


# --- §8.7: settlement timeout --------------------------------------------------------


@pytest.mark.asyncio
async def test_settlement_timeout_cancels_and_books_unchecked(tmp_path):
    _write_plan(tmp_path, DONE_PLAN_NO_CHECKS)
    ctx = _make_ctx(tmp_path, settlement_timeout=0.05)
    run = _hanging_run()
    ctx.checkpoint_tasks["cid"] = run

    await settle_all_checkpoints(ctx, _make_state())

    assert run.task.cancelled()
    records = read_ledger(tmp_path)
    assert records and all(r["status"] == "unchecked" for r in records)
    assert "settlement timeout" in records[0]["evidence"]


# --- §8.9: when-semantics at the exit ------------------------------------------------


@pytest.mark.asyncio
async def test_final_check_receives_ledger_and_at_end_items(tmp_path):
    _write_plan(tmp_path, DONE_PLAN_WITH_CHECKS)
    ctx = _make_ctx(tmp_path)
    append_ledger_record(
        tmp_path,
        {
            "attempt_id": "x#1",
            "checkpoint_id": "x",
            "item_text": "toast appeared",
            "kind": "assert",
            "when": "on_complete",
            "status": "failed",
            "evidence": "no toast in anchor history",
        },
    )

    captured = {}

    async def fake_final(ctx_arg, goal, plan_text, ledger, check_items):
        captured["goal"] = goal
        captured["ledger"] = ledger
        captured["items"] = list(check_items)
        return CheckReport(verdicts=[])

    with patch("artemis.graph.graph.run_final_check", side_effect=fake_final):
        await exit_settlement_node(_make_state(), ctx)

    # Final review audits the user's original goal, citing the ledger
    assert captured["goal"] == "the user goal"
    assert any(r["status"] == "failed" for r in captured["ledger"])
    whens = {(ci.text, ci.when) for ci in captured["items"]}
    assert ("no crash dialog", "at_end") in whens
    assert ("alarm exists", "on_complete") in whens


def test_on_complete_without_history_evidence_never_passes():
    """The 'current state satisfied but historical evidence missing' case: no
    substantive record -> unchecked; a final inconclusive verdict stays
    inconclusive. Neither ever resolves to passed."""
    assert resolve_item_status("assert", []) == "unchecked"
    inconclusive_only = [{"status": "inconclusive", "evidence": "evidence missing for past moment"}]
    assert resolve_item_status("assert", inconclusive_only) == "inconclusive"
    # First assert failure is permanent even if a later record passes
    assert resolve_item_status("assert", [{"status": "failed"}, {"status": "passed"}]) == "failed"
    # Verify: latest substantive verdict wins (repair-then-pass is passed)
    assert resolve_item_status("verify", [{"status": "failed"}, {"status": "passed"}]) == "passed"


# --- §8.14: run outcome + SDK wrap-up ------------------------------------------------


@pytest.mark.asyncio
async def test_assert_failure_yields_failed_tests_but_completed_status(tmp_path):
    _write_plan(tmp_path, DONE_PLAN_WITH_CHECKS)
    ctx = _make_ctx(tmp_path)
    report = CheckReport(
        verdicts=[
            CheckVerdict(item_text="alarm exists", kind="verify", status="passed", evidence="e"),
            CheckVerdict(item_text="toast appeared", kind="assert", status="failed", evidence="e"),
            CheckVerdict(item_text="no crash dialog", kind="assert", status="passed", evidence="e"),
        ]
    )

    with patch("artemis.graph.graph.run_final_check", new=AsyncMock(return_value=report)):
        update = await exit_settlement_node(_make_state(), ctx)

    # Assert failures never bounce the task back
    assert update["exit_settlement_route"] == "end"
    outcome = update["run_outcome"]
    assert outcome["task_status"] == "completed"
    assert outcome["tests"]["failed"] == 1
    assert outcome["tests"]["passed"] == 2
    assert outcome["tests"]["failed_items"][0]["item_text"] == "toast appeared"

    # SDK wrap-up: trace suffix distinguishes assertion failures from _PASS
    assert resolve_trace_suffix("completed", outcome) == "_TESTFAIL"
    assert resolve_trace_suffix("completed", {"tests": {"failed": 0}}) == "_PASS"
    assert resolve_trace_suffix("failed", outcome) == "_FAIL"

    # And the return structure surfaces the machine-readable summary
    wrapped = attach_test_summary("report text", outcome)
    assert wrapped["result"] == "report text"
    assert wrapped["test_summary"]["failed"] == 1
    as_dict = attach_test_summary({"answer": 42}, outcome)
    assert as_dict["answer"] == 42 and as_dict["test_summary"]["failed"] == 1


@pytest.mark.asyncio
async def test_verify_unmet_bounces_back_then_blocks_on_budget_exhaustion(tmp_path):
    _write_plan(tmp_path, DONE_PLAN_WITH_CHECKS)
    ctx = _make_ctx(tmp_path, final_check_max_attempts=2)
    failing = CheckReport(
        verdicts=[
            CheckVerdict(
                item_text="alarm exists",
                kind="verify",
                status="failed",
                evidence="e",
                suggestion="create it",
            )
        ],
        unmet_subgoals=["Create alarm"],
    )

    with patch("artemis.graph.graph.run_final_check", new=AsyncMock(return_value=failing)):
        first = await exit_settlement_node(_make_state(), ctx)
        assert first["exit_settlement_route"] == "continue"
        assert exit_settlement_gate(_make_state(exit_settlement_route="continue")) == "continue"
        # The unmet subgoal was reverted forward to in-progress
        plan = (tmp_path / "notes" / "task_plan.md").read_text(encoding="utf-8")
        assert "- [/] Create alarm" in plan
        assert any("verify failed" in f for f in first["operator_feedback"])

        # Re-complete and exhaust the budget
        _write_plan(tmp_path, DONE_PLAN_WITH_CHECKS)
        second = await exit_settlement_node(_make_state(), ctx)

    assert ctx.final_check_attempts == 2
    assert second["exit_settlement_route"] == "end"
    outcome = second["run_outcome"]
    assert outcome["task_status"] == "blocked"


def test_no_run_outcome_without_check_material(tmp_path):
    """§1.3: the test summary only exists when check items exist."""
    outcome = compute_run_outcome(parse_plan(DONE_PLAN_NO_CHECKS), [], verify_blocked=False)
    assert outcome.tests.passed + outcome.tests.failed == 0
    assert attach_test_summary("plain", outcome.model_dump()) == "plain"


# --- §8.15: continuous + user_stop + FAIL never loops back ---------------------------


@pytest.mark.asyncio
async def test_user_stop_final_fail_does_not_reenter_loop(tmp_path):
    plan = "- [x] [Loop:continuous] Monitor inbox\n  - verify: monitoring log is complete\n"
    _write_plan(tmp_path, plan)
    ctx = _make_ctx(tmp_path)
    failing = CheckReport(
        verdicts=[
            CheckVerdict(
                item_text="monitoring log is complete",
                kind="verify",
                status="failed",
                evidence="gaps in the log",
            )
        ]
    )

    with patch("artemis.graph.graph.run_final_check", new=AsyncMock(return_value=failing)):
        update = await exit_settlement_node(_make_state(user_stop_requested=True), ctx)

    assert update["exit_settlement_route"] == "end"
    assert update["run_outcome"]["task_status"] == "blocked"


# --- §8.16: four switch-combination matrix -------------------------------------------


def _fresh_final_report():
    return CheckReport(
        verdicts=[
            CheckVerdict(item_text="no crash dialog", kind="assert", status="passed", evidence="e")
        ]
    )


@pytest.mark.asyncio
async def test_matrix_off_off_reports_all_unchecked(tmp_path):
    _write_plan(tmp_path, DONE_PLAN_WITH_CHECKS)
    ctx = _make_ctx(tmp_path, disable_midway_checks=True, disable_final_check=True)
    with patch("artemis.graph.graph.run_final_check", new=AsyncMock()) as final_mock:
        update = await exit_settlement_node(_make_state(), ctx)
    final_mock.assert_not_awaited()
    outcome = update["run_outcome"]
    assert outcome["tests"]["unchecked"] == 3
    assert outcome["tests"]["passed"] == 0


@pytest.mark.asyncio
async def test_matrix_off_on_final_judges_from_state_and_history(tmp_path):
    _write_plan(tmp_path, DONE_PLAN_WITH_CHECKS)
    ctx = _make_ctx(tmp_path, disable_midway_checks=True)
    report = CheckReport(
        verdicts=[
            CheckVerdict(item_text="no crash dialog", kind="assert", status="passed", evidence="e"),
            CheckVerdict(item_text="alarm exists", kind="verify", status="passed", evidence="e"),
            CheckVerdict(
                item_text="toast appeared",
                kind="assert",
                status="inconclusive",
                evidence="transient evidence was never captured",
            ),
        ]
    )
    with patch("artemis.graph.graph.run_final_check", new=AsyncMock(return_value=report)):
        update = await exit_settlement_node(_make_state(), ctx)
    outcome = update["run_outcome"]
    assert outcome["tests"]["passed"] == 2
    assert outcome["tests"]["inconclusive"] == 1
    assert outcome["tests"]["failed"] == 0


@pytest.mark.asyncio
async def test_matrix_on_off_settles_without_final(tmp_path):
    # covered structurally by test_settlement_runs_phase_one_only_when_final_disabled;
    # here assert the summary is still produced with unchecked leftovers.
    _write_plan(tmp_path, DONE_PLAN_WITH_CHECKS)
    ctx = _make_ctx(tmp_path, disable_final_check=True)
    with patch("artemis.graph.graph.run_final_check", new=AsyncMock()) as final_mock:
        update = await exit_settlement_node(_make_state(), ctx)
    final_mock.assert_not_awaited()
    assert update["run_outcome"]["tests"]["unchecked"] == 3


@pytest.mark.asyncio
async def test_matrix_on_on_full_pipeline(tmp_path):
    _write_plan(tmp_path, DONE_PLAN_WITH_CHECKS)
    ctx = _make_ctx(tmp_path)
    # A checkpoint verdict already in the ledger...
    append_ledger_record(
        tmp_path,
        {
            "attempt_id": "c#1",
            "checkpoint_id": "c",
            "item_text": "alarm exists",
            "kind": "verify",
            "when": "on_complete",
            "status": "passed",
            "evidence": "probe output",
        },
    )
    # ...and the final review supplements the rest.
    report = CheckReport(
        verdicts=[
            CheckVerdict(item_text="no crash dialog", kind="assert", status="passed", evidence="e"),
            CheckVerdict(item_text="toast appeared", kind="assert", status="failed", evidence="e"),
        ]
    )
    with patch("artemis.graph.graph.run_final_check", new=AsyncMock(return_value=report)):
        update = await exit_settlement_node(_make_state(), ctx)
    outcome = update["run_outcome"]
    assert outcome["tests"]["passed"] == 2
    assert outcome["tests"]["failed"] == 1
    assert outcome["tests"]["unchecked"] == 0
