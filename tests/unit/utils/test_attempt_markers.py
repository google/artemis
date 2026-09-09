"""The ``attempts`` marker contract between the Validator and the renderers.

The Validator writes ``"Dispatched"`` as the terminal attempt of an action the
device accepted (kept only when a retry preceded it); every failed attempt keeps
its error text. The renderers must read a retried-then-dispatched action as a
clean dispatch, never as a failure.
"""

from artemis.utils.task_tree import (
    _burst_member_status,
    failed_execution_error,
    format_result_clean,
)


def _report(attempts: list[str], status: str = "dispatched") -> dict:
    return {
        "status": status,
        "execution": [
            {"action": "tap", "normalized_coordinates": [500, 500], "attempts": attempts}
        ],
    }


def test_retried_then_dispatched_action_is_not_a_failure():
    report = _report(["Error: tap rejected", "Dispatched"])
    assert failed_execution_error(report) is None
    assert format_result_clean(report) is None


def test_terminal_failure_keeps_every_attempt():
    report = _report(["Error: tap rejected", "Error: tap rejected again"], status="failed")
    assert failed_execution_error(report) == "Error: tap rejected | Error: tap rejected again"
    assert format_result_clean(report) == "Error: tap rejected | Error: tap rejected again"


def test_burst_member_labels_follow_the_dispatch_vocabulary():
    report = {
        "status": "failed",
        "execution": [
            {"action": "tap"},
            {"action": "tap", "attempts": ["Error: boom"]},
            {"action": "tap", "attempts": ["Skipped (burst aborted)"]},
        ],
    }
    assert _burst_member_status(report, 0) == " (dispatched)"
    assert _burst_member_status(report, 1) == " (FAILED: Error: boom)"
    assert _burst_member_status(report, 2) == " (skipped)"
    assert _burst_member_status(report, 3) == " (not dispatched)"
    assert _burst_member_status({"execution": [{"attempts": ["Dispatched"]}]}, 0) == " (dispatched)"
