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

"""MCP Tool: mobile_manage_task."""

import json
import os
import signal
import sqlite3
import sys
import time
from typing import Any

from mcp_server.base import mcp
from mcp_server.notifiers import notify
from mcp_server.utils import env_utils, trace_store
from artemis.runtime import DeviceExecutionLock, process_supervisor

_LIVENESS_FAILURE_ERROR = "Task runner process terminated unexpectedly."
# Grace window covering the spawn race: the launcher pid may already have exited
# while the real runner has not yet registered its own pid in the DataEngine DB.
_STARTUP_GRACE_SECONDS = 45.0


def _find_data_engine_db() -> str | None:
    db_path = os.path.join(trace_store.TRACES_DIR, "data_engine.db")
    if os.path.exists(db_path):
        return db_path
    db_path = os.path.join(env_utils.get_project_root(), "traces", "data_engine.db")
    return db_path if os.path.exists(db_path) else None


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        import psutil

        proc = psutil.Process(int(pid))
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except Exception:
        try:
            os.kill(int(pid), 0)
            return True
        except OSError:
            return False


def _session_tracked_by_lock(trace_id: str) -> bool:
    """True when the device-lock layer still tracks this session (queued or active owner)."""
    try:
        for q_item in DeviceExecutionLock.get_queued_tasks():
            if str(q_item.get("session_id")) == trace_id:
                return True
    except Exception:
        pass
    try:
        for owner in DeviceExecutionLock.get_active_owners().values():
            if owner.session_id and str(owner.session_id) == trace_id:
                return True
    except Exception:
        pass
    return False


def _reconcile_task_state(
    trace_id: str, status_data: dict[str, Any]
) -> tuple[str, int | None, bool]:
    """Reconcile status.json with the DataEngine DB and real process liveness.

    The DB session row is written by the runner process itself (``os.getpid()``),
    so its pid and terminal status outrank the pid recorded at spawn time -- on
    Windows that spawn pid is a short-lived venv launcher, not the runner. A dead
    or missing pid alone never fails a task that the DB, the device-lock queue,
    or an active lock owner still reports as alive.

    Returns ``(current_status, pid, is_alive)`` and persists any correction back
    to status.json (including recovering from a previously misreported failure).
    """
    current_status = status_data.get("status", "unknown")
    pid = status_data.get("pid")
    dirty = False

    # "success" is a legacy alias for the canonical "completed" terminal
    # status; consumers of this reconcile must only ever see "completed".
    if current_status == "success":
        current_status = "completed"
        status_data["status"] = "completed"
        dirty = True

    db_status: str | None = None
    db_pid: int | None = None
    db_path = _find_data_engine_db()
    if db_path:
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT status, pid FROM sessions WHERE session_id = ? ORDER BY start_time DESC LIMIT 1",
                (trace_id,),
            ).fetchone()
            conn.close()
            if row:
                db_status = row["status"]
                db_pid = row["pid"]
        except Exception:
            pass

    if db_pid and db_pid != pid:
        pid = db_pid
        status_data["pid"] = pid
        dirty = True

    # The DB terminal verdict only fills in a status.json that has not reached a
    # terminal state itself, or corrects a liveness-inferred failure. It never
    # overrides a completed result or an explicit user cancellation.
    liveness_failed = (
        current_status == "failed" and status_data.get("error") == _LIVENESS_FAILURE_ERROR
    )
    if db_status in ("completed", "success", "failed", "cancelled") and (
        current_status in ("running", "pending") or liveness_failed
    ):
        canonical = "completed" if db_status in ("completed", "success") else db_status
        if current_status != canonical:
            current_status = canonical
            status_data["status"] = canonical
            if canonical != "failed" and status_data.get("error") == _LIVENESS_FAILURE_ERROR:
                status_data["error"] = None
            if not status_data.get("end_time"):
                status_data["end_time"] = time.time()
            dirty = True
        is_alive = False
    elif current_status in ("running", "pending"):
        if not pid:
            # No pid recorded (daemon dispatch never writes one): liveness cannot be
            # inferred, so the task is assumed alive -- matching the historical
            # behavior of only checking liveness when a pid exists.
            is_alive = True
        else:
            is_alive = _pid_alive(pid)
        if not is_alive:
            if _session_tracked_by_lock(trace_id):
                # Still queued for a device or owned by a live lock holder.
                is_alive = True
            elif db_status in ("running", "pending") and db_pid:
                # The runner registered itself and its process is gone: truly dead.
                current_status = "failed"
                _mark_liveness_failure(trace_id, status_data)
            elif (time.time() - (status_data.get("start_time") or 0)) < _STARTUP_GRACE_SECONDS:
                is_alive = True
            else:
                current_status = "failed"
                _mark_liveness_failure(trace_id, status_data)
    else:
        is_alive = False

    if dirty:
        trace_store.write_status(trace_id, status_data)
    return current_status, pid, is_alive


def _mark_liveness_failure(trace_id: str, status_data: dict[str, Any]) -> None:
    trace_store.update_trace_status(trace_id, "failed", error=_LIVENESS_FAILURE_ERROR)
    status_data["status"] = "failed"
    status_data["error"] = _LIVENESS_FAILURE_ERROR
    conv_id = status_data.get("conversation_id")
    if conv_id:
        try:
            notify(
                conversation_id=conv_id,
                message=(
                    f"Artemis background task died unexpectedly for trace '{trace_id}'. "
                    f"Error: {_LIVENESS_FAILURE_ERROR}"
                ),
                event_type="failed",
                payload={"trace_id": trace_id, "error": _LIVENESS_FAILURE_ERROR},
            )
        except Exception:
            pass


@mcp.tool()
def mobile_manage_task(
    action: str,
    trace_id: str,
    instruction: str | None = None,
    release_loop: bool = False,
) -> dict[str, Any]:
    """Manages the lifecycle and retrieves the status of a background mobile automation task.

    This is your primary diagnostic and control tool for tasks started by
    `mobile_run_task`. You MUST use it to poll the task status when your
    1-minute fallback timer triggers (as required by `mobile_run_task`). You
    can also steer a subagent that is stuck or off-track, or abort a task.

    ### Actions
    - **'status'**: Returns `trace_id`, `status` ('running'/'completed'/'failed'/
      'cancelled'), `device_serial` (which phone owns this task in multi-device
      setups), `task_desc`, `model`, `elapsed_seconds`, a `test_summary` (when
      the run declared verification check items: machine-readable
      passed/failed/inconclusive/unchecked counts plus failed-item details —
      no need to parse report prose), and `progress` (Flash:
      current turn, latest thought/action — intervene if turns climb without
      progress or the thought indicates it is stuck; Pro: the active task plan
      — check it still aligns with your goal).
    - **'inject_instruction'**: Injects real-time guidance mid-flight when the
      subagent errs, stalls, or loops. Applied at the start of the next
      planning turn (Pro) or the next reactive loop (Flash). Requires
      `instruction` (unless `release_loop=True`). To end a continuous
      monitoring task ([Loop:continuous] milestone) gracefully, you MUST pass
      `release_loop=True` — this is the only signal that unlocks the
      milestone's completion; natural-language "please stop" phrasing in
      `instruction` is NOT interpreted as a stop signal.
    - **'stop'**: Forcefully terminates the subagent, immediately halting
      device interactions and releasing the device. Use when the task is done,
      irreparably broken, or running out of control. Prefer
      `inject_instruction` with `release_loop=True` when a monitoring task
      should wind down cleanly instead of being killed.

    Args:
        action: `"status"`, `"inject_instruction"`, or `"stop"`.
        trace_id: The task's session identifier from `mobile_run_task`.
        instruction: Guidance string; required for `inject_instruction`
          (optional when `release_loop=True`), omit otherwise.
        release_loop: With `inject_instruction`: explicit user stop signal
          that authorizes the subagent to complete continuous monitoring
          loops and finish the task gracefully.
    """
    status_data = trace_store.read_status(trace_id)
    if not status_data:
        return {
            "trace_id": trace_id,
            "status": "unknown",
            "message": f"Trace ID '{trace_id}' not found.",
        }

    current_status, pid, is_alive = _reconcile_task_state(trace_id, status_data)

    trace_dir = trace_store.get_trace_dir(trace_id)

    if action == "status":
        start_time = status_data.get("start_time")
        end_time = status_data.get("end_time") or time.time()
        elapsed = round(end_time - start_time, 1) if start_time else 0

        project_root = env_utils.get_project_root()
        db_path = os.path.join(trace_store.TRACES_DIR, "data_engine.db")
        if not os.path.exists(db_path):
            db_path = os.path.join(project_root, "traces", "data_engine.db")

        device_serial = status_data.get("device_serial")
        if not device_serial and os.path.exists(db_path):
            try:
                conn_dev = sqlite3.connect(db_path)
                conn_dev.row_factory = sqlite3.Row
                cur_dev = conn_dev.cursor()
                cur_dev.execute(
                    "SELECT device_info FROM sessions WHERE session_id = ? OR pid = ? ORDER BY start_time DESC LIMIT 1",
                    (trace_id, pid),
                )
                row_dev = cur_dev.fetchone()
                if row_dev and row_dev["device_info"]:
                    try:
                        d_info = json.loads(row_dev["device_info"])
                        if isinstance(d_info, dict) and d_info.get("device_id"):
                            device_serial = d_info["device_id"]
                            status_data["device_serial"] = device_serial
                            trace_store.write_status(trace_id, status_data)
                    except Exception:
                        pass
                conn_dev.close()
            except Exception:
                pass

        if not device_serial and pid:
            try:
                owners = DeviceExecutionLock.get_active_owners()
                for clean_id, owner in owners.items():
                    if owner.pid == pid or owner.session_id == trace_id:
                        device_serial = owner.device_id
                        status_data["device_serial"] = device_serial
                        trace_store.write_status(trace_id, status_data)
                        break
            except Exception:
                pass

        response: dict[str, Any] = {
            "trace_id": trace_id,
            "status": current_status,
            "device_serial": device_serial,
            "elapsed_seconds": elapsed,
            "task_desc": status_data.get("task_desc"),
            "model": status_data.get("model"),
            "stdout_log": os.path.join(trace_dir, "stdout.log"),
            "stderr_log": os.path.join(trace_dir, "stderr.log"),
        }
        if status_data.get("model", "").lower() != "flash":
            response["notes_dir"] = os.path.join(trace_dir, "notes")

        if current_status == "failed":
            response["error"] = status_data.get("error")
        elif current_status == "completed":
            response["result"] = status_data.get("result")

        # Machine-readable test summary (written by exit settlement): callers
        # get assertion results without parsing report prose.
        for candidate in (
            os.path.join(trace_store.TRACES_DIR, trace_id, "run_outcome.json"),
            os.path.join(trace_dir, "run_outcome.json"),
        ):
            if os.path.exists(candidate):
                try:
                    with open(candidate, encoding="utf-8") as f:
                        run_outcome = json.load(f)
                    tests = run_outcome.get("tests") or {}
                    response["test_summary"] = {
                        "task_status": run_outcome.get("task_status"),
                        **tests,
                    }
                except Exception:
                    pass
                break

        if current_status in ("running", "pending") and is_alive:
            progress: dict[str, Any] = {}

            project_root = env_utils.get_project_root()
            db_path = os.path.join(trace_store.TRACES_DIR, "data_engine.db")
            if not os.path.exists(db_path):
                db_path = os.path.join(project_root, "traces", "data_engine.db")

            session_id = None
            if os.path.exists(db_path):
                try:
                    conn = sqlite3.connect(db_path)
                    conn.row_factory = sqlite3.Row
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT session_id FROM sessions WHERE pid = ? ORDER BY start_time DESC LIMIT 1",
                        (pid,),
                    )
                    session_row = cursor.fetchone()
                    if session_row:
                        session_id = session_row["session_id"]
                    conn.close()
                except Exception as db_err:
                    print(f"Error querying data_engine.db for session: {db_err}", file=sys.stderr)

            if session_id:
                if status_data.get("model", "").lower() == "flash":
                    progress["phase"] = "running_flash_loop"
                    if os.path.exists(db_path):
                        try:
                            conn2 = sqlite3.connect(db_path)
                            conn2.row_factory = sqlite3.Row
                            cur2 = conn2.cursor()
                            cur2.execute(
                                "SELECT count(*) as cnt FROM traces WHERE session_id = ? AND type = 'llm_call'",
                                (session_id,),
                            )
                            row_cnt = cur2.fetchone()
                            if row_cnt:
                                progress["current_turn"] = row_cnt["cnt"]

                            cur2.execute(
                                "SELECT payload FROM traces WHERE session_id = ? AND type in ('raw_thinking', 'llm_call') "
                                "AND status = 'success' ORDER BY timestamp DESC LIMIT 1",
                                (session_id,),
                            )
                            row_thought = cur2.fetchone()
                            if row_thought and row_thought["payload"]:
                                try:
                                    p_obj = json.loads(row_thought["payload"])
                                    if isinstance(p_obj, dict):
                                        if "thought" in p_obj:
                                            progress["latest_thought"] = p_obj["thought"]
                                        elif (
                                            "response" in p_obj
                                            and isinstance(p_obj["response"], list)
                                            and p_obj["response"]
                                        ):
                                            progress["latest_thought"] = p_obj["response"][0].get(
                                                "text", ""
                                            )
                                except Exception:
                                    pass

                            cur2.execute(
                                "SELECT name, payload FROM traces WHERE session_id = ? AND type = 'tool' "
                                "AND status = 'success' ORDER BY timestamp DESC LIMIT 1",
                                (session_id,),
                            )
                            row_action = cur2.fetchone()
                            if row_action:
                                act_name = row_action["name"]
                                act_payload = {}
                                if row_action["payload"]:
                                    try:
                                        act_payload = json.loads(row_action["payload"]).get(
                                            "args", {}
                                        )
                                    except Exception:
                                        pass
                                progress["latest_action"] = (
                                    f"{act_name}({act_payload})" if act_payload else act_name
                                )
                            conn2.close()
                        except Exception as q_err:
                            print(
                                f"Error querying flash progress from data_engine.db: {q_err}",
                                file=sys.stderr,
                            )
                else:
                    plan_content = None
                    plan_path = os.path.join(
                        trace_store.TRACES_DIR, session_id, "notes", "task_plan.md"
                    )
                    if not os.path.exists(plan_path):
                        plan_path = os.path.join(trace_dir, "notes", "task_plan.md")
                    if os.path.exists(plan_path):
                        try:
                            with open(plan_path, encoding="utf-8") as f:
                                plan_content = f.read()
                        except Exception:
                            pass
                    progress["task_plan"] = plan_content
            else:
                progress["phase"] = "initializing"
                progress["status_message"] = "Spawning background runner..."

            response["progress"] = progress

        return response

    elif action == "stop":
        if current_status not in ("running", "pending"):
            return {
                "trace_id": trace_id,
                "status": current_status,
                "message": f"Cannot stop task because it is already in a terminal state: {current_status}",
            }

        # 1. First attempt graceful cancellation via unified Daemon if available
        stopped_via_daemon = False
        if os.environ.get("ARTEMIS_STANDALONE") != "1":
            try:
                from artemis.runtime import is_daemon_running, stop_task_on_daemon

                if is_daemon_running():
                    stopped_via_daemon = stop_task_on_daemon(trace_id)
            except Exception:
                pass

        # 2. Cancel queue reservation if present
        queue_ticket = status_data.get("queue_ticket")
        if queue_ticket:
            try:
                DeviceExecutionLock.cancel_reservation(queue_ticket)
            except Exception:
                pass

        # 3. If process PID is missing, try to resolve from active device owners
        if not pid:
            try:
                active_owners = DeviceExecutionLock.get_active_owners()
                for dev_owner in active_owners.values():
                    if dev_owner.session_id and str(dev_owner.session_id) == trace_id:
                        pid = dev_owner.pid
                        status_data["pid"] = pid
                        trace_store.write_status(trace_id, status_data)
                        break
            except Exception:
                pass

        # 4. If process PID could not be determined and not stopped via daemon or queue
        if not pid and not stopped_via_daemon and not queue_ticket:
            return {
                "trace_id": trace_id,
                "status": current_status,
                "message": "Process ID (PID) is missing from the task status. Cannot stop task.",
            }

        # 5. If process PID is available, terminate the process tree
        if pid:
            try:
                if sys.platform == "win32":
                    if not process_supervisor.terminate_tree(pid):
                        import psutil

                        if psutil.pid_exists(pid):
                            raise RuntimeError(
                                f"Failed to terminate Windows process tree rooted at {pid}"
                            )
                    DeviceExecutionLock.cleanup_stale_locks()
                else:
                    os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except Exception as e:
                if not stopped_via_daemon:
                    return {
                        "trace_id": trace_id,
                        "status": current_status,
                        "message": f"Failed to terminate process {pid}: {e}",
                    }

        trace_store.update_trace_status(
            trace_id, "cancelled", error="Task aborted by user request."
        )
        return {
            "trace_id": trace_id,
            "status": "cancelled",
            "message": f"Successfully stopped background task '{trace_id}'.",
        }

    elif action == "inject_instruction":
        if not instruction and release_loop:
            instruction = (
                "The user has requested to stop the continuous monitoring loop."
                " Wrap up gracefully and complete the task."
            )
        if not instruction:
            return {
                "trace_id": trace_id,
                "status": current_status,
                "message": "Missing required argument 'instruction' for action 'inject_instruction'.",
            }

        if current_status not in ("running", "pending"):
            return {
                "trace_id": trace_id,
                "status": current_status,
                "message": f"Cannot inject instruction because task is in a terminal state: {current_status}",
            }

        if not os.path.exists(trace_dir):
            return {
                "trace_id": trace_id,
                "status": current_status,
                "message": f"Trace directory not found: {trace_dir}",
            }

        instruction_path = os.path.join(trace_dir, "injected_instruction.json")
        try:
            with open(instruction_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "instruction": instruction,
                        "release_loop": bool(release_loop),
                        "timestamp": time.time(),
                        "status": "pending",
                    },
                    f,
                    indent=2,
                )
            suffix = " (with release_loop stop signal)" if release_loop else ""
            return {
                "trace_id": trace_id,
                "status": current_status,
                "message": f"Successfully injected instruction{suffix}: '{instruction}'",
            }
        except Exception as e:
            return {
                "trace_id": trace_id,
                "status": current_status,
                "message": f"Failed to inject instruction: {e}",
            }

    else:
        return {
            "trace_id": trace_id,
            "status": current_status,
            "message": f"Invalid action '{action}'. Supported actions are 'status', 'stop', and 'inject_instruction'.",
        }
