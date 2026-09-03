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
import json
import os
import shutil
import tempfile
from types import SimpleNamespace
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_server.tools import (
    mobile_get_device_state,
    mobile_inspect_trace,
    mobile_manage_task,
    mobile_run_task,
)
from artemis.runtime import trace_store


@pytest.fixture
def temp_trace_env(monkeypatch):
    temp_dir = tempfile.mkdtemp()
    monkeypatch.setattr(trace_store, "TRACES_DIR", temp_dir)
    monkeypatch.setenv("ARTEMIS_STANDALONE", "1")
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
    assert "device_serial" in sig_run.parameters
    assert "verification_level" in sig_run.parameters
    assert "explorer_mode" in sig_run.parameters
    assert sig_run.parameters["verification_level"].default is None
    assert sig_run.parameters["explorer_mode"].default is None
    # The tool description is the only schema an MCP caller sees.
    assert "verification_level" in (mobile_run_task.__doc__ or "")
    assert "explorer_mode" in (mobile_run_task.__doc__ or "")

    # mobile_manage_task signature check
    sig_manage = inspect.signature(mobile_manage_task)
    assert "action" in sig_manage.parameters
    assert "trace_id" in sig_manage.parameters
    assert "instruction" in sig_manage.parameters

    # mobile_get_device_state signature check
    sig_device = inspect.signature(mobile_get_device_state)
    assert "view_type" in sig_device.parameters
    assert "device_serial" in sig_device.parameters

    # mobile_inspect_trace signature check
    sig_inspect = inspect.signature(mobile_inspect_trace)
    assert "action" in sig_inspect.parameters
    assert "trace_id" in sig_inspect.parameters
    assert "step_number" in sig_inspect.parameters
    assert "query" in sig_inspect.parameters
    assert "step_range" in sig_inspect.parameters
    assert sig_inspect.parameters["max_results"].default == 5
    assert "'search'" in (mobile_inspect_trace.__doc__ or "")


def test_mobile_run_task_invalid_model():
    with pytest.raises(ValueError, match="Invalid model"):
        mobile_run_task(task_desc="test", conversation_id="conv-1", model="invalid_model")


def test_mobile_run_task_reserves_and_passes_global_queue_ticket(temp_trace_env):
    process = MagicMock(pid=43210)
    with (
        patch(
            "mcp_server.tools.task_runner.DeviceExecutionLock.reserve",
            return_value="queue-ticket-1",
        ) as reserve,
        patch("mcp_server.tools.task_runner.DeviceExecutionLock.transfer_reservation") as transfer,
        patch("mcp_server.tools.task_runner.subprocess.Popen", return_value=process) as popen,
    ):
        result = mobile_run_task(
            task_desc="Open Settings",
            conversation_id="conv-1",
            model="Flash",
        )

    reserve.assert_called_once()
    transfer.assert_called_once_with(
        "queue-ticket-1",
        43210,
        description="MCP task: Open Settings",
        session_id=result["trace_id"],
        ingress="mcp",
    )
    assert result["trace_id"]
    assert result["device_serial"] == "auto-select"
    assert popen.call_args.kwargs["env"]["ARTEMIS_DEVICE_QUEUE_TICKET"] == "queue-ticket-1"
    assert popen.call_args.kwargs["env"]["ARTEMIS_TASK_INGRESS"] == "mcp"
    status = trace_store.read_status(result["trace_id"])
    assert status["queue_ticket"] == "queue-ticket-1"
    assert status["device_serial"] is None


def test_mobile_run_task_with_device_serial(temp_trace_env):
    process = MagicMock(pid=54321)
    with (
        patch(
            "mcp_server.tools.task_runner.DeviceExecutionLock.reserve",
            return_value="queue-ticket-dev",
        ) as reserve,
        patch("mcp_server.tools.task_runner.DeviceExecutionLock.transfer_reservation") as transfer,
        patch("mcp_server.tools.task_runner.subprocess.Popen", return_value=process) as popen,
        # Hermetic device validation: the requested serial reports as ready
        # regardless of what is attached to the host running the tests.
        patch(
            "artemis.runtime.device_pool.device_pool.try_list_devices",
            return_value=[SimpleNamespace(serial="pixel-11-pro-001", state="device")],
        ),
    ):
        result = mobile_run_task(
            task_desc="Open Settings on target phone",
            conversation_id="conv-2",
            model="Flash",
            device_serial="pixel-11-pro-001",
        )

    reserve.assert_called_once_with(
        description="MCP task: Open Settings on target phone",
        session_id=result["trace_id"],
        ingress="mcp",
        device_id="pixel-11-pro-001",
    )
    transfer.assert_called_once_with(
        "queue-ticket-dev",
        54321,
        description="MCP task: Open Settings on target phone",
        session_id=result["trace_id"],
        ingress="mcp",
        device_id="pixel-11-pro-001",
    )
    assert result["device_serial"] == "pixel-11-pro-001"
    cmd = popen.call_args.args[0]
    assert "--device-serial" in cmd
    assert cmd[cmd.index("--device-serial") + 1] == "pixel-11-pro-001"
    assert popen.call_args.kwargs["env"]["ADB_DEVICE_SERIAL"] == "pixel-11-pro-001"
    assert popen.call_args.kwargs["env"]["ARTEMIS_DEVICE_ID"] == "pixel-11-pro-001"

    status = trace_store.read_status(result["trace_id"])
    assert status["device_serial"] == "pixel-11-pro-001"


def test_mobile_run_task_dispatched_to_daemon(temp_trace_env, monkeypatch):
    monkeypatch.delenv("ARTEMIS_STANDALONE", raising=False)
    with (
        patch(
            "mcp_server.tools.task_runner.ensure_daemon_running",
            return_value=(True, "http://127.0.0.1:8000"),
        ),
        patch(
            "mcp_server.tools.task_runner.submit_task_to_daemon",
            return_value={"status": "started", "tasks": [{"session_id": "daemon-sid-1"}]},
        ),
        patch("mcp_server.tools.task_runner.subprocess.Popen") as popen,
    ):
        result = mobile_run_task(
            task_desc="Open Settings via Daemon",
            conversation_id="conv-daemon",
            model="Flash",
        )

    popen.assert_not_called()
    assert result["trace_id"] == "daemon-sid-1"
    assert result["status"] == "running"
    assert "enqueued via unified Artemis Daemon" in result["message"]
    assert "stdout_log" in result
    assert "stderr_log" in result


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"verification_level": "paranoid"}, "Invalid verification_level"),
        ({"explorer_mode": "turbo"}, "Invalid explorer_mode"),
    ],
)
def test_mobile_run_task_rejects_unknown_pro_tuning_before_creating_a_trace(
    temp_trace_env, kwargs, match
):
    with pytest.raises(ValueError, match=match):
        mobile_run_task(task_desc="Open Settings", model="Pro", **kwargs)
    # Rejected before init_trace: nothing was written to the trace store.
    assert os.listdir(temp_trace_env) == []


def test_mobile_run_task_forwards_pro_tuning_to_background_runner(temp_trace_env):
    process = MagicMock(pid=777)
    with (
        patch("mcp_server.tools.task_runner.DeviceExecutionLock.reserve", return_value="t"),
        patch("mcp_server.tools.task_runner.DeviceExecutionLock.transfer_reservation"),
        patch("mcp_server.tools.task_runner.subprocess.Popen", return_value=process) as popen,
    ):
        mobile_run_task(
            task_desc="Audit the checkout flow",
            model="Pro",
            verification_level=" Strict ",
            explorer_mode="ULTRA",
        )

    cmd = popen.call_args.args[0]
    # Same flag spelling as `artemis run` / the admin-console worker, normalised values.
    assert cmd[cmd.index("--verification-level") + 1] == "strict"
    assert cmd[cmd.index("--explorer-pro-mode") + 1] == "ultra"


def test_mobile_run_task_omits_pro_tuning_flags_when_unset(temp_trace_env):
    process = MagicMock(pid=778)
    with (
        patch("mcp_server.tools.task_runner.DeviceExecutionLock.reserve", return_value="t"),
        patch("mcp_server.tools.task_runner.DeviceExecutionLock.transfer_reservation"),
        patch("mcp_server.tools.task_runner.subprocess.Popen", return_value=process) as popen,
    ):
        mobile_run_task(task_desc="Open Settings", model="Pro", verification_level="  ")

    cmd = popen.call_args.args[0]
    assert "--verification-level" not in cmd
    assert "--explorer-pro-mode" not in cmd


def test_mobile_run_task_forwards_pro_tuning_to_daemon(temp_trace_env, monkeypatch):
    monkeypatch.delenv("ARTEMIS_STANDALONE", raising=False)
    with (
        patch(
            "mcp_server.tools.task_runner.ensure_daemon_running",
            return_value=(True, "http://127.0.0.1:8000"),
        ),
        patch(
            "mcp_server.tools.task_runner.submit_task_to_daemon",
            return_value={"status": "started", "tasks": [{"session_id": "daemon-sid-2"}]},
        ) as submit,
        patch("mcp_server.tools.task_runner.subprocess.Popen") as popen,
    ):
        result = mobile_run_task(
            task_desc="Audit via Daemon",
            model="Pro",
            verification_level="Checkpoints",
            explorer_mode="pro",
        )

    popen.assert_not_called()
    assert result["trace_id"] == "daemon-sid-2"
    # JSON field spelling of /api/run: verification_level / explorer_mode.
    assert submit.call_args.kwargs["verification_level"] == "checkpoints"
    assert submit.call_args.kwargs["explorer_mode"] == "pro"


def test_mobile_manage_task_unknown_trace(temp_trace_env):
    res = mobile_manage_task(action="status", trace_id="non-existent-trace")
    assert res["status"] == "unknown"
    assert "not found" in res["message"]


def test_mobile_manage_task_status_and_stop(temp_trace_env):
    trace_id = str(uuid.uuid4())
    trace_store.init_trace(trace_id, "Test task", "Flash", "conv-1", device_serial="device-xyz")

    # Status check
    status_res = mobile_manage_task(action="status", trace_id=trace_id)
    assert status_res["trace_id"] == trace_id
    assert status_res["status"] == "running"
    assert status_res["model"] == "Flash"
    assert status_res["device_serial"] == "device-xyz"

    # Stop without PID
    stop_res = mobile_manage_task(action="stop", trace_id=trace_id)
    assert stop_res["trace_id"] == trace_id
    assert "PID" in stop_res["message"] or "Cannot stop" in stop_res["message"]


def test_mobile_manage_task_status_backfills_device_serial_from_db(temp_trace_env):
    import sqlite3
    import os

    trace_id = str(uuid.uuid4())
    trace_store.init_trace(trace_id, "Test task without serial", "Flash", "conv-1")

    # Create dummy data_engine.db with session record
    db_path = os.path.join(temp_trace_env, "data_engine.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, device_info TEXT, pid INTEGER, start_time REAL)"
    )
    conn.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?)",
        (trace_id, json.dumps({"device_id": "auto-chosen-serial-777"}), 12345, 1000.0),
    )
    conn.commit()
    conn.close()

    status_res = mobile_manage_task(action="status", trace_id=trace_id)
    assert status_res["device_serial"] == "auto-chosen-serial-777"


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
async def test_mobile_get_device_state_hierarchy_without_ocr():
    """XML hierarchy remains available when OCR is not configured."""
    controller = MagicMock()
    controller.ctx.device.device_width = 1080
    controller.ctx.device.device_height = 2400
    controller.get_screen_data = AsyncMock(
        return_value=SimpleNamespace(
            base64="ZHVtbXk=",
            elements=[
                {
                    "class": "android.widget.Button",
                    "text": "Start",
                    "bounds": "[100,500][400,600]",
                }
            ],
            width=1080,
            height=2400,
        )
    )

    with (
        patch("mcp_server.tools.device_state._get_controller", return_value=controller),
        patch("mcp_server.tools.device_state.is_ocr_configured", return_value=False),
        patch(
            "mcp_server.tools.device_state.perform_ocr", new_callable=AsyncMock
        ) as perform_ocr_mock,
    ):
        result = await mobile_get_device_state(view_type="hierarchy")

    assert "Start" in result
    perform_ocr_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_mobile_inspect_trace_invalid_action():
    res = await mobile_inspect_trace(action="invalid_action", trace_id="trace-123")
    assert "error" in res
    assert "not supported" in res["message"]


@pytest.mark.asyncio
async def test_mobile_get_device_state_passes_device_serial():
    """Verify that device_serial is forwarded to _get_controller."""
    controller = MagicMock()
    controller.ctx.device.device_width = 1080
    controller.ctx.device.device_height = 2400
    controller.get_screen_data = AsyncMock(
        return_value=SimpleNamespace(
            base64="ZHVtbXk=",
            elements=[],
            width=1080,
            height=2400,
        )
    )

    with (
        patch(
            "mcp_server.tools.device_state._get_controller", return_value=controller
        ) as get_ctrl_mock,
        patch("mcp_server.tools.device_state.is_ocr_configured", return_value=False),
    ):
        await mobile_get_device_state(view_type="hierarchy", device_serial="device-serial-abc")

    get_ctrl_mock.assert_called_once_with(device_serial="device-serial-abc")


@pytest.mark.asyncio
async def test_mobile_inspect_trace_includes_device_serial(temp_trace_env):
    import sqlite3

    trace_id = str(uuid.uuid4())
    trace_store.init_trace(
        trace_id, "Inspect test", "Flash", "conv-1", device_serial="pixel-target-999"
    )

    # Create dummy database and table
    db_path = os.path.join(temp_trace_env, "data_engine.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, start_time REAL, device_info TEXT)"
    )
    conn.execute(
        "CREATE TABLE steps (step_id TEXT PRIMARY KEY, session_id TEXT, step_number INTEGER, timestamp REAL, pre_image_name TEXT, post_image_name TEXT, summary TEXT, action_taken TEXT, operator_raw_thinking TEXT, last_execution_result TEXT, extra_metadata TEXT)"
    )
    conn.execute(
        "CREATE TABLE traces (trace_id TEXT PRIMARY KEY, step_id TEXT, session_id TEXT, type TEXT, name TEXT, status TEXT, timestamp REAL, duration REAL, payload TEXT)"
    )
    conn.execute("INSERT INTO sessions VALUES (?, ?, ?)", (trace_id, 1000.0, None))
    conn.commit()
    conn.close()

    res_summary = await mobile_inspect_trace(action="view_summary", trace_id=trace_id)
    assert "**Device Serial:** `pixel-target-999`" in res_summary


def test_trace_store_device_serial(temp_trace_env):
    trace_id = "test-store-serial"
    data = trace_store.init_trace(trace_id, "desc", "Flash", "conv", device_serial="init-serial")
    assert data["device_serial"] == "init-serial"

    data_updated = trace_store.update_trace_device_serial(trace_id, "updated-serial")
    assert data_updated["device_serial"] == "updated-serial"

    read_back = trace_store.read_status(trace_id)
    assert read_back["device_serial"] == "updated-serial"


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


def test_mobile_manage_task_syncs_terminal_status_from_db(temp_trace_env):
    import sqlite3

    trace_id = str(uuid.uuid4())
    trace_store.init_trace(trace_id, "Running task to sync", "Flash", "conv-sync")

    db_path = os.path.join(temp_trace_env, "data_engine.db")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            status TEXT,
            pid INTEGER,
            start_time REAL,
            end_time REAL,
            device_info TEXT
        )"""
    )
    cur.execute(
        "INSERT INTO sessions (session_id, status, pid, start_time) VALUES (?, ?, ?, ?)",
        (trace_id, "completed", 54321, 1000.0),
    )
    conn.commit()
    conn.close()

    res = mobile_manage_task(action="status", trace_id=trace_id)
    assert res["status"] == "completed"
    # Ensure status.json was persisted
    saved_st = trace_store.read_status(trace_id)
    assert saved_st["status"] == "completed"
    assert saved_st["pid"] == 54321


def test_mobile_manage_task_stop_via_daemon(temp_trace_env, monkeypatch):
    monkeypatch.delenv("ARTEMIS_STANDALONE", raising=False)
    trace_id = str(uuid.uuid4())
    trace_store.init_trace(trace_id, "Daemon task to stop", "Flash", "conv-stop")

    with (
        patch("mcp_server.tools.task_manager.is_daemon_running", return_value=True),
        patch("mcp_server.tools.task_manager.stop_task_on_daemon", return_value=True) as mock_stop,
    ):
        res = mobile_manage_task(action="stop", trace_id=trace_id)
        mock_stop.assert_called_once_with(trace_id)
        assert res["status"] == "cancelled"
        assert trace_store.read_status(trace_id)["status"] == "cancelled"


def _seed_trace(temp_dir: str, trace_id: str, *, with_image: bool = True) -> str:
    """Writes one Flash-style step (with a tool trace and a pre screenshot)
    through the real storage layer, exactly as a live run would."""
    import io

    from PIL import Image

    from artemis.data_engine.models import (
        ImageRecord,
        SessionMetadata,
        StepRecord,
        TraceRecord,
    )
    from artemis.data_engine.storage import StorageManager

    storage = StorageManager(os.path.join(temp_dir, "data_engine.db"), temp_dir)
    storage.create_session(
        SessionMetadata(session_id=trace_id, initial_goal="Replay test", start_time=1000.0)
    )
    image_name = None
    if with_image:
        image_name = "a" * 64
        images_dir = os.path.join(temp_dir, "images")
        os.makedirs(images_dir, exist_ok=True)
        buf = io.BytesIO()
        Image.new("RGB", (1080, 2400), "white").save(buf, format="JPEG")
        with open(os.path.join(images_dir, f"{image_name}.jpg"), "wb") as f:
            f.write(buf.getvalue())
        storage.create_image(
            ImageRecord(
                image_name=image_name,
                ocr_result=[{"text": "Save ocrneedle", "bounds": [0, 0, 10, 10]}],
            )
        )
    step_id = str(uuid.uuid4())
    storage.create_step(
        StepRecord(
            step_id=step_id,
            session_id=trace_id,
            step_number=1,
            timestamp=1012.0,
            pre_image_name=image_name,
            summary="Asked the explorer, then tapped Save.",
            action_taken={"action": "click", "target": [540, 1200], "target_text": "Save"},
            operator_raw_thinking="Confirm the toggle first.",
            last_execution_result={"status": "success"},
            extra_metadata={"width": 1080, "height": 2400},
        )
    )
    storage.create_trace(
        TraceRecord(
            trace_id=str(uuid.uuid4()),
            session_id=trace_id,
            step_id=step_id,
            type="tool",
            name="ask_explorer",
            status="success",
            timestamp=1011.0,
            duration=0.8,
            payload={"args": {"question": "toggle?"}, "result": "The toggle is ON. " * 100},
        )
    )
    return step_id


@pytest.mark.asyncio
async def test_mobile_inspect_trace_step_details_replays_tool_calls_for_flash(temp_trace_env):
    """view_step_details works for Flash runs too and replays the recorded
    tool calls through the same friendly-step path the live DataEngine uses."""
    trace_id = str(uuid.uuid4())
    trace_store.init_trace(trace_id, "Replay test", "Flash", "conv-1", device_serial="pixel-7")
    _seed_trace(temp_trace_env, trace_id)

    res = await mobile_inspect_trace(action="view_step_details", trace_id=trace_id, step_number=1)
    assert "error" not in res
    assert res["device_serial"] == "pixel-7"
    details = res["details"]
    assert "- **Step 1 (Start: 12.0s)**" in details
    assert "[Screen]: Asked the explorer, then tapped Save." in details
    assert "Confirm the toggle first." in details
    assert "`ask_explorer(" in details
    assert "The toggle is ON. " * 100 in details
    # Coordinates are replayed normalized, not as stored pixels.
    assert "[540, 1200]" not in details
    assert "Tapped 'Save'" in details

    missing = await mobile_inspect_trace(
        action="view_step_details", trace_id=trace_id, step_number=9
    )
    assert missing["error"] == "Step not found"


@pytest.mark.asyncio
async def test_mobile_inspect_trace_step_screenshots_draw_the_overlay(temp_trace_env):
    trace_id = str(uuid.uuid4())
    trace_store.init_trace(trace_id, "Shot test", "Flash", "conv-1", device_serial="pixel-7")
    _seed_trace(temp_trace_env, trace_id)

    res = await mobile_inspect_trace(
        action="view_step_screenshots", trace_id=trace_id, step_number=1
    )
    assert "error" not in res
    assert res["before_screenshot"].startswith("file://")
    assert res["before_screenshot"].endswith(f"{'a' * 64}.jpg")
    assert res["after_screenshot"] is None
    overlay = res["action_overlay_screenshot"]
    assert overlay is not None
    overlay_path = overlay[len("file://") :]
    assert os.path.exists(overlay_path)
    assert overlay_path.endswith("step_1_overlay.jpg")
    # The overlay differs from the plain pre screenshot: the action was drawn.
    with open(overlay_path, "rb") as f, open(res["before_screenshot"][7:], "rb") as g:
        assert f.read() != g.read()

    # Second call reuses the file it already wrote.
    again = await mobile_inspect_trace(
        action="view_step_screenshots", trace_id=trace_id, step_number=1
    )
    assert again["action_overlay_screenshot"] == overlay

    no_step = await mobile_inspect_trace(
        action="view_step_screenshots", trace_id=trace_id, step_number=None
    )
    assert no_step["error"] == "Missing parameter"


@pytest.mark.asyncio
async def test_mobile_inspect_trace_search_uses_the_shared_history_search(temp_trace_env):
    trace_id = str(uuid.uuid4())
    trace_store.init_trace(trace_id, "Search test", "Flash", "conv-1", device_serial="pixel-7")
    _seed_trace(temp_trace_env, trace_id)

    res = await mobile_inspect_trace(action="search", trace_id=trace_id, query="toggle")
    assert "error" not in res
    assert res["device_serial"] == "pixel-7"
    assert "[Step 1 " in res["results"]
    assert "Screen: Asked the explorer, then tapped Save." in res["results"]

    # On-screen OCR text of the step's screenshot is part of the search surface.
    res = await mobile_inspect_trace(action="search", trace_id=trace_id, query="ocrneedle")
    assert "[Step 1 " in res["results"]

    ledger = await mobile_inspect_trace(action="search", trace_id=trace_id, step_range=[1, 1])
    assert "Action ledger for Steps 1–1" in ledger["results"]

    empty = await mobile_inspect_trace(action="search", trace_id=trace_id)
    assert empty["error"] == "Missing parameter"
