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

import os
import shutil
import tempfile
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


def test_read_nonexistent_status(temp_trace_env):
    non_existent = str(uuid.uuid4())
    assert trace_store.read_status(non_existent) is None


def test_trace_paths(temp_trace_env):
    trace_id = "test-paths-id"
    trace_dir = trace_store.get_trace_dir(trace_id)
    assert trace_store.get_trace_notes_dir(trace_id) == os.path.join(trace_dir, "notes")
    assert trace_store.get_trace_stdout_log_path(trace_id) == os.path.join(trace_dir, "stdout.log")
    assert trace_store.get_trace_stderr_log_path(trace_id) == os.path.join(trace_dir, "stderr.log")
