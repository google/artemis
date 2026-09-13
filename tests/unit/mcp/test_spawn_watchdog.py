"""Tests for the spawn watchdog guarding mobile_run_task's standalone fallback."""

import json
import os
from unittest.mock import patch

from mcp_server.tools import task_runner as spawn_tool


def _write_status(trace_dir: str, trace_id: str, status: str) -> None:
    os.makedirs(trace_dir, exist_ok=True)
    with open(os.path.join(trace_dir, "status.json"), "w", encoding="utf-8") as f:
        json.dump({"trace_id": trace_id, "status": status}, f)


def _patch_trace_dir(tmp_path):
    return patch.object(spawn_tool.trace_store, "TRACES_DIR", str(tmp_path))


def test_watchdog_passes_when_runner_creates_logs(tmp_path):
    trace_id = "wd-boot-ok"
    trace_dir = tmp_path / trace_id
    _write_status(str(trace_dir), trace_id, "running")
    (trace_dir / "stdout.log").write_text("booted", encoding="utf-8")

    with _patch_trace_dir(tmp_path), patch.object(spawn_tool, "_kill_process_tree") as kill:
        assert spawn_tool._watch_spawn(
            trace_id,
            pid=999999,
            queue_ticket="t",
            conversation_id=None,
            deadline_seconds=0.2,
            poll_interval=0.05,
        )
    kill.assert_not_called()


def test_watchdog_skips_already_terminal_task(tmp_path):
    trace_id = "wd-cancelled"
    trace_dir = tmp_path / trace_id
    _write_status(str(trace_dir), trace_id, "cancelled")

    with _patch_trace_dir(tmp_path), patch.object(spawn_tool, "_kill_process_tree") as kill:
        assert spawn_tool._watch_spawn(
            trace_id,
            pid=999999,
            queue_ticket="t",
            conversation_id=None,
            deadline_seconds=0.2,
            poll_interval=0.05,
        )
    kill.assert_not_called()


def test_watchdog_kills_and_fails_hung_runner(tmp_path):
    trace_id = "wd-hung"
    trace_dir = tmp_path / trace_id
    _write_status(str(trace_dir), trace_id, "running")

    with (
        _patch_trace_dir(tmp_path),
        patch.object(spawn_tool, "_kill_process_tree") as kill,
        patch.object(spawn_tool.DeviceExecutionLock, "cancel_reservation") as cancel,
        patch.object(spawn_tool, "notify") as notifier,
    ):
        assert not spawn_tool._watch_spawn(
            trace_id,
            pid=424242,
            queue_ticket="ticket-1",
            conversation_id="conv-1",
            deadline_seconds=0.2,
            poll_interval=0.05,
        )

    kill.assert_called_once_with(424242)
    cancel.assert_called_once_with("ticket-1")
    notifier.assert_called_once()

    with open(trace_dir / "status.json", encoding="utf-8") as f:
        status = json.load(f)
    assert status["status"] == "failed"
    assert "no logs" in status.get("error", "")


def test_watchdog_tolerates_logs_appearing_at_deadline(tmp_path):
    """Logs that appear between the last poll and the deadline must not cause a kill."""
    trace_id = "wd-late-boot"
    trace_dir = tmp_path / trace_id
    _write_status(str(trace_dir), trace_id, "running")

    real_read_status = spawn_tool.trace_store.read_status
    calls = {"n": 0}

    def read_status_and_create_log(tid):
        calls["n"] += 1
        if calls["n"] == 1:
            (trace_dir / "stderr.log").write_text("late boot", encoding="utf-8")
        return real_read_status(tid)

    with (
        _patch_trace_dir(tmp_path),
        patch.object(spawn_tool.trace_store, "read_status", side_effect=read_status_and_create_log),
        patch.object(spawn_tool, "_kill_process_tree") as kill,
    ):
        assert spawn_tool._watch_spawn(
            trace_id,
            pid=999999,
            queue_ticket="t",
            conversation_id=None,
            deadline_seconds=0.15,
            poll_interval=0.2,
        )
    kill.assert_not_called()
