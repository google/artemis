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
from datetime import datetime
import logging
import os
from pathlib import Path
import shutil
import sys
import threading
import time
from typing import Any
import uuid

try:
    from admin_console.core.state import state
    from admin_console.database.repositories.session_repository import session_repo
    from admin_console.services import worker_process_io
    from admin_console.services.media_service import media_service
except ImportError:
    from apps.admin_console.core.state import state
    from apps.admin_console.database.repositories.session_repository import session_repo
    from apps.admin_console.services import worker_process_io
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
    clear_cancel_request,
    current_adb_endpoint,
    pid_is_alive,
    process_supervisor,
    request_cancel,
    trace_store,
)

logger = logging.getLogger(__name__)


class TaskQueueService:
    """Service managing FIFO task execution, background worker, subprocess lifecycle,
    and startup tasks.
    """

    # Strong references to in-flight _execute_task_item tasks (asyncio itself only
    # keeps weak references to running tasks).
    _run_tasks: set[asyncio.Task] = set()
    # Deadline enforcers for graceful stops (see _stop_worker_gracefully).
    _forced_stop_tasks: set[asyncio.Task] = set()

    DEFAULT_CANCEL_GRACE_SECONDS = 45.0

    @classmethod
    def _cancel_grace_seconds(cls) -> float:
        """How long a worker may finalize itself before it is killed.

        ``ARTEMIS_CANCEL_GRACE_SECONDS=0`` restores the legacy immediate kill.
        """
        raw = os.getenv("ARTEMIS_CANCEL_GRACE_SECONDS")
        if raw is None or not raw.strip():
            return cls.DEFAULT_CANCEL_GRACE_SECONDS
        try:
            return max(0.0, float(raw))
        except ValueError:
            return cls.DEFAULT_CANCEL_GRACE_SECONDS

    @staticmethod
    def _hard_kill(pid: int, process_created_at: float = 0.0) -> bool:
        if not pid:
            return False
        if process_created_at and process_created_at > 0:
            return process_supervisor.terminate_tree_verified(pid, process_created_at)
        try:
            return process_supervisor.terminate_tree(pid)
        except Exception:
            return False

    @classmethod
    def _stop_worker_gracefully(
        cls,
        pid: Any,
        process_created_at: float = 0.0,
        session_id: str | None = None,
        reason: str = "Task stopped from the Artemis frontend.",
    ) -> tuple[bool, bool]:
        """Ask a worker to cancel itself; hard-kill it once the grace period lapses.

        Workers are isolated from the daemon's console (and may belong to
        another ingress process), so instead of a signal the daemon drops a
        cancel marker the worker polls for. Honouring it runs the worker's
        normal cancellation path: the screen recording is stopped and remuxed,
        the trace folder is compiled, and the device lease is released. A
        worker that never picks the marker up is killed after the grace period.

        Returns ``(stopped, deferred)``: ``stopped`` mirrors the legacy kill
        result, ``deferred`` is True when the kill was handed to the deadline
        enforcer instead of happening now.
        """
        try:
            pid_int = int(pid) if pid else 0
        except (TypeError, ValueError):
            pid_int = 0
        grace = cls._cancel_grace_seconds()
        if not pid_int or grace <= 0:
            return cls._hard_kill(pid_int, process_created_at), False

        created_at = float(process_created_at or 0.0)
        if created_at <= 0:
            # PID markers carry the creation time so a leftover marker can never
            # cancel a future process that reuses this PID.
            try:
                import psutil

                created_at = float(psutil.Process(pid_int).create_time())
            except Exception:
                created_at = 0.0
        if not pid_is_alive(pid_int, created_at or None):
            return True, False

        written = request_cancel(
            session_id=str(session_id) if session_id else None,
            pid=pid_int,
            process_created_at=created_at,
            reason=reason,
        )
        if not written:
            return cls._hard_kill(pid_int, created_at), False

        print(
            f"[stop_tasks] Cancel requested for worker {pid_int}"
            f" (session {session_id or 'n/a'}); forcing termination after {grace:.0f}s"
        )
        cls._schedule_forced_stop(pid_int, created_at, grace, session_id)
        return True, True

    @classmethod
    def _stop_proc_gracefully(cls, proc: Any, session_id: Any) -> bool:
        """Graceful variant for a locally spawned process; True when deferred."""
        pid = getattr(proc, "pid", None)
        if not pid or cls._cancel_grace_seconds() <= 0:
            return False
        try:
            _stopped, deferred = cls._stop_worker_gracefully(
                pid, session_id=str(session_id) if session_id else None
            )
        except Exception:
            return False
        return deferred

    @classmethod
    def _schedule_forced_stop(
        cls, pid: int, process_created_at: float, grace: float, session_id: Any
    ) -> None:
        """Kill ``pid`` if it is still alive once ``grace`` seconds have passed."""

        def _still_alive() -> bool:
            return pid_is_alive(pid, process_created_at or None)

        def _force() -> None:
            print(
                f"[stop_tasks] Worker {pid} (session {session_id or 'n/a'}) did not exit"
                f" within {grace:.0f}s of the cancel request; forcing termination."
            )
            cls._hard_kill(pid, process_created_at)

        async def _enforce_async() -> None:
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                if not _still_alive():
                    break
                await asyncio.sleep(0.5)
            else:
                _force()
            clear_cancel_request(session_id=str(session_id) if session_id else None, pid=pid)

        def _enforce_sync() -> None:
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                if not _still_alive():
                    break
                time.sleep(0.5)
            else:
                _force()
            clear_cancel_request(session_id=str(session_id) if session_id else None, pid=pid)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            task = loop.create_task(_enforce_async())
            cls._forced_stop_tasks.add(task)
            task.add_done_callback(cls._forced_stop_tasks.discard)
            return
        threading.Thread(
            target=_enforce_sync, name=f"artemis-forced-stop-{pid}", daemon=True
        ).start()

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
                # One broken subscriber must not block the others, but a
                # silent drop hides it entirely.
                logger.warning(
                    "Event subscriber %r failed for event %s",
                    cb,
                    event_type,
                    exc_info=True,
                )

    @classmethod
    def _broadcast_startup_progress(cls, session_id: str | None, stage: str, message: str) -> None:
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

    # Worker subprocess I/O plumbing lives in worker_process_io; the historical
    # private names stay bound here so callers and tests keep working unchanged.
    _subprocess_creation_kwargs = staticmethod(worker_process_io.subprocess_creation_kwargs)
    _forward_worker_output = staticmethod(worker_process_io.forward_worker_output)
    _finish_output_forwarder = staticmethod(worker_process_io.finish_output_forwarder)
    _wait_for_worker_process = staticmethod(worker_process_io.wait_for_worker_process)

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

    @classmethod
    def ensure_worker_running(cls):
        """Guarantees that the background queue worker task is active and running."""
        try:
            loop = asyncio.get_running_loop()
            if state.worker_task is None or state.worker_task.done():
                state.worker_task = loop.create_task(cls.queue_worker())
                try:
                    state.wake_event.set()
                except AttributeError:
                    # Partially initialized state (unit tests): the worker
                    # loop polls anyway.
                    pass
        except RuntimeError:
            pass

    @classmethod
    def _concurrency_limit(cls) -> int:
        """Return 0 for per-device concurrency, or the global task limit."""
        return DeviceExecutionLock.resolve_env_concurrency()

    @classmethod
    async def queue_worker(cls):
        """Persistent dispatcher scheduling pending tasks onto devices.

        Scans the queue and launches each eligible task as an independent
        coroutine. Admission is governed by _concurrency_limit(): 0 admits one
        task per device so distinct devices execute concurrently, N>=1 admits
        at most N tasks across all devices. Per-device FIFO ordering and
        cross-process mutual exclusion remain enforced by DeviceExecutionLock
        inside each worker process.
        """
        print(
            f"[QueueWorker] Dispatcher initialized (concurrency limit: {cls._concurrency_limit()})."
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
        """Launch every pending task admissible under the current concurrency limit."""
        limit = cls._concurrency_limit()
        state.prune_finished_runs()
        if limit == 1 and state.is_running:
            return

        # Dispatched-but-not-yet-spawned runs are only visible as queue items in
        # "running" state, so admission must count those too -- active_runs alone
        # lags behind by the subprocess startup latency.
        in_flight = [
            i for i in state.queue_items if isinstance(i, dict) and i.get("status") == "running"
        ]
        capacity = None
        if limit >= 1:
            # Registered workers still have running queue rows until finalization.
            active_pids = {
                getattr(run.get("process"), "pid", None) for run in state.active_runs.values()
            } - {None}
            starting = sum(
                str(item.get("session_id")) not in state.active_runs
                and item.get("pid") not in active_pids
                for item in in_flight
            )
            capacity = limit - len(state.active_runs) - starting
            if capacity <= 0:
                return
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
            if limit == 0:
                # A task without a resolved device may bind to any serial, so it
                # only launches on an otherwise idle scheduler; the device lock
                # then allocates freely without contending against active runs.
                if device is None and (state.active_runs or in_flight or dispatched_any):
                    continue
                if device is not None and target.lock_key in busy_devices:
                    continue
            elif limit > 1 and device is not None and target.lock_key in busy_devices:
                # A second worker for this device would wait on its lock.
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
            if capacity is not None:
                capacity -= 1
                if capacity <= 0:
                    break

    @classmethod
    def _begin_task_run(
        cls,
        task_item: dict[str, Any],
        run_key: str,
        sess_id: Any,
        goal: str,
        profile: str,
    ) -> None:
        """Mark the task as running and announce the launch to subscribers."""
        task_item["status"] = "running"
        task_item["start_time"] = time.time()

        # A fresh launch clears a stale stop request left over for this run
        # key from a previous task. Manual stops are tracked per run in
        # manually_stopped_run_ids so stopping one device's task never
        # affects concurrent runs. Per-session stops are tracked in
        # cancelled_session_ids and are unaffected.
        state.manually_stopped_run_ids.discard(run_key)
        state.current_goal = goal
        state.current_profile = profile
        state.active_session_id = sess_id

        cls._broadcast_startup_progress(sess_id, "launching", "Starting the execution process")

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

    @classmethod
    def _build_worker_invocation(
        cls,
        task_item: dict[str, Any],
        run_key: str,
        sess_id: Any,
        goal: str,
        profile: str,
        target: AdbTarget,
    ) -> tuple[list[str], dict[str, str]]:
        """Assemble the worker subprocess command line and environment."""
        expected_output = task_item.get("expected_output")
        enable_outputter = task_item.get("enable_outputter")
        verification_level = task_item.get("verification_level")
        explorer_mode = task_item.get("explorer_mode")
        locked_app = task_item.get("locked_app_package") or task_item.get("locked_app")
        app_path = task_item.get("app_path")

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
        if verification_level:
            cmd.extend(["--verification-level", str(verification_level)])
        if explorer_mode:
            cmd.extend(["--explorer-pro-mode", str(explorer_mode)])
        if locked_app:
            cmd.extend(["--locked-app", str(locked_app)])
        if app_path:
            cmd.extend(["--app-path", str(app_path)])
        device_serial = task_item.get("device_serial")
        if device_serial:
            cmd.extend(["--device-serial", str(device_serial)])
            env["ADB_DEVICE_SERIAL"] = str(device_serial)
        return cmd, env

    @classmethod
    def _register_worker_run(
        cls,
        task_item: dict[str, Any],
        run_key: str,
        sess_id: Any,
        goal: str,
        profile: str,
        target: AdbTarget,
        proc: asyncio.subprocess.Process,
    ) -> None:
        """Record the spawned worker in shared state and hand it the device reservation."""
        device_serial = task_item.get("device_serial")
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
                # Preserve status updates written concurrently by the worker.
                trace_store.update_trace_pid(str(sess_id), proc.pid)
            except OSError as exc:
                # Status probes fall back to DB PID bookkeeping, but note it.
                print(
                    f"[QueueWorker] Could not record worker pid in status.json for {sess_id}: {exc}"
                )
        cls._broadcast_startup_progress(sess_id, "process_ready", "Execution process started")
        ingress_type = str(task_item.get("ingress", "frontend"))
        queue_ticket = task_item.get("queue_ticket")
        if queue_ticket:
            transferred = DeviceExecutionLock.transfer_reservation(
                str(queue_ticket),
                proc.pid,
                description=f"{ingress_type} task: {goal[:120]}",
                device_id=device_serial or "pending",
                session_id=str(sess_id) if sess_id else None,
                ingress=ingress_type,
                lock_scope=target.lock_scope,
            )
            if not transferred:
                print(
                    f"[QueueWorker] Could not transfer queue ticket {queue_ticket} to worker"
                    f" pid {proc.pid}; the worker will queue a fresh ticket itself."
                )

    @classmethod
    def _start_output_forwarder(
        cls, sess_id: Any, proc: asyncio.subprocess.Process
    ) -> asyncio.Task[None] | None:
        """Start forwarding the worker's output; returns the forwarder task, if any."""
        # Forward the worker's combined output and tee it into the trace's
        # stdout.log so the log paths advertised by the MCP API exist.
        log_path = None
        if sess_id:
            try:
                log_path = trace_store.get_trace_stdout_log_path(str(sess_id))
            except Exception:
                log_path = None
        if isinstance(proc.stdout, asyncio.StreamReader):
            return asyncio.create_task(cls._forward_worker_output(proc.stdout, log_path))
        return None

    @classmethod
    async def _terminate_if_cancelled_during_launch(
        cls, run_key: str, sess_id: Any, proc: asyncio.subprocess.Process
    ) -> None:
        """Terminate a worker whose task was cancelled while it was being launched."""
        if sess_id and (
            str(sess_id) in getattr(state, "cancelled_session_ids", set())
            or run_key in state.manually_stopped_run_ids
        ):
            print(f"[QueueWorker] Task [{sess_id}] was cancelled during launch. Terminating.")
            await cls._terminate_worker_process(proc)

    @classmethod
    async def _persist_terminal_session_status(
        cls, sess_id: Any, returncode: int, manual_stop: bool
    ) -> str:
        """Resolve and persist the session's terminal status in the DB and trace store."""
        current_status = session_repo.get_session_status(sess_id)
        new_status, should_persist = cls._resolve_terminal_status(
            current_status,
            returncode,
            manual_stop,
        )
        if should_persist:
            if session_repo.update_session_status(sess_id, new_status, time.time()):
                print(f"[QueueWorker] Updated session {sess_id} status to '{new_status}'")
            else:
                # The DB row is the fallback MCP pollers reconcile
                # against when status.json is stale, so a failed DB
                # write must not pass silently.
                logger.error(
                    "[QueueWorker] Could not persist terminal DB status '%s' for session %s",
                    new_status,
                    sess_id,
                )
        else:
            print(f"[QueueWorker] Preserved authoritative session {sess_id} status '{new_status}'")
        # A stale status.json would leave MCP pollers seeing "running"
        # until their next DB reconcile, so retry transient write
        # failures before giving up.
        for attempt in range(3):
            try:
                if trace_store.read_status(str(sess_id)):
                    canonical_mcp_status = (
                        "completed" if new_status in ("completed", "success") else new_status
                    )
                    trace_store.update_trace_status(
                        str(sess_id),
                        canonical_mcp_status,
                    )
                break
            except OSError as exc:
                if attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                logger.error(
                    "[QueueWorker] Could not persist terminal MCP status "
                    "for %s after %d attempts: %s",
                    sess_id,
                    attempt + 1,
                    exc,
                )
        return new_status

    @classmethod
    async def _recover_or_fail_recording(cls, sess_id: Any) -> None:
        """Finalize a recording whose worker died before it could, else mark it failed.

        A worker that stops gracefully finalizes its own recording and this is
        a no-op. A hard-killed (or crashed) worker leaves the raw scrcpy file it
        registered at recording start; remuxing that file is all that is
        needed to publish the full video.
        """
        rec_info = session_repo.get_video_recording_for_session(sess_id)
        rec_status = (rec_info or {}).get("status")
        if rec_status == "ready":
            return

        recovered_url = None
        # 1. Direct recovery from the recording row written at recording start.
        try:
            local_video_path = (rec_info or {}).get("local_video_path")
            if local_video_path:
                start_time = (rec_info or {}).get("start_time")
                if not start_time:
                    session_row = session_repo.get_session_by_id(sess_id)
                    start_time = dict(session_row).get("start_time") if session_row else None
                final_path = await asyncio.to_thread(
                    media_service.recover_orphaned_recording,
                    local_video_path,
                    start_time,
                )
                if final_path:
                    session_repo.mark_recording_ready(sess_id, str(final_path))
                    recovered_url = media_service.path_to_video_url(Path(final_path))
        except Exception as rec_err:
            print(f"[QueueWorker] Error finalizing orphaned recording: {rec_err}")

        # 2. Fallback: locate an already finalized file by folder / session naming.
        if not recovered_url:
            try:
                video_rec_map = session_repo.get_video_recordings_map()
                video_idx = await asyncio.to_thread(media_service.build_video_index)
                fallback_url = await asyncio.to_thread(
                    media_service.resolve_video_url,
                    {"session_id": sess_id},
                    video_rec_map,
                    video_idx,
                )
                if fallback_url:
                    session_repo.mark_recording_ready(sess_id, fallback_url)
                    recovered_url = fallback_url
            except Exception as rec_err:
                print(f"[QueueWorker] Error attempting recording recovery: {rec_err}")

        if recovered_url:
            cls._broadcast_event(
                "recording_ready",
                {"session_id": sess_id, "video_url": recovered_url},
            )
            return

        recording_error = "Task worker exited before recording finalization completed"
        if session_repo.mark_recording_failed_if_pending(sess_id, recording_error):
            cls._broadcast_event(
                "recording_failed",
                {"session_id": sess_id, "error": recording_error},
            )

    @classmethod
    def _announce_session_end(
        cls,
        task_item: dict[str, Any],
        sess_id: Any,
        goal: str,
        new_status: str,
        manual_stop: bool,
    ) -> None:
        """Broadcast session_ended and dispatch the external completion notification."""
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

    @classmethod
    def _release_run_slot(
        cls, sess_id: Any, run_key: str, proc: asyncio.subprocess.Process | None
    ) -> None:
        """Clean up the finished task and release this run's scheduling slot."""
        if sess_id:
            cls._remove_task(sess_id)
            state.cancelled_session_ids.discard(str(sess_id))
        state.cancelled_session_ids.discard(run_key)
        state.manually_stopped_run_ids.discard(run_key)
        try:
            clear_cancel_request(
                session_id=str(sess_id) if sess_id else None,
                pid=getattr(proc, "pid", None),
            )
        except (OSError, TypeError, ValueError):
            # Marker cleanup is best-effort: an unwritable temp dir or an odd
            # pid value must not block releasing the run slot.
            pass
        state.active_runs.pop(run_key, None)
        if proc is not None and state.current_process is proc:
            state.current_process = None
        if not state.active_runs:
            state.active_session_id = None
            state.current_goal = None
            state.current_profile = None
            # No runs left: any not-yet-consumed manual-stop markers are
            # stale and must not leak into future runs.
            state.manually_stopped_run_ids.clear()
        state.wake_event.set()

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
            cls._begin_task_run(task_item, run_key, sess_id, goal, profile)

            target = cls._task_target(task_item)
            cmd, env = cls._build_worker_invocation(
                task_item, run_key, sess_id, goal, profile, target
            )

            device_serial = task_item.get("device_serial")
            print(
                f"[QueueWorker] Starting task [{sess_id}]: '{goal}' (profile: {profile}, device: {device_serial or 'auto'}, outputter: {bool(task_item.get('expected_output') or task_item.get('enable_outputter'))})"
            )
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(WORKSPACE_ROOT),
                env=env,
                **cls._subprocess_creation_kwargs(),
            )
            cls._register_worker_run(task_item, run_key, sess_id, goal, profile, target, proc)
            output_task = cls._start_output_forwarder(sess_id, proc)
            await cls._terminate_if_cancelled_during_launch(run_key, sess_id, proc)

            # 3. Await subprocess completion
            returncode = await cls._wait_for_worker_process(proc)
            print(f"[QueueWorker] Task [{sess_id}] exited with returncode {returncode}")

            manual_stop = run_key in state.manually_stopped_run_ids or bool(
                sess_id and str(sess_id) in state.cancelled_session_ids
            )

            # 4. Perform fallback database status update and notification
            if sess_id:
                new_status = await cls._persist_terminal_session_status(
                    sess_id, returncode, manual_stop
                )
                await cls._recover_or_fail_recording(sess_id)
                cls._announce_session_end(task_item, sess_id, goal, new_status, manual_stop)

        except asyncio.CancelledError:
            print(f"[QueueWorker] Task [{sess_id}] received cancellation signal.")
            if proc is not None and proc.returncode is None:
                await cls._terminate_worker_process(proc)
            raise
        except Exception:
            logger.exception(f"[QueueWorker] Unexpected error executing task [{sess_id}]")
            # A spawn failure must also leave a terminal session status.
            if sess_id:
                try:
                    new_status = await cls._persist_terminal_session_status(
                        sess_id, returncode=1, manual_stop=False
                    )
                    cls._announce_session_end(task_item, sess_id, goal or "", new_status, False)
                except (OSError, RuntimeError, ValueError):
                    logger.exception(
                        f"[QueueWorker] Could not persist failure status for [{sess_id}]"
                    )
        finally:
            await cls._finish_output_forwarder(output_task)
            # 5. Clean up the finished task and release this run's scheduling slot
            cls._release_run_slot(sess_id, run_key, proc)

    @classmethod
    def _find_duplicate_submission(
        cls,
        goals: list[str],
        session_id: str | None,
        device_serial: str | None,
        endpoint: AdbEndpoint,
        now: float,
    ) -> dict[str, Any] | None:
        """Return the short-circuit response for a duplicate submission, if any."""
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
        return None

    @classmethod
    async def _reject_unavailable_device(cls, device_serial: str | None) -> dict[str, Any] | None:
        """Return the rejection response for an unattached explicit serial, if any."""
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
        return None

    @classmethod
    def _create_queue_item(
        cls,
        goal: str,
        index: int,
        now: float,
        endpoint: AdbEndpoint,
        single_session_id: str | None,
        profile: str,
        expected_output: str | None,
        enable_outputter: bool | None,
        locked_app_package: str | None,
        app_path: str | None,
        device_serial: str | None,
        ingress: str,
        conversation_id: str | None,
        verification_level: str | None = None,
        explorer_mode: str | None = None,
    ) -> dict[str, Any]:
        """Reserve a device slot and build one pending queue item for a goal."""
        sess_id = single_session_id if single_session_id else str(uuid.uuid4())
        # enqueue_tasks resolves the device before creating queue items.
        assigned_serial = device_serial

        queue_ticket = DeviceExecutionLock.reserve(
            description=f"{ingress} task: {goal[:120]}",
            device_id=assigned_serial or "pending",
            session_id=sess_id,
            ingress=ingress,
            lock_scope=endpoint.identity,
        )
        return {
            "session_id": sess_id,
            "goal": goal,
            "profile": profile or "flash",
            "expected_output": expected_output,
            "enable_outputter": enable_outputter,
            "verification_level": verification_level,
            "explorer_mode": explorer_mode,
            "locked_app_package": locked_app_package,
            "app_path": app_path,
            "device_serial": assigned_serial,
            "adb_endpoint": endpoint.to_dict(),
            "ingress": ingress,
            "conversation_id": conversation_id,
            "status": "pending",
            "queue_ticket": queue_ticket,
            "created_at": now + index * 0.001,
            "start_time": now + index * 0.001,
        }

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
        verification_level: str | None = None,
        explorer_mode: str | None = None,
    ) -> dict[str, Any]:
        """Enqueues one or more goals and wakes up the background worker.

        ``verification_level`` and ``explorer_mode`` are Pro-profile tuning knobs
        forwarded to the worker as ``--verification-level`` / ``--explorer-pro-mode``;
        they are normalised here so the queue item and the CLI see one spelling.
        """
        verification_level = (
            str(verification_level).strip().lower() or None if verification_level else None
        )
        explorer_mode = str(explorer_mode).strip().lower() or None if explorer_mode else None
        cls.ensure_worker_running()

        enqueued_tasks = []
        now = time.time()
        endpoint = current_adb_endpoint()

        duplicate_response = cls._find_duplicate_submission(
            goals, session_id, device_serial, endpoint, now
        )
        if duplicate_response is not None:
            return duplicate_response

        rejection_response = await cls._reject_unavailable_device(device_serial)
        if rejection_response is not None:
            return rejection_response

        single_session_id = session_id if (session_id and len(goals) == 1) else None
        if not device_serial:
            # Device enumeration may block on ADB.
            from artemis.runtime import device_pool

            try:
                device_serial = await device_pool.select_device_async()
            except Exception:
                device_serial = None
        for i, goal in enumerate(goals):
            task_item = cls._create_queue_item(
                goal,
                i,
                now,
                endpoint,
                single_session_id,
                profile,
                expected_output,
                enable_outputter,
                locked_app_package,
                app_path,
                device_serial,
                ingress,
                conversation_id,
                verification_level=verification_level,
                explorer_mode=explorer_mode,
            )
            state.queue_items.append(task_item)
            enqueued_tasks.append(task_item)
            cls._broadcast_startup_progress(
                task_item["session_id"], "queued", "Task received and queued"
            )

        # Wake worker immediately
        state.wake_event.set()

        return {
            "status": "queued" if state.is_running else "started",
            "tasks": enqueued_tasks,
            "enqueued_count": len(goals),
            "total_queued": len(state.queue_tasks),
        }

    @staticmethod
    def _clear_pause_file() -> None:
        """Remove a leftover pause marker after a stop request."""
        if PAUSE_FILE.exists():
            try:
                PAUSE_FILE.unlink()
            except OSError:
                # Best-effort cleanup of the pause marker; a leftover file
                # only pauses until the next resume request.
                pass

    @classmethod
    def _terminate_all_device_owners(cls) -> None:
        """Terminate every device lock owner and persist each cancellation."""
        active_owners: dict[str, Any] = {}
        try:
            active_owners = DeviceExecutionLock.get_active_owners()
        except OSError as exc:
            # Unreadable lock dir: fall back to the single-owner probe below.
            print(f"[stop_tasks] Could not enumerate device owners: {exc}")
        fallback = DeviceExecutionLock.get_active_owner()
        if fallback and not active_owners:
            active_owners["default"] = fallback

        for dev_owner in list(active_owners.values()):
            if dev_owner and DeviceExecutionLock.is_active_owner(dev_owner):
                cls._stop_worker_gracefully(
                    dev_owner.pid,
                    dev_owner.process_created_at,
                    session_id=dev_owner.session_id,
                )
                DeviceExecutionLock.cleanup_stale_locks(dev_owner.device_id)
                sid = dev_owner.session_id
                if sid:
                    session_repo.update_session_status(str(sid), "cancelled", time.time())
                    try:
                        is_mcp = bool(dev_owner and dev_owner.ingress == "mcp")
                        if is_mcp or trace_store.read_status(str(sid)):
                            trace_store.update_trace_status(
                                str(sid),
                                "cancelled",
                                error="Task stopped from the Artemis frontend.",
                            )
                    except OSError as exc:
                        print(
                            f"[stop_tasks] Could not persist cancelled MCP status for {sid}: {exc}"
                        )
                    cls._broadcast_event(
                        "session_ended",
                        {
                            "session_id": sid,
                            "status": "cancelled",
                            "was_stopped_manually": True,
                        },
                    )

    @classmethod
    def _kill_all_local_runs(cls) -> None:
        """Mark every locally-managed run manually stopped and kill its process."""
        for run_key, run in list(state.active_runs.items()):
            # Mark each run individually: the global "stopped manually"
            # boolean was shared process-wide and polluted the terminal
            # status of unrelated concurrent runs.
            state.manually_stopped_run_ids.add(str(run_key))
            # Also track it per session so each finalizer resolves its
            # terminal status as cancelled.
            state.cancelled_session_ids.add(str(run_key))
            run_proc = run.get("process")
            if run_proc is not None and run_proc.returncode is None:
                if not cls._stop_proc_gracefully(run_proc, run.get("session_id") or run_key):
                    try:
                        run_proc.kill()
                    except (ProcessLookupError, OSError):
                        # Process already exited between the check and the kill.
                        pass
        # active_runs entries are popped by each run's finalizer once the
        # process exit is observed; clearing them here would free the device
        # slots before the processes are actually gone.
        if state.current_process:
            if not cls._stop_proc_gracefully(state.current_process, state.active_session_id):
                try:
                    state.current_process.kill()
                except (ProcessLookupError, OSError):
                    # Process already exited between the check and the kill.
                    pass
            state.current_process = None

    @classmethod
    def _stop_all_tasks(cls) -> bool:
        """Terminate all active device owners and clear pending queue submissions."""
        # 1. Cancel local queue reservations
        for item in state.queue_items:
            if isinstance(item, dict) and item.get("status") != "running":
                DeviceExecutionLock.cancel_reservation(item.get("queue_ticket"))
        state.clear_queue()

        # 2. Terminate all active owners across all devices
        cls._terminate_all_device_owners()
        cls._kill_all_local_runs()

        state.active_connections.clear()
        state.active_session_id = None
        state.current_goal = None
        state.current_profile = None

        cls._clear_pause_file()

        cls.ensure_worker_running()
        state.wake_event.set()
        return True

    @classmethod
    def _resolve_stop_owner(
        cls,
        active_owners: dict[str, Any],
        target_sid: str | None,
        target_device: str | None,
    ) -> Any:
        """Resolve the device lock owner targeted by this stop request, if any."""
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
        return owner

    @classmethod
    def _resolve_local_run(
        cls, target_sid: str | None, target_device: str | None
    ) -> tuple[str | None, dict[str, Any] | None, Any]:
        """Resolve the locally-managed run for this target.

        Concurrent scheduling keeps one subprocess per run in
        state.active_runs. Returns ``(local_run_key, local_run, local_proc)``.
        """
        local_run = None
        local_run_key: str | None = None
        if target_sid:
            local_run = state.active_runs.get(target_sid)
            if local_run is not None:
                local_run_key = target_sid
        elif target_device:
            local_run_key, local_run = next(
                (
                    (key, run)
                    for key, run in state.active_runs.items()
                    if run.get("device_id") and str(run["device_id"]) == target_device
                ),
                (None, None),
            )
        elif len(state.active_runs) == 1:
            # Untargeted stop (legacy single-device gesture): with exactly one
            # live run the target is unambiguous.
            local_run_key, local_run = next(iter(state.active_runs.items()))
        local_proc = (local_run or {}).get("process")
        if local_proc is None and not target_sid and not target_device:
            # Untargeted legacy fallback only: the last-started process may
            # stand in when no per-run entry resolved. An explicitly targeted
            # stop that matched no run must never grab current_process -- it
            # mirrors the newest run, which can be an unrelated concurrent one.
            local_proc = state.current_process
        if local_run is None and local_proc is not None:
            # Legacy path: current_process without a resolved run entry. Find
            # the run that owns this process so a manual stop can be attributed
            # to it instead of to the whole scheduler.
            local_run_key = next(
                (
                    str(key)
                    for key, run in state.active_runs.items()
                    if run.get("process") is local_proc
                ),
                None,
            )
        return local_run_key, local_run, local_proc

    @classmethod
    def _find_target_queue_item(cls, target_sid: str | None) -> dict[str, Any] | None:
        """Find the running (preferred) or pending queue item for this target."""
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
        return running_item or next(
            (
                item
                for item in state.queue_items
                if isinstance(item, dict)
                and item.get("status") == "pending"
                and (not target_sid or str(item.get("session_id")) == target_sid)
            ),
            None,
        )

    @classmethod
    def _resolve_stopped_session_id(
        cls,
        owner: Any,
        target_sid: str | None,
        local_run_key: str | None,
        is_local_owner: bool,
        local_item: dict[str, Any] | None,
    ) -> Any:
        """Attribute the stop request to a session id, if one can be resolved."""
        # active_session_id mirrors the most recently launched run, so with
        # concurrent runs the resolved local run key (== its session id) must
        # take precedence to avoid attributing the stop to an unrelated run.
        return (
            owner.session_id
            if owner and owner.session_id
            else target_sid
            if target_sid
            else local_run_key
            if local_run_key is not None
            else state.active_session_id
            if is_local_owner
            else local_item.get("session_id")
            if local_item
            else None
        )

    @classmethod
    def _terminate_stop_target(
        cls,
        owner: Any,
        owner_record_exists: bool,
        target_sid: str | None,
        local_run: dict[str, Any] | None,
        local_run_key: str | None,
        local_proc: Any,
        local_pid: Any,
        local_item: dict[str, Any] | None,
        is_local_owner: bool,
    ) -> tuple[bool, bool, bool] | None:
        """Terminate the resolved stop target.

        Returns ``(stopped, is_local_owner, reservation_cancelled)``, or
        ``None`` when the caller must refuse the stop outright.
        """
        stopped = False
        reservation_cancelled = False
        if owner and DeviceExecutionLock.is_active_owner(owner):
            stopped, _deferred = cls._stop_worker_gracefully(
                owner.pid,
                owner.process_created_at,
                session_id=owner.session_id or target_sid,
            )
            DeviceExecutionLock.cleanup_stale_locks(owner.device_id)
        elif owner is None and owner_record_exists and not target_sid:
            # Never fall back to a frontend PID while another process has an
            # owner record that is still being published or cannot be parsed.
            return None
        elif (
            owner is None
            and local_proc is not None
            and (
                local_run is not None
                or not target_sid
                or str(state.active_session_id) == target_sid
            )
        ):
            # The locally-managed worker can be stopped during its short
            # initialization window before Agent acquires the device lease.
            if target_sid:
                state.cancelled_session_ids.add(target_sid)
            elif local_run_key is not None:
                state.manually_stopped_run_ids.add(str(local_run_key))
            deferred = False
            if local_pid:
                try:
                    stopped, deferred = cls._stop_worker_gracefully(
                        local_pid,
                        session_id=target_sid or (local_run or {}).get("session_id"),
                    )
                except Exception:
                    stopped = False
            if not deferred:
                try:
                    local_proc.kill()
                    stopped = True
                except (ProcessLookupError, OSError):
                    # Process already exited; the stop above may have got it.
                    pass
            stopped = True
            is_local_owner = True
        elif (
            owner is None
            and local_item
            and (not target_sid or str(local_item.get("session_id")) == target_sid)
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
                    # get_queued_tasks exposes the reservation as "token".
                    stopped = DeviceExecutionLock.cancel_reservation(q_item.get("token"))
                    break
            # Fallback 2: check session repository for a running session with a live worker PID
            if not stopped:
                row = session_repo.get_session_by_id(target_sid)
                if row and row.get("status") == "running":
                    row_pid = row.get("pid")
                    if row_pid and session_repo.process_is_alive(row_pid):
                        try:
                            cls._stop_worker_gracefully(int(row_pid), session_id=target_sid)
                        except Exception as exc:
                            # Report it: a surviving worker keeps the device busy.
                            print(f"[stop_tasks] Could not terminate worker pid {row_pid}: {exc}")
                    DeviceExecutionLock.cleanup_stale_locks()
                    stopped = True
        return stopped, is_local_owner, reservation_cancelled

    @classmethod
    def _finalize_targeted_stop(
        cls,
        stopped_session_id: Any,
        owner: Any,
        owner_pid: Any,
        is_local_owner: bool,
        local_run_key: str | None,
        local_proc: Any,
    ) -> None:
        """Propagate the stop into shared state, the DB, and event subscribers."""
        if is_local_owner:
            if stopped_session_id:
                # Per-session cancellation: the run's own finalizer resolves the
                # terminal status without affecting other concurrent runs.
                state.cancelled_session_ids.add(str(stopped_session_id))
            elif local_run_key is not None:
                state.manually_stopped_run_ids.add(str(local_run_key))
            if local_proc is not None and state.current_process is local_proc:
                state.current_process = None

        for sid, conn_info in list(state.active_connections.items()):
            if (stopped_session_id and str(sid) == str(stopped_session_id)) or (
                owner_pid and conn_info.get("pid") == owner_pid
            ):
                state.active_connections.pop(sid, None)

        if stopped_session_id:
            session_repo.update_session_status(str(stopped_session_id), "cancelled", time.time())
            try:
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
            not stopped_session_id or str(state.active_session_id) == str(stopped_session_id)
        ):
            state.active_session_id = None
            state.current_goal = None
            state.current_profile = None

    @classmethod
    def _remove_stopped_queue_item(
        cls, stopped_session_id: Any, reservation_cancelled: bool
    ) -> None:
        """Drop the stopped session's queue item and cancel its reservation."""
        if not stopped_session_id:
            return
        stopped_item = next(
            (
                item
                for item in state.queue_items
                if isinstance(item, dict) and str(item.get("session_id")) == str(stopped_session_id)
            ),
            None,
        )
        if stopped_item and not reservation_cancelled:
            DeviceExecutionLock.cancel_reservation(stopped_item.get("queue_ticket"))
        state.queue_items = [
            item
            for item in state.queue_items
            if not (
                isinstance(item, dict) and str(item.get("session_id")) == str(stopped_session_id)
            )
        ]

    @classmethod
    def _stop_targeted_task(cls, target_sid: str | None, target_device: str | None) -> bool:
        """Stop a specific task (or default single-device active task)."""
        active_owners = {}
        try:
            active_owners = DeviceExecutionLock.get_active_owners()
        except OSError as exc:
            # Unreadable lock dir: the local-process fallbacks below still apply.
            print(f"[stop_tasks] Could not enumerate device owners: {exc}")

        owner = cls._resolve_stop_owner(active_owners, target_sid, target_device)

        owner_record_exists = DeviceExecutionLock.has_owner_record(target_device)
        owner_pid = owner.pid if owner else None

        local_run_key, local_run, local_proc = cls._resolve_local_run(target_sid, target_device)
        local_pid = getattr(local_proc, "pid", None)
        is_local_owner = bool(owner_pid and local_pid and owner_pid == local_pid)

        local_item = cls._find_target_queue_item(target_sid)
        stopped_session_id = cls._resolve_stopped_session_id(
            owner, target_sid, local_run_key, is_local_owner, local_item
        )

        outcome = cls._terminate_stop_target(
            owner,
            owner_record_exists,
            target_sid,
            local_run,
            local_run_key,
            local_proc,
            local_pid,
            local_item,
            is_local_owner,
        )
        if outcome is None:
            return False
        stopped, is_local_owner, reservation_cancelled = outcome

        if not stopped and not target_sid:
            return False

        cls._finalize_targeted_stop(
            stopped_session_id,
            owner,
            owner_pid,
            is_local_owner,
            local_run_key,
            local_proc,
        )
        cls._remove_stopped_queue_item(stopped_session_id, reservation_cancelled)

        cls._clear_pause_file()

        cls.ensure_worker_running()
        state.wake_event.set()
        return True

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
        target_sid = str(session_id).strip() if session_id else None
        target_device = str(device_id).strip() if device_id else None

        if clear_all:
            return cls._stop_all_tasks()

        return cls._stop_targeted_task(target_sid, target_device)

    @classmethod
    def resume_task(cls) -> bool:
        if PAUSE_FILE.exists():
            PAUSE_FILE.unlink()
            return True
        return False

    @classmethod
    def recover_orphaned_recordings_on_launch(cls) -> int:
        """Finalize recordings left behind by workers that died with the daemon.

        The per-run finalizer handles workers that exit while the daemon is up;
        this sweep covers everything else (daemon crash, machine reboot, tasks
        cancelled before this recovery path existed). Returns the number of
        recordings published.
        """
        recovered = 0
        try:
            pending = session_repo.get_unfinalized_video_recordings()
        except Exception as exc:
            print(f"[ServerStartup] Could not enumerate unfinalized recordings: {exc}")
            return 0
        for row in pending:
            sess_id = row.get("session_id")
            try:
                final_path = media_service.recover_orphaned_recording(
                    row.get("local_video_path"),
                    row.get("start_time") or row.get("session_start_time"),
                )
            except Exception as exc:
                print(f"[ServerStartup] Recording recovery failed for {sess_id}: {exc}")
                continue
            if not final_path:
                continue
            if session_repo.mark_recording_ready(str(sess_id), str(final_path)):
                recovered += 1
                print(f"[ServerStartup] Recovered recording for session {sess_id}: {final_path}")
        if recovered:
            print(f"[ServerStartup] Recovered {recovered} orphaned recording(s).")
        return recovered

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
