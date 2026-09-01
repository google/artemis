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
import psutil
import shutil
import subprocess
import sys
import time
from typing import Any
import uuid

try:
    from admin_console.core.state import state
    from admin_console.database.repositories.session_repository import session_repo
    from admin_console.services.media_service import media_service
except ImportError:
    from apps.admin_console.core.state import state
    from apps.admin_console.database.repositories.session_repository import session_repo
    from apps.admin_console.services.media_service import media_service

from artemis.config import (
    PAUSE_FILE,
    TEST_DATA_DIR,
    TEST_OUTPUTS_DIR,
    WORKSPACE_ROOT,
)
from artemis.runtime import (
    AdbEndpoint,
    AdbTarget,
    DeviceExecutionLock,
    current_adb_endpoint,
    process_supervisor,
)


class TaskQueueService:
    """Service managing FIFO task execution, background worker, subprocess lifecycle,
    and startup tasks.
    """

    # Strong references to in-flight _execute_task_item tasks (asyncio itself only
    # keeps weak references to running tasks).
    _run_tasks: set[asyncio.Task] = set()

    @staticmethod
    def _task_target(task_item: dict[str, Any]) -> AdbTarget:
        endpoint_data = task_item.get("adb_endpoint")
        endpoint = (
            AdbEndpoint.from_mapping(endpoint_data)
            if isinstance(endpoint_data, dict)
            else current_adb_endpoint()
        )
        serial = task_item.get("device_serial")
        return AdbTarget(endpoint=endpoint, serial=str(serial) if serial else None)

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
        shared console boundary.

        Output is captured on every platform so it can be forwarded to the
        server terminal and teed into the trace's stdout.log (the daemon itself
        is often spawned with its stdio discarded, so inheriting would lose the
        worker's logs entirely).
        """
        kwargs: dict[str, Any] = {
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.STDOUT,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
        return kwargs

    @staticmethod
    async def _forward_worker_output(
        stream: asyncio.StreamReader | None, log_path: str | None = None
    ) -> None:
        """Forward a worker's combined output without corrupting UTF-8.

        When ``log_path`` is given, the output is also teed into that file so
        the trace's advertised stdout.log actually exists for diagnostics.
        """
        if stream is None:
            return

        log_file = None
        if log_path:
            try:
                os.makedirs(os.path.dirname(log_path), exist_ok=True)
                log_file = open(log_path, "w", buffering=1, encoding="utf-8", errors="replace")
            except Exception as exc:
                print(f"[QueueWorker] Could not open worker log file '{log_path}': {exc}")

        def _emit(text: str) -> None:
            sys.stdout.write(text)
            sys.stdout.flush()
            if log_file is not None:
                try:
                    log_file.write(text)
                except Exception:
                    pass

        try:
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                text = decoder.decode(chunk)
                if text:
                    _emit(text)

            tail = decoder.decode(b"", final=True)
            if tail:
                _emit(tail)
        finally:
            if log_file is not None:
                try:
                    log_file.close()
                except Exception:
                    pass

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
    def _concurrency_mode(cls) -> str:
        """Resolve the scheduler concurrency mode.

        Mirrors DeviceExecutionLock's contract (ARTEMIS_CONCURRENCY_MODE /
        ARTEMIS_MAX_CONCURRENT_TASKS):
        - "global": strict serial -- one task at a time across all devices.
        - "per_device": one task per device -- distinct devices run concurrently.
        Defaults to per-device concurrency, matching the lock layer's default.
        """
        mode = os.environ.get("ARTEMIS_CONCURRENCY_MODE", "").strip().lower()
        if mode in ("global", "serial", "1"):
            return "global"
        if mode in ("per_device", "device", "parallel", "0"):
            return "per_device"
        try:
            if int(os.environ.get("ARTEMIS_MAX_CONCURRENT_TASKS", 0) or 0) == 1:
                return "global"
        except (TypeError, ValueError):
            pass
        return "per_device"

    @classmethod
    async def queue_worker(cls):
        """Persistent dispatcher scheduling pending tasks onto devices.

        Scans the queue and launches each eligible task as an independent
        coroutine. Admission is governed by _concurrency_mode(): "global" admits
        one task at a time overall, "per_device" admits one task per device so
        distinct devices execute concurrently. Per-device FIFO ordering and
        cross-process mutual exclusion remain enforced by DeviceExecutionLock
        inside each worker process.
        """
        print(
            "[QueueWorker] Dispatcher initialized "
            f"(concurrency mode: {cls._concurrency_mode()})."
        )
        try:
            DeviceExecutionLock.cleanup_stale_locks()
        except Exception as exc:
            print(f"[QueueWorker] Initial stale lock cleanup notice: {exc}")

        try:
            while True:
                try:
                    cls._dispatch_pending_tasks()
                except Exception as exc:
                    print(f"[QueueWorker] Dispatch error: {exc}")
                try:
                    await asyncio.wait_for(state.wake_event.wait(), timeout=0.3)
                    state.wake_event.clear()
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            print("[QueueWorker] Dispatcher received cancellation signal.")
            for run in list(state.active_runs.values()):
                run_proc = run.get("process")
                if run_proc is not None and run_proc.returncode is None:
                    await cls._terminate_worker_process(run_proc)
            raise

    @classmethod
    def _dispatch_pending_tasks(cls) -> None:
        """Launch every pending task admissible under the current concurrency mode."""
        mode = cls._concurrency_mode()
        state.prune_finished_runs()
        if mode == "global" and state.is_running:
            return

        # Dispatched-but-not-yet-spawned runs are only visible as queue items in
        # "running" state, so admission must count those too -- active_runs alone
        # lags behind by the subprocess startup latency.
        in_flight = [
            i
            for i in state.queue_items
            if isinstance(i, dict) and i.get("status") == "running"
        ]
        busy_devices = state.busy_device_ids | {
            cls._task_target(i).lock_key for i in in_flight if i.get("device_serial")
        }
        dispatched_any = False
        loop = asyncio.get_running_loop()
        for item in list(state.queue_items):
            if not (isinstance(item, dict) and item.get("status") == "pending"):
                continue
            sess_id = item.get("session_id")
            if sess_id and str(sess_id) in state.cancelled_session_ids:
                cls._remove_task(sess_id)
                continue

            device = item.get("device_serial")
            target = cls._task_target(item)
            if mode == "per_device":
                # A task without a resolved device may bind to any serial, so it
                # only launches on an otherwise idle scheduler; the device lock
                # then allocates freely without contending against active runs.
                if device is None and (state.active_runs or in_flight or dispatched_any):
                    continue
                if device is not None and target.lock_key in busy_devices:
                    continue

            item["status"] = "running"
            dispatched_any = True
            if device is not None:
                busy_devices.add(target.lock_key)
            run_task = loop.create_task(cls._execute_task_item(item))
            # Hold a strong reference: asyncio keeps only weak refs to running
            # tasks, and a collected run would strand its queue item forever.
            cls._run_tasks.add(run_task)
            run_task.add_done_callback(cls._run_tasks.discard)
            if mode == "global":
                break

    @classmethod
    async def _execute_task_item(cls, task_item: dict[str, Any]) -> None:
        """Run one queued task to completion in its own worker subprocess."""
        sess_id = task_item.get("session_id")
        run_key = str(sess_id) if sess_id else uuid.uuid4().hex
        goal = task_item.get("goal")
        profile = task_item.get("profile", "flash")
        proc: asyncio.subprocess.Process | None = None
        output_task: asyncio.Task[None] | None = None
        try:
            if not isinstance(goal, str) or not goal.strip():
                raise ValueError("Queued task must contain a non-empty string goal.")
            expected_output = task_item.get("expected_output")
            enable_outputter = task_item.get("enable_outputter")
            locked_app = task_item.get("locked_app_package") or task_item.get("locked_app")
            app_path = task_item.get("app_path")
            task_item["status"] = "running"
            task_item["start_time"] = time.time()

            # A fresh launch clears a stale stop request from a previous task,
            # matching the historical serial worker's per-task reset. Per-session
            # stops are tracked in cancelled_session_ids and are unaffected.
            state.was_stopped_manually = False
            state.current_goal = goal
            state.current_profile = profile
            state.active_session_id = sess_id

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

            test_name = f"web_{int(time.time())}_{run_key[:8]}"
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
            env["ARTEMIS_TASK_WORKER"] = "1"
            target = cls._task_target(task_item)
            target.endpoint.apply_to_environment(env)
            env[DeviceExecutionLock.LOCK_SCOPE_ENV] = target.lock_scope
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
            state.active_runs[run_key] = {
                "process": proc,
                "device_id": str(device_serial) if device_serial else None,
                "lock_key": target.lock_key if device_serial else None,
                "adb_endpoint": target.endpoint.to_dict(),
                "goal": goal,
                "profile": profile,
            }
            if sess_id:
                try:
                    from mcp_server.utils import trace_store

                    st = trace_store.read_status(str(sess_id))
                    if st:
                        st["pid"] = proc.pid
                        trace_store.write_status(str(sess_id), st)
                except Exception:
                    pass
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
                lock_scope=target.lock_scope,
            )
            # Forward the worker's combined output and tee it into the trace's
            # stdout.log so the log paths advertised by the MCP API exist.
            log_path = None
            if sess_id:
                try:
                    from mcp_server.utils import trace_store

                    log_path = trace_store.get_trace_stdout_log_path(str(sess_id))
                except Exception:
                    log_path = None
            if isinstance(proc.stdout, asyncio.StreamReader):
                output_task = asyncio.create_task(
                    cls._forward_worker_output(proc.stdout, log_path)
                )

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

            manual_stop = state.was_stopped_manually or bool(
                sess_id and str(sess_id) in state.cancelled_session_ids
            )

            # 4. Perform fallback database status update and notification
            if sess_id:
                current_status = session_repo.get_session_status(sess_id)
                new_status, should_persist = cls._resolve_terminal_status(
                    current_status,
                    returncode,
                    manual_stop,
                )
                if should_persist:
                    session_repo.update_session_status(sess_id, new_status, time.time())
                    print(f"[QueueWorker] Updated session {sess_id} status to '{new_status}'")
                else:
                    print(
                        f"[QueueWorker] Preserved authoritative session {sess_id} "
                        f"status '{new_status}'"
                    )
                try:
                    from mcp_server.utils import trace_store

                    if trace_store.read_status(str(sess_id)):
                        canonical_mcp_status = "completed" if new_status in ("completed", "success") else new_status
                        trace_store.update_trace_status(
                            str(sess_id),
                            canonical_mcp_status,
                        )
                except Exception:
                    pass
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
                        "was_stopped_manually": manual_stop,
                    },
                )

                conversation_id = task_item.get("conversation_id") if task_item else None
                if conversation_id or task_item.get("ingress") == "mcp":
                    try:
                        from mcp_server.notifiers import notify

                        notify(
                            conversation_id=conversation_id or "",
                            message=f"Artemis autonomous task '{goal}' finished with status '{new_status}'.\nTrace ID: {sess_id}",
                            title=f"Task {new_status.capitalize()}: {goal[:40]}",
                            event_type=new_status,
                            payload={
                                "trace_id": sess_id,
                                "session_id": sess_id,
                                "status": new_status,
                                "goal": goal,
                            },
                        )
                    except Exception as notif_err:
                        print(f"[QueueWorker] Notification dispatch notice: {notif_err}")

        except asyncio.CancelledError:
            print(f"[QueueWorker] Task [{sess_id}] received cancellation signal.")
            if proc is not None and proc.returncode is None:
                await cls._terminate_worker_process(proc)
            raise
        except Exception as e:
            print(f"[QueueWorker] Unexpected error executing task [{sess_id}]: {e}")
        finally:
            await cls._finish_output_forwarder(output_task)
            # 5. Clean up the finished task and release this run's scheduling slot
            if sess_id:
                cls._remove_task(sess_id)
                state.cancelled_session_ids.discard(str(sess_id))
            state.cancelled_session_ids.discard(run_key)
            state.active_runs.pop(run_key, None)
            if proc is not None and state.current_process is proc:
                state.current_process = None
            if not state.active_runs:
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
        endpoint = current_adb_endpoint()

        # 1. Deduplication by session_id: if session_id is already running or queued, do not re-enqueue
        if session_id and len(goals) == 1:
            target_sid = str(session_id)
            if state.active_session_id and str(state.active_session_id) == target_sid:
                return {
                    "status": "started",
                    "tasks": [{"session_id": target_sid, "status": "running"}],
                    "enqueued_count": 0,
                    "total_queued": len(state.queue_tasks),
                }
            existing_item = next(
                (
                    item
                    for item in state.queue_items
                    if isinstance(item, dict) and str(item.get("session_id")) == target_sid
                ),
                None,
            )
            if existing_item:
                return {
                    "status": existing_item.get("status", "queued"),
                    "tasks": [existing_item],
                    "enqueued_count": 0,
                    "total_queued": len(state.queue_tasks),
                }

        # 2. Debounce duplicate rapid submissions (e.g. UI double-click or network retry within 1s)
        if len(goals) == 1:
            first_goal = goals[0]
            recent_duplicate = next(
                (
                    item
                    for item in reversed(state.queue_items)
                    if isinstance(item, dict)
                    and item.get("status") == "pending"
                    and item.get("goal") == first_goal
                    and (not device_serial or item.get("device_serial") == device_serial)
                    and item.get("adb_endpoint", {}).get("identity") == endpoint.identity
                    and (now - float(item.get("created_at", 0))) < 1.0
                ),
                None,
            )
            if recent_duplicate:
                return {
                    "status": "queued",
                    "tasks": [recent_duplicate],
                    "enqueued_count": 0,
                    "total_queued": len(state.queue_tasks),
                }

        # Strict device binding: reject an explicitly requested serial that is not
        # attached and authorized, instead of silently running on another device.
        # The shared validator fails open on an indeterminate/empty enumeration so
        # the task can proceed and fail downstream with a clear no-device error.
        if device_serial:
            try:
                from artemis.runtime import device_pool

                rejection = await device_pool.validate_explicit_serial_async(device_serial)
            except Exception:
                rejection = None
            if rejection:
                return {
                    "status": "rejected",
                    "error": rejection,
                    "tasks": [],
                    "enqueued_count": 0,
                    "total_queued": len(state.queue_tasks),
                }

        for i, goal in enumerate(goals):
            sess_id = (session_id if (session_id and len(goals) == 1) else str(uuid.uuid4()))
            assigned_serial = device_serial
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
                lock_scope=endpoint.identity,
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
                "adb_endpoint": endpoint.to_dict(),
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
                        try:
                            from mcp_server.utils import trace_store

                            is_mcp = bool(dev_owner and dev_owner.ingress == "mcp")
                            if is_mcp or trace_store.read_status(str(sid)):
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

            if state.active_runs or state.current_process:
                state.was_stopped_manually = True
            for run_key, run in list(state.active_runs.items()):
                # Track the stop per run so each finalizer resolves its terminal
                # status as cancelled even if the global flag is reset meanwhile.
                state.cancelled_session_ids.add(str(run_key))
                run_proc = run.get("process")
                if run_proc is not None and run_proc.returncode is None:
                    try:
                        run_proc.kill()
                    except Exception:
                        pass
            # active_runs entries are popped by each run's finalizer once the
            # process exit is observed; clearing them here would free the device
            # slots before the processes are actually gone.
            if state.current_process:
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
            # Covers scoped/legacy lock records that get_active_owners cannot
            # key by session or device. A stale owner for a different session
            # is never adopted: with no live owner, a session-targeted stop
            # falls through to the queue/session-repository fallbacks below.
            fallback_owner = DeviceExecutionLock.get_active_owner(target_device)
            if fallback_owner and (
                not target_sid
                or (fallback_owner.session_id and str(fallback_owner.session_id) == target_sid)
            ):
                owner = fallback_owner

        owner_record_exists = DeviceExecutionLock.has_owner_record(target_device)
        owner_pid = owner.pid if owner else None

        # Resolve the locally-managed run for this target (concurrent scheduling
        # keeps one subprocess per run in state.active_runs).
        local_run = None
        if target_sid:
            local_run = state.active_runs.get(target_sid)
        elif target_device:
            local_run = next(
                (
                    run
                    for run in state.active_runs.values()
                    if run.get("device_id") and str(run["device_id"]) == target_device
                ),
                None,
            )
        elif len(state.active_runs) == 1:
            # Untargeted stop (legacy single-device gesture): with exactly one
            # live run the target is unambiguous. With several runs no run is
            # resolved -- callers must target a session or device explicitly.
            local_run = next(iter(state.active_runs.values()))
        local_proc = (local_run or {}).get("process")
        if local_proc is None and not state.active_runs:
            # Legacy fallback for the window without per-run bookkeeping. With
            # live active_runs, current_process belongs to the newest run and
            # must not stand in for an unresolved target.
            local_proc = state.current_process
        local_pid = getattr(local_proc, "pid", None)
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
        elif owner is None and local_proc is not None and (
            local_run is not None
            or not target_sid
            or str(state.active_session_id) == target_sid
        ):
            # The locally-managed worker can be stopped during its short
            # initialization window before Agent acquires the device lease.
            if target_sid:
                state.cancelled_session_ids.add(target_sid)
            else:
                state.was_stopped_manually = True
            if local_pid:
                try:
                    stopped = process_supervisor.terminate_tree(local_pid)
                except Exception:
                    stopped = False
            try:
                local_proc.kill()
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
            if stopped_session_id:
                # Per-session cancellation: the run's own finalizer resolves the
                # terminal status without affecting other concurrent runs.
                state.cancelled_session_ids.add(str(stopped_session_id))
            else:
                state.was_stopped_manually = True
            if local_proc is not None and state.current_process is local_proc:
                state.current_process = None

        for sid, conn_info in list(state.active_connections.items()):
            if (stopped_session_id and str(sid) == str(stopped_session_id)) or (
                owner_pid and conn_info.get("pid") == owner_pid
            ):
                state.active_connections.pop(sid, None)

        if stopped_session_id:
            session_repo.update_session_status(
                str(stopped_session_id), "cancelled", time.time()
            )
            try:
                from mcp_server.utils import trace_store

                is_mcp = bool(owner and owner.ingress == "mcp")
                if is_mcp or trace_store.read_status(str(stopped_session_id)):
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
