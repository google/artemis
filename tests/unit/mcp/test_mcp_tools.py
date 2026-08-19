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

"""Unit tests for Artemis MCP Tools."""

import inspect
import shutil
import tempfile
import uuid
import pytest

from mcp_server.tools import (
    mobile_get_device_state,
    mobile_inspect_trace,
    mobile_manage_task,
    mobile_run_task,
)
from mcp_server.utils import trace_store


@pytest.fixture
def temp_trace_env(monkeypatch):
    temp_dir = tempfile.mkdtemp()
    monkeypatch.setattr(trace_store, "TRACES_DIR", temp_dir)
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


def test_tool_signatures():
    # mobile_run_task signature check
    sig_run = inspect.signature(mobile_run_task)
    assert "task_desc" in sig_run.parameters
    assert "conversation_id" in sig_run.parameters
    assert "model" in sig_run.parameters
    assert "locked_app_package" in sig_run.parameters
    assert "app_path" in sig_run.parameters
    assert "expected_output_desc" in sig_run.parameters

    # mobile_manage_task signature check
    sig_manage = inspect.signature(mobile_manage_task)
    assert "action" in sig_manage.parameters
    assert "trace_id" in sig_manage.parameters
    assert "instruction" in sig_manage.parameters

    # mobile_get_device_state signature check
    sig_device = inspect.signature(mobile_get_device_state)
    assert "view_type" in sig_device.parameters

    # mobile_inspect_trace signature check
    sig_inspect = inspect.signature(mobile_inspect_trace)
    assert "action" in sig_inspect.parameters
    assert "trace_id" in sig_inspect.parameters
    assert "step_number" in sig_inspect.parameters


def test_mobile_run_task_invalid_model():
    with pytest.raises(ValueError, match="Invalid model"):
        mobile_run_task(task_desc="test", conversation_id="conv-1", model="invalid_model")


def test_mobile_manage_task_unknown_trace(temp_trace_env):
    res = mobile_manage_task(action="status", trace_id="non-existent-trace")
    assert res["status"] == "unknown"
    assert "not found" in res["message"]


def test_mobile_manage_task_status_and_stop(temp_trace_env):
    trace_id = str(uuid.uuid4())
    trace_store.init_trace(trace_id, "Test task", "Flash", "conv-1")

    # Status check
    status_res = mobile_manage_task(action="status", trace_id=trace_id)
    assert status_res["trace_id"] == trace_id
    assert status_res["status"] == "running"
    assert status_res["model"] == "Flash"

    # Stop without PID
    stop_res = mobile_manage_task(action="stop", trace_id=trace_id)
    assert stop_res["trace_id"] == trace_id
    assert "PID" in stop_res["message"] or "Cannot stop" in stop_res["message"]


def test_mobile_manage_task_inject_instruction(temp_trace_env):
    trace_id = str(uuid.uuid4())
    trace_store.init_trace(trace_id, "Test task", "Pro", "conv-1")

    # Missing instruction parameter
    err_res = mobile_manage_task(action="inject_instruction", trace_id=trace_id)
    assert "Missing required argument" in err_res["message"]

    # Valid injection
    ok_res = mobile_manage_task(
        action="inject_instruction", trace_id=trace_id, instruction="Scroll down slowly"
    )
    assert "Successfully injected" in ok_res["message"]


@pytest.mark.asyncio
async def test_mobile_inspect_trace_invalid_action():
    res = await mobile_inspect_trace(action="invalid_action", trace_id="trace-123")
    assert "error" in res
    assert "not supported" in res["message"]


@pytest.mark.asyncio
async def test_mcp_server_auto_registers_tools():
    """Verify that importing mcp_server.base auto-registers all mobile tools without manual tools import."""
    from mcp_server.base import mcp
    tools = await mcp.list_tools()
    tool_names = {t.name for t in tools}
    assert {
        "mobile_run_task",
        "mobile_manage_task",
        "mobile_get_device_state",
        "mobile_inspect_trace",
    }.issubset(tool_names)
