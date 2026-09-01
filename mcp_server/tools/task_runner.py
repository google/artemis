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

"""MCP Tool: mobile_run_task."""

import logging
import os
import subprocess
import sys
import threading
import time
from typing import Any
import uuid

from mcp_server.base import mcp
from mcp_server.notifiers import notify
from mcp_server.utils import env_utils
from artemis.config.runtime import read_ipc_port
from artemis.runtime import (
    DeviceExecutionLock,
    device_pool,
    ensure_daemon_running,
    submit_task_to_daemon,
    trace_store,
)

# Seconds the spawned runner gets to finish its imports and open its log files
# before the spawn is declared dead. Normal startup creates stdout.log within a
# few seconds; a runner that produced no logs by this deadline is hung in
# interpreter/DLL startup and will never recover (observed on Windows when the
# MCP server process context degrades).
SPAWN_WATCHDOG_SECONDS = 60


def _kill_process_tree(pid: int) -> None:
    """Terminate a spawned runner and its children (the venv shim re-execs python)."""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def _watch_spawn(
    trace_id: str,
    pid: int,
    queue_ticket: str,
    conversation_id: str | None,
    deadline_seconds: float = SPAWN_WATCHDOG_SECONDS,
    poll_interval: float = 2.0,
) -> bool:
    """Watch a freshly spawned runner; kill and fail the task if it never boots.

    The runner opens stdout.log/stderr.log immediately after module imports, so
    their absence after the deadline means the process is wedged before running
    any task code. Without this, a wedged spawn leaves the task in 'running'
    forever and holds its device-queue ticket.

    Returns True when the runner booted (or was already terminal), False when it
    was declared hung and killed.
    """
    trace_dir = trace_store.get_trace_dir(trace_id)
    log_paths = (
        os.path.join(trace_dir, "stdout.log"),
        os.path.join(trace_dir, "stderr.log"),
    )

    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        if any(os.path.exists(p) for p in log_paths):
            return True  # runner booted normally
        status_data = trace_store.read_status(trace_id)
        if status_data and status_data.get("status") not in ("running", "pending"):
            return True  # already terminal (e.g. stopped by the user)
        time.sleep(poll_interval)

    if any(os.path.exists(p) for p in log_paths):
        return True
    status_data = trace_store.read_status(trace_id)
    if status_data and status_data.get("status") not in ("running", "pending"):
        return True

    error_text = (
        f"Background runner (pid {pid}) produced no logs within "
        f"{deadline_seconds:.0f}s of spawn and was killed: the process hung "
        "during interpreter startup. Restarting the MCP server usually clears "
        "this; the task can be resubmitted afterwards."
    )
    _kill_process_tree(pid)
    DeviceExecutionLock.cancel_reservation(queue_ticket)
    trace_store.update_trace_status(trace_id, "failed", error=error_text)
    if conversation_id:
        try:
            notify(
                conversation_id=conversation_id,
                message=f"Artemis task '{trace_id}' failed to start: {error_text}",
                event_type="failed",
                payload={"trace_id": trace_id, "error": error_text},
            )
        except Exception:
            pass
    return False


def _start_spawn_watchdog(
    trace_id: str,
    pid: int,
    queue_ticket: str,
    conversation_id: str | None,
) -> None:
    """Run _watch_spawn on a daemon thread so the tool call returns immediately."""
    threading.Thread(
        target=_watch_spawn,
        args=(trace_id, pid, queue_ticket, conversation_id),
        name=f"spawn-watchdog-{trace_id[:8]}",
        daemon=True,
    ).start()


def _validate_device_serial(device_serial: str) -> dict[str, Any] | None:
    """Reject a task whose explicitly requested device is not attached and authorized.

    Returns a failure response dict when the serial must be rejected, or None when
    the device is usable. If device enumeration itself fails (no adb available),
    validation is skipped rather than blocking task submission.
    """
    try:
        # The shared validator fails open on an indeterminate/empty enumeration:
        # the task proceeds and fails downstream with a clear no-device error.
        detail = device_pool.validate_explicit_serial(device_serial)
    except Exception:
        return None
    if detail is None:
        return None
    return {
        "status": "failed",
        "error": detail,
        "message": (
            f"{detail} Task rejected to prevent execution on an unintended device. "
            "Run `adb devices -l` to inspect attached hardware, then resubmit with a "
            "valid serial (or omit device_serial for automatic selection)."
        ),
    }


@mcp.tool()
def mobile_run_task(
    task_desc: str,
    conversation_id: str | None = None,
    model: str = "Flash",
    locked_app_package: str | None = None,
    app_path: str | None = None,
    expected_output_desc: str | None = None,
    device_serial: str | None = None,
) -> dict[str, Any]:
    """Starts an autonomous mobile UI automation subagent on a connected Android device.

    Delegates a mobile workflow to a background agent. Non-blocking: returns
    immediately with `trace_id` (for `mobile_manage_task` / `mobile_inspect_trace`),
    `device_serial`, `stdout_log`/`stderr_log` paths, and `notes_dir` (Pro only).
    On completion (success or failure) a Reactive Wakeup notifies you — but only
    when `conversation_id` was provided; without it, rely on your fallback timer.

    ### Model selection (routing)
    - **Flash** (default, preferred): simple, deterministic tasks completable
      within ~30 UI steps. No log/video analysis, no ADB shell, no persistent
      plan or notes — unsuitable for exploration, complex error recovery,
      monitoring/polling, or tasks that must report back large amounts of
      detail.
    - **Pro**: complex, exploratory, or multi-branch tasks; continuous
      monitoring/polling; tasks needing ADB shell, system logs, video analysis,
      multi-step planning, or detailed written output (via notes and
      `expected_output_desc`).

    Timing is not precise: the agent's own inference adds ~5 s per step (Flash)
    or ~30 s per turn (Pro) on top of any requested waits — account for this
    when the task involves waiting. For recurring workflows, run once to
    discover the path, then author a deterministic script instead of
    re-delegating.

    ### Follow-up protocol (CRITICAL)
    1. **Fallback timer**: you MUST set a background timer and check the task
       status via `mobile_manage_task` at least once every 1 minute — mobile
       tasks can stall silently, so never rely on the completion wakeup alone.
    2. **Post-task inspection**: after completion, inspect the execution logs
       to verify success (`stderr_log` carries critical failures and Python
       tracebacks — essential for debugging).
    3. **Pro notes**: for Pro runs, read `notes_dir` to extract the results and
       plans the subagent recorded.

    Args:
        task_desc: Clear, self-contained task description with all context,
          exact UI actions, and termination conditions (execution is one-shot).
          For Pro, may also specify what the subagent must record in its notes.
        conversation_id: Optional conversation ID used to route the completion
          wakeup notification back to the caller. Omit if unknown.
        model: `"Flash"` or `"Pro"` — see model selection above.
        locked_app_package: Optional package name to lock execution to; the
          agent auto-launches it and restricts actions to that app.
        app_path: Optional local APK path to install before running.
        expected_output_desc: Optional, Pro only. If set, a summarization agent
          writes a report to `output.md` in `notes_dir`. Ignored for Flash.
        device_serial: Optional device serial (e.g. "emulator-5554") to bind
          execution to a specific device; distinct devices run concurrently.
          If omitted, an available device is selected automatically. When
          several devices are attached, confirm the target with the user first
          (`adb devices -l` lists serials and authorization states).
    """
    # 0. Validate and normalize model
    if model.lower() not in ("flash", "pro"):
        raise ValueError(f"Invalid model '{model}'. Must be either 'Flash' or 'Pro'.")
    canonical_model = "Flash" if model.lower() == "flash" else "Pro"

    # 1. Generate a unique trace_id
    trace_id = str(uuid.uuid4())

    # 2. Initialize the trace store directory and status.json
    trace_store.init_trace(
        trace_id=trace_id,
        task_desc=task_desc,
        model=canonical_model,
        conversation_id=conversation_id,
        device_serial=device_serial,
    )

    # 2b. Strict device binding: an explicitly requested serial must be attached and
    # authorized. Rejecting here prevents the task from silently running on a
    # different device than the caller asked for. Runs after init_trace so the
    # rejection carries a trace_id like every other response of this tool.
    if device_serial:
        rejection = _validate_device_serial(device_serial)
        if rejection:
            trace_store.update_trace_status(trace_id, "failed", error=rejection["error"])
            return {"trace_id": trace_id, **rejection}

    # 3. Dispatch via unified Artemis Daemon scheduler if available (unless standalone forced)
    if os.environ.get("ARTEMIS_STANDALONE") != "1":
        try:
            is_running, base_url = ensure_daemon_running(timeout=2.0, wait_ready=True)
            if is_running:
                resp = submit_task_to_daemon(
                    goal=task_desc,
                    profile=canonical_model.lower(),
                    device_serial=device_serial,
                    expected_output=expected_output_desc if canonical_model != "Flash" else None,
                    locked_app_package=locked_app_package,
                    app_path=app_path,
                    session_id=trace_id,
                    ingress="mcp",
                    conversation_id=conversation_id,
                    base_url=base_url,
                )
                if resp and resp.get("status") == "rejected":
                    rejection_error = resp.get("error") or "Task rejected by Artemis Daemon."
                    trace_store.update_trace_status(trace_id, "failed", error=rejection_error)
                    return {
                        "trace_id": trace_id,
                        "status": "failed",
                        "error": rejection_error,
                        "message": f"Task rejected by Artemis Daemon: {rejection_error}",
                    }
                if resp and resp.get("tasks"):
                    assigned_sid = resp["tasks"][0].get("session_id", trace_id)
                    trace_store.update_trace_status(
                        assigned_sid, "running", device_serial=device_serial
                    )
                    notes_dir = str(trace_store.get_trace_notes_dir(assigned_sid))
                    stdout_log = str(trace_store.get_trace_stdout_log_path(assigned_sid))
                    stderr_log = str(trace_store.get_trace_stderr_log_path(assigned_sid))
                    return {
                        "trace_id": assigned_sid,
                        "status": "running",
                        "message": (
                            f"Autonomous task '{task_desc}' enqueued via unified Artemis Daemon.\n"
                            f"Trace ID: {assigned_sid}\n"
                            f"Model: {canonical_model}\n"
                            f"Live telemetry: streaming to web workspace."
                        ),
                        "notes_dir": notes_dir,
                        "stdout_log": stdout_log,
                        "stderr_log": stderr_log,
                    }

                # Check if the task was already enqueued despite a slow or None response:
                try:
                    queued_tasks = DeviceExecutionLock.get_queued_tasks()
                    for q in queued_tasks:
                        if str(q.get("session_id")) == str(trace_id):
                            trace_store.update_trace_status(
                                trace_id, "running", device_serial=device_serial
                            )
                            return {
                                "trace_id": trace_id,
                                "status": "running",
                                "message": (
                                    f"Autonomous task '{task_desc}' enqueued via unified Artemis Daemon.\n"
                                    f"Trace ID: {trace_id}\n"
                                    f"Model: {canonical_model}\n"
                                    f"Live telemetry: streaming to web workspace."
                                ),
                                "notes_dir": str(trace_store.get_trace_notes_dir(trace_id)),
                                "stdout_log": str(trace_store.get_trace_stdout_log_path(trace_id)),
                                "stderr_log": str(trace_store.get_trace_stderr_log_path(trace_id)),
                            }
                except Exception:
                    pass

                # When Daemon is running, refuse to launch a conflicting standalone runner on the same device
                logging.getLogger("mcp_server").warning(
                    f"Daemon dispatch failed for task '{task_desc}'. Aborting dispatch to prevent runner collision."
                )
                return {
                    "trace_id": trace_id,
                    "status": "failed",
                    "error": f"Failed to enqueue task '{task_desc}' to running Artemis Daemon scheduler.",
                    "message": "Task rejected or timed out in Artemis Daemon. Standalone fallback blocked to prevent runner collision.",
                }
        except Exception as exc:
            logging.getLogger("mcp_server").warning(
                f"Daemon dispatch failed: {exc}"
            )
            return {
                "trace_id": trace_id,
                "status": "failed",
                "error": f"Daemon dispatch error: {exc}",
                "message": "Failed to dispatch task to Artemis Daemon.",
            }

    # 4. Standalone Fallback: Resolve project root, python executable, and background runner module
    project_root = env_utils.get_project_root()
    python_exe = env_utils.resolve_python_executable(project_root)
    reserve_kwargs: dict[str, Any] = {
        "description": f"MCP task: {task_desc[:120]}",
        "session_id": trace_id,
        "ingress": "mcp",
    }
    if device_serial:
        reserve_kwargs["device_id"] = device_serial
    queue_ticket = DeviceExecutionLock.reserve(**reserve_kwargs)

    # 5. Spawn the background task runner as an independent subprocess
    try:
        cmd = [
            python_exe,
            "-m",
            "mcp_server.background.task_runner",
            "--trace-id",
            trace_id,
            "--task-desc",
            task_desc,
            "--model",
            canonical_model,
            "--conversation-id",
            conversation_id or "",
        ]
        if locked_app_package:
            cmd.extend(["--locked-app-package", locked_app_package])
        if app_path:
            cmd.extend(["--app-path", app_path])
        if expected_output_desc and canonical_model != "Flash":
            cmd.extend(["--expected-output-desc", expected_output_desc])
        if device_serial:
            cmd.extend(["--device-serial", device_serial])

        env = os.environ.copy()
        env["ARTEMIS_SESSION_ID"] = trace_id
        env["ARTEMIS_TASK_INGRESS"] = "mcp"
        if device_serial:
            env["ADB_DEVICE_SERIAL"] = device_serial
            env["ARTEMIS_DEVICE_ID"] = device_serial
        env[DeviceExecutionLock.QUEUE_TICKET_ENV] = queue_ticket
        try:
            ipc_port = read_ipc_port()
            if ipc_port:
                env["ARTEMIS_IPC_PORT"] = str(ipc_port)
        except Exception:
            pass

        proc_kwargs = env_utils.get_detached_process_kwargs()
        proc = subprocess.Popen(
            cmd,
            env=env,
            cwd=project_root,
            **proc_kwargs,
        )
        transfer_kwargs: dict[str, Any] = {
            "description": f"MCP task: {task_desc[:120]}",
            "session_id": trace_id,
            "ingress": "mcp",
        }
        if device_serial:
            transfer_kwargs["device_id"] = device_serial

        DeviceExecutionLock.transfer_reservation(
            queue_ticket,
            proc.pid,
            **transfer_kwargs,
        )

        # 5. Record the PID of the spawned subprocess
        status_data = trace_store.read_status(trace_id)
        if status_data:
            status_data["pid"] = proc.pid
            status_data["queue_ticket"] = queue_ticket
            trace_store.write_status(trace_id, status_data)

        # 6. Arm a watchdog: a runner that never opens its log files is hung in
        # interpreter startup and must be killed instead of squatting on the
        # queue as 'running' forever.
        _start_spawn_watchdog(trace_id, proc.pid, queue_ticket, conversation_id)

        trace_dir = trace_store.get_trace_dir(trace_id)
        stdout_log_path = os.path.join(trace_dir, "stdout.log")
        stderr_log_path = os.path.join(trace_dir, "stderr.log")

        response_dict: dict[str, Any] = {
            "trace_id": trace_id,
            "device_serial": device_serial or "auto-select",
            "message": "Successfully started background task.",
            "stdout_log": stdout_log_path,
            "stderr_log": stderr_log_path,
        }
        if canonical_model != "Flash":
            response_dict["notes_dir"] = os.path.join(trace_dir, "notes")

        return response_dict

    except Exception as e:
        DeviceExecutionLock.cancel_reservation(queue_ticket)
        trace_store.update_trace_status(
            trace_id, "failed", error=f"Failed to spawn background runner: {e}"
        )
        return {
            "trace_id": trace_id,
            "status": "failed",
            "message": f"Error starting background task: {e}",
        }
