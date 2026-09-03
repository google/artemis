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

"""Regression tests for mobile_manage_task's state reconciliation.

Guards the invariants restored after the reconcile rewrite:
- a DB terminal verdict never overrides a status.json that already reached its
  own terminal state (a completed task must keep its result),
- except to correct a previously liveness-inferred failure,
- and a task without a recorded pid (daemon dispatch) is never inferred dead.
"""

import sqlite3
import time

import pytest

import mcp_server.tools.task_manager as task_manager
from mcp_server.tools.task_manager import _LIVENESS_FAILURE_ERROR, _reconcile_task_state


@pytest.fixture
def no_persistence(monkeypatch):
    """Keep reconciliation in-memory: no DB, no status writes, no lock queries."""
    monkeypatch.setattr(task_manager, "_find_data_engine_db", lambda: None)
    monkeypatch.setattr(task_manager, "_session_tracked_by_lock", lambda _tid: False)
    monkeypatch.setattr(task_manager.trace_store, "write_status", lambda *_a, **_k: None)
    monkeypatch.setattr(task_manager.trace_store, "update_trace_status", lambda *_a, **_k: None)
    monkeypatch.setattr(task_manager, "notify", lambda *_a, **_k: None)


def _db_with_session(tmp_path, trace_id: str, status: str, pid: int | None = 4242):
    db_path = tmp_path / "artemis.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE sessions (session_id TEXT, status TEXT, pid INTEGER, start_time REAL)"
    )
    conn.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", (trace_id, status, pid, time.time()))
    conn.commit()
    conn.close()
    return str(db_path)


@pytest.fixture
def use_db(tmp_path, monkeypatch, no_persistence):
    def _install(trace_id: str, status: str, pid: int | None = 4242):
        db_path = _db_with_session(tmp_path, trace_id, status, pid)
        monkeypatch.setattr(task_manager, "_find_data_engine_db", lambda: db_path)

    return _install


def test_db_failed_never_overrides_completed_status(use_db):
    use_db("t1", "failed")
    status_data = {"status": "completed", "result": "the answer", "pid": 111}

    current_status, _pid, is_alive = _reconcile_task_state("t1", status_data)

    assert current_status == "completed"
    assert status_data["status"] == "completed"
    assert status_data["result"] == "the answer"
    assert is_alive is False


def test_db_verdict_corrects_liveness_inferred_failure(use_db):
    use_db("t2", "completed")
    status_data = {"status": "failed", "error": _LIVENESS_FAILURE_ERROR, "pid": 111}

    current_status, _pid, _is_alive = _reconcile_task_state("t2", status_data)

    assert current_status == "completed"
    assert status_data["error"] is None


def test_db_never_overrides_user_cancellation(use_db):
    use_db("t3", "completed")
    status_data = {"status": "cancelled", "pid": 111}

    current_status, _pid, _is_alive = _reconcile_task_state("t3", status_data)

    assert current_status == "cancelled"


def test_legacy_success_status_is_normalized_to_completed(no_persistence):
    status_data = {"status": "success", "result": "the answer", "pid": 111}

    current_status, _pid, is_alive = _reconcile_task_state("t7", status_data)

    assert current_status == "completed"
    assert status_data["status"] == "completed"
    assert status_data["result"] == "the answer"
    assert is_alive is False


def test_db_success_verdict_surfaces_as_completed(use_db):
    use_db("t8", "success")
    status_data = {"status": "running", "pid": 111}

    current_status, _pid, _is_alive = _reconcile_task_state("t8", status_data)

    assert current_status == "completed"
    assert status_data["status"] == "completed"


def test_pidless_running_task_is_assumed_alive(no_persistence):
    # Daemon dispatch writes no pid into status.json; liveness must not be
    # inferred even long past the startup grace window.
    status_data = {"status": "running", "start_time": time.time() - 3600}

    current_status, _pid, is_alive = _reconcile_task_state("t4", status_data)

    assert current_status == "running"
    assert is_alive is True
    assert status_data.get("error") is None


def test_dead_pid_past_grace_is_marked_failed(no_persistence, monkeypatch):
    monkeypatch.setattr(task_manager, "_pid_alive", lambda _pid: False)
    status_data = {
        "status": "running",
        "pid": 99999,
        "start_time": time.time() - 3600,
    }

    current_status, _pid, is_alive = _reconcile_task_state("t5", status_data)

    assert current_status == "failed"
    assert status_data["error"] == _LIVENESS_FAILURE_ERROR
    assert is_alive is False


def test_dead_pid_within_grace_is_assumed_alive(no_persistence, monkeypatch):
    monkeypatch.setattr(task_manager, "_pid_alive", lambda _pid: False)
    status_data = {"status": "running", "pid": 99999, "start_time": time.time()}

    current_status, _pid, is_alive = _reconcile_task_state("t6", status_data)

    assert current_status == "running"
    assert is_alive is True
