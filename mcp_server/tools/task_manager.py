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


@mcp.tool()
def mobile_manage_task(
    action: str, trace_id: str, instruction: str | None = None
) -> dict[str, Any]:
    """Manages the lifecycle and retrieves the status of a background mobile automation task.

    This is your primary diagnostic and control tool. You MUST use this tool to
    poll the task status
    when your 1-minute fallback timer triggers (as requested by
    `mobile_run_task`). You can also use
    it to gracefully abort a task or forcefully correct the subagent's behavior
    mid-flight.

    ### Available Actions:
    - **'status'**: Retrieves execution progress, elapsed time, and metadata.
    Returns a JSON object.
        * **How to use the output**:
            - For **Pro Model**, check `progress.task_plan` to see if the
            agent's long-term plan aligns with your goal.
            - For **Flash Model**, monitor `current_turn`, `latest_action`, and
            `latest_thought`. If the turn count increases rapidly without
            progressing, or the thought indicates it is stuck, you should
            intervene.

    - **'inject_instruction'**: Injects real-time guidance when the subagent
    makes a mistake, gets stuck, or loops.
        * **Pro Model**: Applied at the start of the next turn's planning phase.
        * **Flash Model**: Applied at the start of the next reactive execution
        loop.
        * *Note*: You MUST provide the `instruction` argument for this action.

    - **'stop'**: Forcefully terminates the subagent (Pro or Flash), immediately
    halting device interactions and releasing the device. Use this if the task
    is complete, irreparably broken, or running out of control.

    Args:
        action: STR. **REQUIRED**. The management action to perform. Must be
          exactly `"status"`, `"stop"`, or `"inject_instruction"`.
        trace_id: STR. **REQUIRED**. The unique session identifier of the task
          (returned by `mobile_run_task`).
        instruction: STR. **CONDITIONAL**. The real-time correction or guidance
          string. - You MUST provide this if `action="inject_instruction"`. -
          You MUST leave this empty or omit it if `action="status"` or
          `action="stop"`.
    """
    status_data = trace_store.read_status(trace_id)
    if not status_data:
        return {
            "trace_id": trace_id,
            "status": "unknown",
            "message": f"Trace ID '{trace_id}' not found.",
        }

    current_status = status_data.get("status", "unknown")
    pid = status_data.get("pid")
    is_alive = False

    if pid and current_status in ("running", "pending"):
        try:
            os.kill(pid, 0)
            is_alive = True
        except OSError:
            current_status = "failed"
            error_text = "Task runner process terminated unexpectedly."
            trace_store.update_trace_status(trace_id, "failed", error=error_text)
            conv_id = status_data.get("conversation_id")
            if conv_id:
                try:
                    notify(
                        conversation_id=conv_id,
                        message=f"Artemis background task died unexpectedly for trace '{trace_id}'. Error: {error_text}",
                        event_type="failed",
                        payload={"trace_id": trace_id, "error": error_text},
                    )
                except Exception:
                    pass
            status_data = trace_store.read_status(trace_id) or status_data

    trace_dir = trace_store.get_trace_dir(trace_id)

    if action == "status":
        start_time = status_data.get("start_time")
        end_time = status_data.get("end_time") or time.time()
        elapsed = round(end_time - start_time, 1) if start_time else 0

        response: dict[str, Any] = {
            "trace_id": trace_id,
            "status": current_status,
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

        if not pid:
            return {
                "trace_id": trace_id,
                "status": current_status,
                "message": "Process ID (PID) is missing from the task status. Cannot stop task.",
            }

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
                # Preserve the existing POSIX signal behavior.
                os.kill(pid, signal.SIGTERM)
            trace_store.update_trace_status(
                trace_id, "cancelled", error="Task aborted by user request."
            )
            return {
                "trace_id": trace_id,
                "status": "cancelled",
                "message": f"Successfully sent termination signal to background process {pid}.",
            }
        except ProcessLookupError:
            trace_store.update_trace_status(
                trace_id, "cancelled", error="Task aborted (process was already stopped)."
            )
            return {
                "trace_id": trace_id,
                "status": "cancelled",
                "message": f"Process {pid} was not found; it may have already exited. Status marked as cancelled.",
            }
        except Exception as e:
            return {
                "trace_id": trace_id,
                "status": current_status,
                "message": f"Failed to terminate process {pid}: {e}",
            }

    elif action == "inject_instruction":
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
                        "timestamp": time.time(),
                        "status": "pending",
                    },
                    f,
                    indent=2,
                )
            return {
                "trace_id": trace_id,
                "status": current_status,
                "message": f"Successfully injected instruction: '{instruction}'",
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
