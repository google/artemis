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

import os
import subprocess
from typing import Any
import uuid

from mcp_server.base import mcp
from mcp_server.utils import env_utils, trace_store
from artemis.runtime import DeviceExecutionLock


@mcp.tool()
def mobile_run_task(
    task_desc: str,
    conversation_id: str,
    model: str = "Flash",
    locked_app_package: str | None = None,
    app_path: str | None = None,
    expected_output_desc: str | None = None,
    device_serial: str | None = None,
) -> dict[str, Any]:
    """Starts a specialized mobile UI automation subagent to autonomously plan and execute tasks on a connected Android device.

    With this tool, you can delegate complex mobile workflows and UI
    interactions to a background agent.
    This tool supports two execution models: **Flash** (for rapid,
    straightforward UI scripts) and **Pro** (for complex, exploratory tasks
    requiring deep reasoning).

    ### Device Selection & Multi-Device Support (CRITICAL)
    ARTEMIS supports multi-device execution and provides two device selection modes:
    1. **Direct Device Specification**: Specify `device_serial` explicitly
       (e.g., `device_serial="63191FDKX00062"` or `device_serial="emulator-5554"`).
       Tasks targeting different devices run concurrently via per-device locking.
    2. **Automatic Device Selection**: If `device_serial` is omitted or `None`,
       ARTEMIS will automatically select the connected device or allocate an idle device
       from the device pool.

    **Device Selection Policy & Diagnostics**:
    - **Prioritize User Choice**: When multiple devices or emulators are connected (or when
      user intent is not explicitly specified), **ALWAYS PRIORITIZE ASKING THE USER** to
      choose or confirm the target device before launching tasks.
    - **Device Diagnosis with `adb devices`**: Always use `adb devices` (or `adb devices -l`)
      to inspect connected hardware, verify authorization states (`device` vs `unauthorized`),
      and retrieve active device serials when diagnosing or before delegating tasks.

    ### Model Selection Guide (CRITICAL FOR ROUTING)
    You MUST choose the appropriate `model` based on the task complexity:
    - **Flash**: Best for simple, deterministic tasks that can be completed
    within **30 UI steps**. You should prioritize using this model to complete
    the task.
      *Limitations*: Flash CANNOT analyze video recordings, query system logs,
      execute arbitrary ADB shell commands, maintain a structured task plan, or
      write custom execution notes/reports. Do not use Flash if the task
      requires exploration, complex error recovery, continuous monitoring/polling
      (due to step count limits and lack of state persistence), or outputting
      large amounts of detailed information (as Flash cannot access the
      notes/scratchpad system to retain extensive details across steps; use
      **Pro** instead).
    - **Pro**: Required for complex, non-linear, or exploratory tasks (e.g.,
    browsing deep lists, troubleshooting app crashes, tasks requiring multi-step
    planning), continuous monitoring or polling tasks (e.g., periodically checking
    UI state, polling until specific conditions or events occur, watching progress,
    or waiting for background changes), OR when you need the subagent to extract,
    remember, and output large amounts of detailed information. Pro is slower but
    fully featured.
      *Capabilities*: Supports full video analysis, system log querying, ADB
      shell access, robust monitoring/polling execution loops with multi-agent
      planning, and generating custom text outputs/reports via the expected
      output agent and structured notes (`notes_dir`).

    Regardless of the model, precise timing for waiting actions cannot be
    guaranteed due to latency. If you need to implement a wait, please note that
    the total waiting time comprises both the time required for the Artemis large
    model to generate output and the actual waiting duration. (For Flash, the
    interval between output steps is typically 3-7 seconds, whereas Pro requires
    25-35 seconds.)

    For long-term repetitive tasks, run the workflow once with this tool to
    discover the path, then author a dedicated automation script instead of
    repeatedly delegating.

    This tool is non-blocking and will immediately return a dictionary
    containing:
    - `trace_id`: The unique session identifier of the task for status tracking
    and inspection.
    - `device_serial`: The assigned target device serial (or "auto-select").
    - `notes_dir`: (Pro Model Only) The directory where the subagent's execution
    files/notes are located.
    - `stdout_log`: The path to the execution framework log.
    - `stderr_log`: The path to the error log containing critical failures and
    Python tracebacks (essential for debugging).

    Upon task completion (success or failure), the system will trigger a
    Reactive Wakeup to notify you.

    CRITICAL BEHAVIORAL RULES:
    1. **Fallback Timer Constraint**: You **MUST** set a background timer to
    check the task status at least once every 1 minute as a safety fallback.
    2. **Post-Task Inspection**: After completion, retrieve and inspect the
    execution logs (`stderr_log`/`stdout_log`) to verify success.
    3. **Inspecting Notes (Pro Model Only)**: If running with Pro, you can
    inspect the notes/plans recorded in `notes_dir` to extract critical
    execution results passed back by the subagent.

    Args:
        task_desc: A clear, highly detailed, actionable task description for the
          mobile subagent. Since execution is autonomous and one-shot, provide
          all necessary context, exact UI actions to take, and clear termination
          conditions. If `model="Pro"`, you can explicitly instruct the subagent
          on what specific information it must record in the notes.
        conversation_id: Your current active Conversation ID. Critical for
          routing the wakeup notification.
        model: Must be either `"Flash"` or `"Pro"`. - Choose `"Flash"` for fast,
          simple UI tasks under 30 steps without log/video analysis or
          monitoring/polling needs. - Choose `"Pro"` for complex tasks requiring
          planning, ADB shell commands, logs, video exploration, or continuous
          monitoring and polling tasks.
        locked_app_package: Optional package name of the app to lock execution
          to. The agent will auto-launch and restrict actions to this app.
        app_path: Optional path to a local APK to install on the device before
          running the task.
        expected_output_desc: Optional (Pro Model Only). If provided, a
          summarization agent will aggregate the interaction history
          (motivation, actions, videos) into a summary report saved as
          "output.md" in `notes_dir`. Ignored if `model="Flash"`.
        device_serial: Optional Android device serial number (e.g. "63191FDKX00062"
          or "emulator-5554"). If specified, binds execution strictly to that device,
          allowing multi-device concurrent automation. If omitted, ARTEMIS will
          automatically select an available device. Prioritize letting the user select
          the target device if multiple devices are attached.
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

    # 3. Dispatch via unified Artemis Daemon scheduler if available (unless standalone forced)
    if os.environ.get("ARTEMIS_STANDALONE") != "1":
        try:
            from artemis.runtime import ensure_daemon_running, submit_task_to_daemon

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
        except Exception as exc:
            import logging
            logging.getLogger("mcp_server").warning(
                f"Daemon dispatch failed, falling back to standalone runner: {exc}"
            )

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
            conversation_id,
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
            from artemis.config.runtime import read_ipc_port

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
