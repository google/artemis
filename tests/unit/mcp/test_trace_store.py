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

"""Unit tests for MCP trace store."""

import glob
import logging
import os
import shutil
import tempfile
import threading
import uuid
import pytest

from mcp_server.utils import trace_store


@pytest.fixture
def temp_trace_env(monkeypatch):
    temp_dir = tempfile.mkdtemp()
    monkeypatch.setattr(trace_store, "TRACES_DIR", temp_dir)
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


def test_init_and_read_status(temp_trace_env):
    trace_id = str(uuid.uuid4())
    task_desc = "Test open settings"
    model = "Flash"
    conv_id = "test-conv-123"

    init_res = trace_store.init_trace(trace_id, task_desc, model, conv_id)
    assert init_res["trace_id"] == trace_id
    assert init_res["status"] == "running"
    assert init_res["model"] == "Flash"

    status_data = trace_store.read_status(trace_id)
    assert status_data is not None
    assert status_data["trace_id"] == trace_id
    assert status_data["task_desc"] == task_desc
    assert status_data["conversation_id"] == conv_id


def test_update_trace_status(temp_trace_env):
    trace_id = str(uuid.uuid4())
    trace_store.init_trace(trace_id, "Test task", "Pro", "conv-456")

    updated = trace_store.update_trace_status(
        trace_id=trace_id,
        status="completed",
        result={"success": True},
    )
    assert updated is not None
    assert updated["status"] == "completed"
    assert updated["result"] == {"success": True}
    assert updated["end_time"] is not None

    failed = trace_store.update_trace_status(
        trace_id=trace_id,
        status="failed",
        error="App crashed",
    )
    assert failed is not None
    assert failed["status"] == "failed"
    assert failed["error"] == "App crashed"


def test_update_trace_status_normalizes_success_alias(temp_trace_env):
    trace_id = str(uuid.uuid4())
    trace_store.init_trace(trace_id, "Test task", "Flash", "conv-789")

    updated = trace_store.update_trace_status(trace_id=trace_id, status="success")
    assert updated is not None
    assert updated["status"] == "completed"
    assert updated["end_time"] is not None

    persisted = trace_store.read_status(trace_id)
    assert persisted["status"] == "completed"


def test_read_nonexistent_status(temp_trace_env):
    non_existent = str(uuid.uuid4())
    assert trace_store.read_status(non_existent) is None


def test_leftover_atomic_temp_file_does_not_affect_reads(temp_trace_env):
    """A crash between temp-write and replace leaves a .tmp file that readers ignore."""
    trace_id = str(uuid.uuid4())
    trace_store.init_trace(trace_id, "Crash sim", "Flash")

    status_path = trace_store.get_status_path(trace_id)
    # Simulate a writer that crashed after writing its temp file.
    with open(f"{status_path}.{uuid.uuid4().hex}.tmp", "w", encoding="utf-8") as f:
        f.write('{"status": "half-writ')

    data = trace_store.read_status(trace_id)
    assert data is not None
    assert data["status"] == "running"
    assert data["task_desc"] == "Crash sim"


def test_failed_atomic_replace_preserves_original(temp_trace_env):
    """If the atomic replace fails, the previous status.json stays intact."""
    from unittest.mock import patch

    trace_id = str(uuid.uuid4())
    trace_store.init_trace(trace_id, "Original", "Flash")

    def broken_replace(src, dst):
        raise OSError("simulated replace failure")

    with patch.object(trace_store, "_replace_with_retry", broken_replace):
        with pytest.raises(OSError):
            trace_store.write_status(trace_id, {"status": "clobbered"})

    data = trace_store.read_status(trace_id)
    assert data is not None
    assert data["task_desc"] == "Original"
    # The failed writer's temp file must not linger.
    assert glob.glob(os.path.join(trace_store.get_trace_dir(trace_id), "*.tmp")) == []


def test_corrupt_status_is_quarantined_and_read_returns_none(temp_trace_env, caplog):
    trace_id = str(uuid.uuid4())
    trace_store.init_trace(trace_id, "Corrupt me", "Flash")

    status_path = trace_store.get_status_path(trace_id)
    with open(status_path, "w", encoding="utf-8") as f:
        f.write('{"status": "running", "trace_id"')  # torn write

    with caplog.at_level(logging.WARNING, logger="mcp_server.utils.trace_store"):
        assert trace_store.read_status(trace_id) is None

    assert any("Corrupt status.json" in rec.message for rec in caplog.records)
    assert os.path.exists(f"{status_path}.corrupt")
    assert not os.path.exists(status_path)


def test_missing_status_stays_silent(temp_trace_env, caplog):
    with caplog.at_level(logging.WARNING, logger="mcp_server.utils.trace_store"):
        assert trace_store.read_status(str(uuid.uuid4())) is None
    assert caplog.records == []


def test_update_on_corrupt_status_logs_and_drops(temp_trace_env, caplog):
    trace_id = str(uuid.uuid4())
    trace_store.init_trace(trace_id, "Corrupt update", "Flash")

    status_path = trace_store.get_status_path(trace_id)
    with open(status_path, "w", encoding="utf-8") as f:
        f.write("not json at all")

    with caplog.at_level(logging.WARNING, logger="mcp_server.utils.trace_store"):
        assert trace_store.update_trace_status(trace_id, "completed") is None

    assert any("Dropping status update" in rec.message for rec in caplog.records)
    # Updating a trace that never existed stays a silent no-op.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="mcp_server.utils.trace_store"):
        assert trace_store.update_trace_status(str(uuid.uuid4()), "completed") is None
    assert not any("Dropping status update" in rec.message for rec in caplog.records)


def test_concurrent_read_modify_write_does_not_lose_updates(temp_trace_env):
    """Interleaved RMW updates from two writers must not lose each other's fields."""
    trace_id = str(uuid.uuid4())
    trace_store.init_trace(trace_id, "Concurrency", "Flash")
    iterations = 25
    failures: list[BaseException] = []

    def set_errors():
        try:
            for i in range(iterations):
                assert trace_store.update_trace_status(
                    trace_id, "running", error=f"err-{i}"
                ) is not None
        except BaseException as exc:  # noqa: BLE001 - surfaced in the main thread
            failures.append(exc)

    def set_serials():
        try:
            for i in range(iterations):
                assert trace_store.update_trace_device_serial(
                    trace_id, f"serial-{i}"
                ) is not None
        except BaseException as exc:  # noqa: BLE001 - surfaced in the main thread
            failures.append(exc)

    threads = [threading.Thread(target=set_errors), threading.Thread(target=set_serials)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30.0)

    assert failures == []
    final = trace_store.read_status(trace_id)
    assert final is not None
    # Every serialized RMW preserves the other writer's latest field, so both
    # final values must survive regardless of which thread finished last.
    assert final["error"] == f"err-{iterations - 1}"
    assert final["device_serial"] == f"serial-{iterations - 1}"
    assert final["status"] == "running"


def test_trace_paths(temp_trace_env):
    trace_id = "test-paths-id"
    trace_dir = trace_store.get_trace_dir(trace_id)
    assert trace_store.get_trace_notes_dir(trace_id) == os.path.join(trace_dir, "notes")
    assert trace_store.get_trace_stdout_log_path(trace_id) == os.path.join(trace_dir, "stdout.log")
    assert trace_store.get_trace_stderr_log_path(trace_id) == os.path.join(trace_dir, "stderr.log")
