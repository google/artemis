from __future__ import annotations

from artemis.agents.flash.runner import FlashRunner


def _runner() -> FlashRunner:
    return object.__new__(FlashRunner)


def test_one_failed_capture_does_not_stop_the_run() -> None:
    runner = _runner()

    runner._note_capture_failure("device offline")

    assert getattr(runner, "_device_stop_reason", None) is None


def test_two_failed_captures_stop_the_run_and_name_the_device() -> None:
    runner = _runner()

    runner._note_capture_failure("no screenshot from 103.123.5.27:5555")
    runner._note_capture_failure("no screenshot from 103.123.5.27:5555")

    reason = runner._device_stop_reason
    assert "failed screen captures" in reason
    assert "103.123.5.27:5555" in reason


def test_the_stop_reason_counts_the_captures_it_saw() -> None:
    runner = _runner()

    for _ in range(3):
        runner._note_capture_failure("device offline")

    assert "3 failed screen captures" in runner._device_stop_reason
