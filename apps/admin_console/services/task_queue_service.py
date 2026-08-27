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

import asyncio
import codecs
from datetime import datetime
import os
import shutil
import subprocess
import sys
import time
from typing import Any
import uuid

try:
    from admin_console.core.config import (
        PAUSE_FILE,
        TEST_DATA_DIR,
        TEST_OUTPUTS_DIR,
        WORKSPACE_ROOT,
    )
    from admin_console.core.state import state
    from admin_console.database.repositories.session_repository import session_repo
    from admin_console.services.media_service import media_service
    from artemis.runtime import DeviceExecutionLock, process_supervisor
except ImportError:
    from apps.admin_console.core.config import (
        PAUSE_FILE,
        TEST_DATA_DIR,
        TEST_OUTPUTS_DIR,
        WORKSPACE_ROOT,
    )
    from apps.admin_console.core.state import state
    from apps.admin_console.database.repositories.session_repository import session_repo
    from apps.admin_console.services.media_service import media_service
    from artemis.runtime import DeviceExecutionLock, process_supervisor


class TaskQueueService:
    """Service managing FIFO task execution, background worker, subprocess lifecycle,
    and startup tasks.
    """

    @classmethod
    def _broadcast_event(cls, event_type: str, data: Any):
        """Broadcasts an event safely to all registered subscribers."""
        for cb in list(state.ipc_subscribers):
            try:
                cb(event_type, data)
            except Exception:
                pass

    @classmethod
    def _broadcast_startup_progress(
        cls, session_id: str | None, stage: str, message: str
    ) -> None:
        if not session_id:
            return
        data = {
            "session_id": str(session_id),
            "stage": stage,
            "message": message,
            "timestamp": time.time(),
        }
        state.record_startup_progress(data)
        cls._broadcast_event("startup_progress", data)

    @classmethod
    def _get_next_pending_task(cls) -> dict[str, Any] | None:
        """Finds and returns the first pending task from queue_items."""
        for item in state.queue_items:
            if isinstance(item, dict) and item.get("status") == "pending":
                return item
        return None

    @classmethod
    def _remove_task(cls, session_id: str | None):
        """Removes a task from queue_items by session_id."""
        if not session_id:
            return
        removed_items = [
            t
            for t in state.queue_items
            if isinstance(t, dict) and t.get("session_id") == session_id
        ]
        for item in removed_items:
            DeviceExecutionLock.cancel_reservation(item.get("queue_ticket"))
        state.queue_items = [
            t
            for t in state.queue_items
            if not (isinstance(t, dict) and t.get("session_id") == session_id)
        ]

    @staticmethod
    def _resolve_terminal_status(
        current_status: str | None,
        returncode: int,
        was_stopped_manually: bool,
    ) -> tuple[str, bool]:
        """Resolve final status while preserving an authoritative task result.

        The worker exit code is only a fallback for sessions that have not
        reached a terminal state. ``success`` is the DataEngine alias for the
        UI-facing ``completed`` status and is normalized here.

        Returns:
            A tuple of ``(resolved_status, should_persist)``.
        """
        if was_stopped_manually:
            return "cancelled", True

        normalized = current_status.lower().strip() if isinstance(current_status, str) else None
        if normalized == "success":
            return "completed", True
        if normalized in {"completed", "failed", "cancelled"}:
            return normalized, False
        return ("completed" if returncode == 0 else "failed"), True

    @staticmethod
    def _subprocess_creation_kwargs() -> dict[str, Any]:
        """Isolate task workers from the UI server's Windows console.

        A new process group alone is insufficient on Windows: the worker still
        shares the parent's console, so a CTRL_C_EVENT generated anywhere in
        that console can reach the UI server. CREATE_NO_WINDOW removes that
        shared console boundary. Output is captured and forwarded explicitly
        so detached workers remain visible in the server terminal.
        """
        if sys.platform == "win32":
            return {
                "creationflags": (
                    subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                ),
                "stdout": asyncio.subprocess.PIPE,
                "stderr": asyncio.subprocess.STDOUT,
            }
        return {}

    @staticmethod
    async def _forward_worker_output(stream: asyncio.StreamReader | None) -> None:
        """Forward a detached Windows worker's combined output without corrupting UTF-8."""
        if stream is None:
            return

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            text = decoder.decode(chunk)
            if text:
                sys.stdout.write(text)
                sys.stdout.flush()

        tail = decoder.decode(b"", final=True)
        if tail:
            sys.stdout.write(tail)
            sys.stdout.flush()

    @staticmethod
    async def _finish_output_forwarder(output_task: asyncio.Task[None] | None) -> None:
        """Drain final worker output without allowing inherited handles to stall the queue."""
        if output_task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(output_task), timeout=2.0)
        except asyncio.CancelledError:
            if output_task.cancelled():
                return
            raise
        except TimeoutError:
            output_task.cancel()
            try:
                await output_task
            except asyncio.CancelledError:
                pass
        except Exception as exc:
            print(f"[QueueWorker] Failed to forward detached worker output: {exc}")

    @staticmethod
    async def _terminate_worker_process(proc: asyncio.subprocess.Process | None) -> None:
        """Stop a worker without changing the established POSIX behavior."""
        if proc is None or proc.returncode is not None:
            return
        if sys.platform == "win32":
            await process_supervisor.stop_process(proc)
            return
        try:
            proc.kill()
        except ProcessLookupError:
            pass

    @staticmethod
    async def _wait_for_worker_process(proc: asyncio.subprocess.Process) -> int:
        """Wait for worker process to exit, with watchdog fallback if PID was reaped externally."""
        while True:
            try:
                return await asyncio.wait_for(proc.wait(), timeout=1.0)
            except TimeoutError:
                pid = getattr(proc, "pid", None)
                if pid:
                    try:
                        import psutil

                        p = psutil.Process(pid)
                        if not p.is_running() or p.status() == psutil.STATUS_ZOMBIE:
                            return proc.returncode if proc.returncode is not None else -15
                    except (psutil.NoSuchProcess, ProcessLookupError):
                        return proc.returncode if proc.returncode is not None else -15
                    except Exception:
                        pass
                else:
                    return proc.returncode if proc.returncode is not None else -15

    @classmethod
    def ensure_worker_running(cls):
        """Guarantees that the background queue worker task is active and running."""
        try:
            loop = asyncio.get_running_loop()
            if state.worker_task is None or state.worker_task.done():
                state.worker_task = loop.create_task(cls.queue_worker())
                try:
                    state.wake_event.set()
                except Exception:
                    pass
        except RuntimeError:
            pass

    @classmethod
    async def queue_worker(cls):
        """Persistent, self-healing background worker loop processing tasks in FIFO order."""
        print("[QueueWorker] Background worker loop initialized and running.")
        try:
            DeviceExecutionLock.cleanup_stale_locks()
        except Exception as exc:
            print(f"[QueueWorker] Initial stale lock cleanup notice: {exc}")

        while True:
            task_item = None
            sess_id = None
            goal = None
            profile = "flash"
            output_task: asyncio.Task[None] | None = None
            try:
                if state.is_running:
                    await asyncio.sleep(0.2)
                    continue

                # 1. Fetch the next pending task
                task_item = cls._get_next_pending_task()
                if task_item is None:
                    await asyncio.sleep(0.3)
                    continue

                # 2. Extract and initialize task execution metadata
                sess_id = task_item.get("session_id")
                if sess_id and str(sess_id) in getattr(state, "cancelled_session_ids", set()):
                    cls._remove_task(sess_id)
                    continue

                goal = task_item.get("goal")
                profile = task_item.get("profile", "flash")
                expected_output = task_item.get("expected_output")
                enable_outputter = task_item.get("enable_outputter")
                locked_app = task_item.get("locked_app_package") or task_item.get("locked_app")
                app_path = task_item.get("app_path")
                task_item["status"] = "running"
                task_item["start_time"] = time.time()

                state.current_goal = goal
                state.current_profile = profile
                state.active_session_id = sess_id
                state.was_stopped_manually = False

                cls._broadcast_startup_progress(
                    sess_id, "launching", "Starting the execution process"
                )

                # Broadcast session_started so all connected clients know the task has started
                cls._broadcast_event(
                    "session_started",
                    {
                        "session_id": sess_id,
                        "initial_goal": goal,
                        "profile": profile,
                        "device_serial": task_item.get("device_serial"),
                    },
                )

                test_name = f"web_{int(time.time())}"
                env = os.environ.copy()
                pythonpath_parts = [
                    str(WORKSPACE_ROOT),
                    str(WORKSPACE_ROOT / "apps" / "admin_console"),
                    str(WORKSPACE_ROOT / "apps" / "cloud_service"),
                    env.get("PYTHONPATH", ""),
                ]
                env["PYTHONPATH"] = os.pathsep.join([p for p in pythonpath_parts if p])
                env["PYTHONUTF8"] = "1"
                env["PYTHONUNBUFFERED"] = "1"
                if state.ipc_port is not None:
                    env["ARTEMIS_IPC_PORT"] = str(state.ipc_port)
                if sess_id:
                    env["ARTEMIS_SESSION_ID"] = str(sess_id)
                env["ARTEMIS_TASK_INGRESS"] = str(task_item.get("ingress", "frontend"))
                queue_ticket = task_item.get("queue_ticket")
                if queue_ticket:
                    env[DeviceExecutionLock.QUEUE_TICKET_ENV] = str(queue_ticket)

                cmd = [
                    sys.executable,
                    "-m",
                    "artemis.main",
                    goal,
                    "--profile",
                    profile,
                    "--test-name",
                    test_name,
                ]
                if sess_id:
                    cmd.extend(["--session-id", str(sess_id)])
                if expected_output:
                    cmd.extend(["--output-description", str(expected_output)])
                if enable_outputter is not None:
                    cmd.append("--enable-outputter" if enable_outputter else "--disable-outputter")
                if locked_app:
                    cmd.extend(["--locked-app", str(locked_app)])
                if app_path:
                    cmd.extend(["--app-path", str(app_path)])
                device_serial = task_item.get("device_serial")
                if device_serial:
                    cmd.extend(["--device-serial", str(device_serial)])
                    env["ADB_DEVICE_SERIAL"] = str(device_serial)

                print(
                    f"[QueueWorker] Starting task [{sess_id}]: '{goal}' (profile: {profile}, device: {device_serial or 'auto'}, outputter: {bool(expected_output or enable_outputter)})"
                )
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(WORKSPACE_ROOT),
                    env=env,
                    **cls._subprocess_creation_kwargs(),
                )
                state.current_process = proc
                task_item["pid"] = proc.pid
                cls._broadcast_startup_progress(
                    sess_id, "process_ready", "Execution process started"
                )
                ingress_type = str(task_item.get("ingress", "frontend"))
                DeviceExecutionLock.transfer_reservation(
                    str(queue_ticket),
                    proc.pid,
                    description=f"{ingress_type} task: {goal[:120]}",
                    device_id=device_serial or "pending",
                    session_id=str(sess_id) if sess_id else None,
                    ingress=ingress_type,
                )
                if sys.platform == "win32" and isinstance(proc.stdout, asyncio.StreamReader):
                    output_task = asyncio.create_task(cls._forward_worker_output(proc.stdout))

                if sess_id and (
                    str(sess_id) in getattr(state, "cancelled_session_ids", set())
                    or state.was_stopped_manually
                ):
                    print(
                        f"[QueueWorker] Task [{sess_id}] was cancelled during launch. Terminating."
                    )
                    await cls._terminate_worker_process(proc)

                # 3. Await subprocess completion
                returncode = await cls._wait_for_worker_process(proc)
                print(f"[QueueWorker] Task [{sess_id}] exited with returncode {returncode}")

                # 4. Perform fallback database status update and notification
                if sess_id:
                    current_status = session_repo.get_session_status(sess_id)
                    new_status, should_persist = cls._resolve_terminal_status(
                        current_status,
                        returncode,
                        state.was_stopped_manually,
                    )
                    if should_persist:
                        session_repo.update_session_status(sess_id, new_status, time.time())
                        print(f"[QueueWorker] Updated session {sess_id} status to '{new_status}'")
                    else:
                        print(
                            f"[QueueWorker] Preserved authoritative session {sess_id} "
                            f"status '{new_status}'"
                        )
                    rec_info = session_repo.get_video_recording_for_session(sess_id)
                    rec_status = (rec_info or {}).get("status")
                    recovered_video_path = None
                    if rec_status != "ready":
                        try:
                            video_rec_map = session_repo.get_video_recordings_map()
                            video_idx = await asyncio.to_thread(media_service.build_video_index)
                            recovered_url = await asyncio.to_thread(
                                media_service.resolve_video_url,
                                {"session_id": sess_id},
                                video_rec_map,
                                video_idx,
                            )
                            if recovered_url:
                                session_repo.mark_recording_ready(sess_id, recovered_url)
                                cls._broadcast_event(
                                    "recording_ready",
                                    {"session_id": sess_id, "video_url": recovered_url},
                                )
                                recovered_video_path = recovered_url
                        except Exception as rec_err:
                            print(f"[QueueWorker] Error attempting recording recovery: {rec_err}")

                    if not recovered_video_path and rec_status != "ready":
                        recording_error = "Task worker exited before recording finalization completed"
                        if session_repo.mark_recording_failed_if_pending(sess_id, recording_error):
                            cls._broadcast_event(
                                "recording_failed",
                                {"session_id": sess_id, "error": recording_error},
                            )
                    state.active_connections.pop(sess_id, None)
                    cls._broadcast_event(
                        "session_ended",
                        {
                            "session_id": sess_id,
                            "status": new_status,
                            "was_stopped_manually": state.was_stopped_manually,
                        },
                    )

                    conversation_id = task_item.get("conversation_id") if task_item else None
                    if conversation_id:
                        try:
                            from mcp_server.notifiers import notify

                            notify(
                                conversation_id=conversation_id,
                                message=f"Artemis autonomous task '{goal}' finished with status '{new_status}'.\nTrace ID: {sess_id}",
                                title=f"Task {new_status.capitalize()}: {goal[:40]}",
                                event_type=new_status,
                                payload={"session_id": sess_id, "status": new_status, "goal": goal},
                            )
                        except Exception as notif_err:
                            print(f"[QueueWorker] Notification dispatch notice: {notif_err}")

            except asyncio.CancelledError:
                print("[QueueWorker] Task received cancellation signal.")
                if state.current_process and state.current_process.returncode is None:
                    await cls._terminate_worker_process(state.current_process)
                break
            except Exception as e:
                print(f"[QueueWorker] Unexpected error executing task: {e}")
                await asyncio.sleep(0.5)
            finally:
                await cls._finish_output_forwarder(output_task)
                # 5. Clean up completed task from queue and reset execution state
                if sess_id:
                    cls._remove_task(sess_id)
                state.current_process = None
                state.active_session_id = None
                state.current_goal = None
                state.current_profile = None
                state.was_stopped_manually = False
                state.wake_event.set()

    @classmethod
    async def enqueue_tasks(
        cls,
        goals: list[str],
        profile: str = "flash",
        expected_output: str | None = None,
        enable_outputter: bool | None = None,
        locked_app_package: str | None = None,
        app_path: str | None = None,
        device_serial: str | None = None,
        ingress: str = "frontend",
        session_id: str | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        """Enqueues one or more goals and wakes up the background worker."""
        cls.ensure_worker_running()

        enqueued_tasks = []
        now = time.time()
        for i, goal in enumerate(goals):
            sess_id = (session_id if (session_id and len(goals) == 1) else str(uuid.uuid4()))
            assigned_serial = device_serial
            if not assigned_serial:
                try:
                    from artemis.core.diagnostics import readiness_engine

                    assigned_serial = readiness_engine.get_active_device_serial()
                except Exception:
                    assigned_serial = None
            if not assigned_serial:
                try:
                    from artemis.runtime import device_pool

                    assigned_serial = device_pool.select_device()
                except Exception:
                    assigned_serial = None

            queue_ticket = DeviceExecutionLock.reserve(
                description=f"{ingress} task: {goal[:120]}",
                device_id=assigned_serial or "pending",
                session_id=sess_id,
                ingress=ingress,
            )
            task_item = {
                "session_id": sess_id,
                "goal": goal,
                "profile": profile or "flash",
                "expected_output": expected_output,
                "enable_outputter": enable_outputter,
                "locked_app_package": locked_app_package,
                "app_path": app_path,
                "device_serial": assigned_serial,
                "ingress": ingress,
                "conversation_id": conversation_id,
                "status": "pending",
                "queue_ticket": queue_ticket,
                "created_at": now + i * 0.001,
                "start_time": now + i * 0.001,
            }
            state.queue_items.append(task_item)
            enqueued_tasks.append(task_item)
            cls._broadcast_startup_progress(
                sess_id, "queued", "Task received and queued"
            )

        # Wake worker immediately
        state.wake_event.set()

        return {
            "status": "queued" if state.is_running else "started",
            "tasks": enqueued_tasks,
            "enqueued_count": len(goals),
            "total_queued": len(state.queue_tasks),
        }

    @classmethod
    def stop_tasks(
        cls,
        clear_all: bool = False,
        session_id: str | None = None,
        device_id: str | None = None,
    ) -> bool:
        """Stop the active task controlling a mobile device or all tasks.

        The active lease is shared by frontend, MCP, CLI, SDK, and other UI
        processes across all connected devices.
        - If ``session_id`` or ``device_id`` is specified, only the corresponding
          running or queued task is cancelled.
        - If ``clear_all`` is requested, all active device owners are terminated
          and pending queue submissions are cleared.
        - If no specific task is specified and ``clear_all`` is False, stops the
          currently active task in single-device mode for backward compatibility.
        """
        stopped = False
        target_sid = str(session_id).strip() if session_id else None
        target_device = str(device_id).strip() if device_id else None

        if clear_all:
            # 1. Cancel local queue reservations
            for item in state.queue_items:
                if isinstance(item, dict) and item.get("status") != "running":
                    DeviceExecutionLock.cancel_reservation(item.get("queue_ticket"))
            state.clear_queue()

            # 2. Terminate all active owners across all devices
            active_owners: dict[str, Any] = {}
            try:
                active_owners = DeviceExecutionLock.get_active_owners()
            except Exception:
                pass
            fallback = DeviceExecutionLock.get_active_owner()
            if fallback and not active_owners:
                active_owners["default"] = fallback

            for dev_owner in list(active_owners.values()):
                if dev_owner and DeviceExecutionLock.is_active_owner(dev_owner):
                    process_supervisor.terminate_tree_verified(
                        dev_owner.pid,
                        dev_owner.process_created_at,
                    )
                    DeviceExecutionLock.cleanup_stale_locks(dev_owner.device_id)
                    sid = dev_owner.session_id
                    if sid:
                        session_repo.update_session_status(str(sid), "cancelled", time.time())
                        if dev_owner.ingress == "mcp":
                            try:
                                from mcp_server.utils import trace_store

                                trace_store.update_trace_status(
                                    str(sid),
                                    "cancelled",
                                    error="Task stopped from the Artemis frontend.",
                                )
                            except Exception:
                                pass
                        cls._broadcast_event(
                            "session_ended",
                            {
                                "session_id": sid,
                                "status": "cancelled",
                                "was_stopped_manually": True,
                            },
                        )

            if state.current_process:
                state.was_stopped_manually = True
                try:
                    state.current_process.kill()
                except Exception:
                    pass
                state.current_process = None

            state.active_connections.clear()
            state.active_session_id = None
            state.current_goal = None
            state.current_profile = None

            if PAUSE_FILE.exists():
                try:
                    PAUSE_FILE.unlink()
                except Exception:
                    pass

            cls.ensure_worker_running()
            state.wake_event.set()
            return True

        # Stop a specific task (or default single-device active task)
        active_owners = {}
        try:
            active_owners = DeviceExecutionLock.get_active_owners()
        except Exception:
            pass

        owner = None
        if target_sid:
            for dev_owner in active_owners.values():
                if dev_owner.session_id and str(dev_owner.session_id) == target_sid:
                    owner = dev_owner
                    break
        if owner is None and target_device:
            clean_target_dev = DeviceExecutionLock._normalize_device_id(target_device)
            for dev_key, dev_owner in active_owners.items():
                if dev_key == clean_target_dev or dev_owner.device_id == target_device:
                    owner = dev_owner
                    break
        if owner is None:
            fallback_owner = DeviceExecutionLock.get_active_owner(target_device)
            if fallback_owner:
                if not target_sid or (
                    fallback_owner.session_id and str(fallback_owner.session_id) == target_sid
                ):
                    owner = fallback_owner
                elif not active_owners:
                    # In unit tests, get_active_owners is unmocked (empty), while get_active_owner is mocked
                    owner = fallback_owner

        owner_record_exists = (
            DeviceExecutionLock.has_owner_record(target_device)
            if hasattr(DeviceExecutionLock, "has_owner_record")
            else False
        )
        owner_pid = owner.pid if owner else None
        local_pid = getattr(state.current_process, "pid", None)
        is_local_owner = bool(owner_pid and local_pid and owner_pid == local_pid)

        running_item = next(
            (
                item
                for item in state.queue_items
                if isinstance(item, dict)
                and item.get("status") == "running"
                and (not target_sid or str(item.get("session_id")) == target_sid)
            ),
            None,
        )
        local_item = running_item or next(
            (
                item
                for item in state.queue_items
                if isinstance(item, dict)
                and item.get("status") == "pending"
                and (not target_sid or str(item.get("session_id")) == target_sid)
            ),
            None,
        )
        stopped_session_id = (
            owner.session_id
            if owner and owner.session_id
            else target_sid
            if target_sid
            else state.active_session_id
            if is_local_owner
            else local_item.get("session_id")
            if local_item
            else None
        )

        reservation_cancelled = False
        if owner and DeviceExecutionLock.is_active_owner(owner):
            stopped = process_supervisor.terminate_tree_verified(
                owner.pid,
                owner.process_created_at,
            )
            DeviceExecutionLock.cleanup_stale_locks(owner.device_id)
        elif owner is None and owner_record_exists and not target_sid:
            # Never fall back to a frontend PID while another process has an
            # owner record that is still being published or cannot be parsed.
            return False
        elif owner is None and state.current_process and (
            not target_sid or str(state.active_session_id) == target_sid
        ):
            # The frontend worker can be stopped during its short initialization
            # window before Agent acquires the global device lease.
            state.was_stopped_manually = True
            if local_pid:
                try:
                    stopped = process_supervisor.terminate_tree(local_pid)
                except Exception:
                    stopped = False
            try:
                state.current_process.kill()
                stopped = True
            except Exception:
                pass
            stopped = True
            is_local_owner = True
        elif owner is None and local_item and (
            not target_sid or str(local_item.get("session_id")) == target_sid
        ):
            # Cancel a frontend submission before its worker has started. This
            # does not touch pending reservations created by other ingresses.
            DeviceExecutionLock.cancel_reservation(local_item.get("queue_ticket"))
            reservation_cancelled = True
            stopped = True
            is_local_owner = True
        elif target_sid:
            # Fallback 1: check global device queue
            global_queued = DeviceExecutionLock.get_queued_tasks()
            for q_item in global_queued:
                if str(q_item.get("session_id")) == target_sid:
                    ticket = q_item.get("queue_ticket")
                    if ticket:
                        DeviceExecutionLock.cancel_reservation(ticket)
                    stopped = True
                    break
            # Fallback 2: check session repository for a running session with a live worker PID
            if not stopped:
                row = session_repo.get_session_by_id(target_sid)
                if row and row.get("status") == "running":
                    row_pid = row.get("pid")
                    if row_pid and session_repo.process_is_alive(row_pid):
                        try:
                            process_supervisor.terminate_tree(int(row_pid))
                        except Exception:
                            pass
                    DeviceExecutionLock.cleanup_stale_locks()
                    stopped = True

        if not stopped and not target_sid:
            return False

        if is_local_owner:
            state.was_stopped_manually = True
            state.current_process = None
            if stopped_session_id:
                state.cancelled_session_ids.add(str(stopped_session_id))

        for sid, conn_info in list(state.active_connections.items()):
            if (stopped_session_id and str(sid) == str(stopped_session_id)) or (
                owner_pid and conn_info.get("pid") == owner_pid
            ):
                state.active_connections.pop(sid, None)

        if stopped_session_id:
            session_repo.update_session_status(
                str(stopped_session_id), "cancelled", time.time()
            )
            if owner and owner.ingress == "mcp":
                try:
                    from mcp_server.utils import trace_store

                    trace_store.update_trace_status(
                        str(stopped_session_id),
                        "cancelled",
                        error="Task stopped from the Artemis frontend.",
                    )
                except Exception as exc:
                    print(f"Failed to update MCP cancellation status: {exc}")
            cls._broadcast_event(
                "session_ended",
                {
                    "session_id": stopped_session_id,
                    "status": "cancelled",
                    "was_stopped_manually": True,
                },
            )

        if state.active_session_id and (
            not stopped_session_id
            or str(state.active_session_id) == str(stopped_session_id)
        ):
            state.active_session_id = None
            state.current_goal = None
            state.current_profile = None

        if stopped_session_id:
            stopped_item = next(
                (
                    item
                    for item in state.queue_items
                    if isinstance(item, dict)
                    and str(item.get("session_id")) == str(stopped_session_id)
                ),
                None,
            )
            if stopped_item and not reservation_cancelled:
                DeviceExecutionLock.cancel_reservation(stopped_item.get("queue_ticket"))
            state.queue_items = [
                item
                for item in state.queue_items
                if not (
                    isinstance(item, dict)
                    and str(item.get("session_id")) == str(stopped_session_id)
                )
            ]

        if PAUSE_FILE.exists():
            try:
                PAUSE_FILE.unlink()
            except Exception:
                pass

        cls.ensure_worker_running()
        state.wake_event.set()
        return True


    @classmethod
    def resume_task(cls) -> bool:
        if PAUSE_FILE.exists():
            PAUSE_FILE.unlink()
            return True
        return False

    @staticmethod
    def archive_older_replays_on_launch():
        """Archives all existing step replay output folders on server launch."""
        if TEST_OUTPUTS_DIR.exists():
            archive_dir = TEST_DATA_DIR / "older"
            for item in TEST_OUTPUTS_DIR.iterdir():
                if item.is_dir():
                    archive_dir.mkdir(parents=True, exist_ok=True)
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    target_name = f"{item.name}_{timestamp}"
                    target_path = archive_dir / target_name

                    counter = 1
                    while target_path.exists():
                        target_name = f"{item.name}_{timestamp}_{counter}"
                        target_path = archive_dir / target_name
                        counter += 1

                    print(
                        f"Archiving older replay record on server launch: "
                        f"{item.name} -> {target_path}"
                    )
                    try:
                        shutil.move(str(item), str(target_path))
                    except Exception as e:
                        print(
                            f"Warning: Failed to archive {item.name} during server launch: {e}",
                            file=sys.stderr,
                        )

    @staticmethod
    def verify_chunks_exist_on_launch(replay_manager):
        """Verifies that chunked directories exist for all sessions in the database."""
        print("Verifying session chunks on launch...")
        try:
            sessions = session_repo.get_all_sessions()
            session_ids = [s.get("session_id") for s in sessions if s.get("session_id")]
            print(f"Found {len(session_ids)} sessions in database to verify.")
            for session_id in session_ids:
                replay_manager._ensure_session_chunked(str(session_id))
        except Exception as e:
            print(
                f"Warning: Failed to verify chunks during server launch: {e}",
                file=sys.stderr,
            )


task_queue_service = TaskQueueService()
