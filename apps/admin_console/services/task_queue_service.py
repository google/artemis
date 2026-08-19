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
    from artemis.runtime import process_supervisor
except ImportError:
    from apps.admin_console.core.config import (
        PAUSE_FILE,
        TEST_DATA_DIR,
        TEST_OUTPUTS_DIR,
        WORKSPACE_ROOT,
    )
    from apps.admin_console.core.state import state
    from apps.admin_console.database.repositories.session_repository import session_repo
    from artemis.runtime import process_supervisor


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
        state.queue_items = [
            t
            for t in state.queue_items
            if not (isinstance(t, dict) and t.get("session_id") == session_id)
        ]

    @staticmethod
    def _subprocess_creation_kwargs() -> dict[str, int]:
        """Keep task workers out of the UI server's Windows console group.

        Without a new process group, a console control event intended for the
        uvicorn server is also delivered to a task that is still importing
        Artemis, causing STATUS_CONTROL_C_EXIT (0xC000013A).
        """
        if sys.platform == "win32":
            return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        return {}

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
                except Exception:
                    pass
        except RuntimeError:
            pass

    @classmethod
    async def queue_worker(cls):
        """Persistent, self-healing background worker loop processing tasks in FIFO order."""
        print("[QueueWorker] Background worker loop initialized and running.")
        while True:
            task_item = None
            sess_id = None
            goal = None
            profile = "flash"
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

                # Broadcast session_started so all connected clients know the task has started
                cls._broadcast_event(
                    "session_started",
                    {
                        "session_id": sess_id,
                        "initial_goal": goal,
                        "profile": profile,
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
                if expected_output:
                    cmd.extend(["--output-description", str(expected_output)])
                if enable_outputter is not None:
                    cmd.append("--enable-outputter" if enable_outputter else "--disable-outputter")
                if locked_app:
                    cmd.extend(["--locked-app", str(locked_app)])
                if app_path:
                    cmd.extend(["--app-path", str(app_path)])

                print(
                    f"[QueueWorker] Starting task [{sess_id}]: '{goal}' (profile: {profile}, outputter: {bool(expected_output or enable_outputter)})"
                )
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(WORKSPACE_ROOT),
                    env=env,
                    **cls._subprocess_creation_kwargs(),
                )
                state.current_process = proc
                task_item["pid"] = proc.pid

                if sess_id and (
                    str(sess_id) in getattr(state, "cancelled_session_ids", set())
                    or state.was_stopped_manually
                ):
                    print(
                        f"[QueueWorker] Task [{sess_id}] was cancelled during launch. Terminating."
                    )
                    await cls._terminate_worker_process(proc)

                # 3. Await subprocess completion
                returncode = await proc.wait()
                print(f"[QueueWorker] Task [{sess_id}] exited with returncode {returncode}")

                # 4. Perform fallback database status update and notification
                if sess_id:
                    if state.was_stopped_manually:
                        new_status = "cancelled"
                    elif returncode == 0:
                        new_status = "completed"
                    else:
                        new_status = "failed"
                    session_repo.update_session_status(sess_id, new_status, time.time())
                    state.active_connections.pop(sess_id, None)
                    print(f"[QueueWorker] Updated session {sess_id} status to '{new_status}'")
                    cls._broadcast_event(
                        "session_ended",
                        {
                            "session_id": sess_id,
                            "status": new_status,
                            "was_stopped_manually": state.was_stopped_manually,
                        },
                    )

            except asyncio.CancelledError:
                print("[QueueWorker] Task received cancellation signal.")
                if state.current_process and state.current_process.returncode is None:
                    await cls._terminate_worker_process(state.current_process)
                break
            except Exception as e:
                print(f"[QueueWorker] Unexpected error executing task: {e}")
                await asyncio.sleep(0.5)
            finally:
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
    ) -> dict[str, Any]:
        """Enqueues one or more goals and wakes up the background worker."""
        cls.ensure_worker_running()

        enqueued_tasks = []
        now = time.time()
        for i, goal in enumerate(goals):
            sess_id = str(uuid.uuid4())
            task_item = {
                "session_id": sess_id,
                "goal": goal,
                "profile": profile or "flash",
                "expected_output": expected_output,
                "enable_outputter": enable_outputter,
                "locked_app_package": locked_app_package,
                "app_path": app_path,
                "status": "pending",
                "created_at": now + i * 0.001,
                "start_time": now + i * 0.001,
            }
            state.queue_items.append(task_item)
            enqueued_tasks.append(task_item)

        # Wake worker immediately
        state.wake_event.set()

        return {
            "status": "queued" if state.is_running else "started",
            "tasks": enqueued_tasks,
            "enqueued_count": len(goals),
            "total_queued": len(state.queue_tasks),
        }

    @classmethod
    def stop_tasks(cls, clear_all: bool = False) -> bool:
        """Stops the currently executing task and optionally clears pending tasks."""
        stopped = False
        stopped_session_id = state.active_session_id

        if not stopped_session_id:
            running_item = next(
                (
                    t
                    for t in state.queue_items
                    if isinstance(t, dict) and t.get("status") == "running"
                ),
                None,
            )
            if running_item:
                stopped_session_id = running_item.get("session_id")
            elif state.queue_items and not clear_all:
                stopped_session_id = state.queue_items[0].get("session_id")

        if stopped_session_id:
            getattr(state, "cancelled_session_ids", set()).add(str(stopped_session_id))
        for t in state.queue_items:
            if isinstance(t, dict) and t.get("status") == "running" and t.get("session_id"):
                getattr(state, "cancelled_session_ids", set()).add(str(t["session_id"]))

        if clear_all:
            state.clear_queue()
            stopped = True

        state.was_stopped_manually = True
        if state.current_process:
            pid = getattr(state.current_process, "pid", None)
            if pid:
                try:
                    process_supervisor.terminate_tree(pid)
                except Exception:
                    pass
            try:
                state.current_process.kill()
            except Exception:
                pass
            stopped = True

        for _, conn_info in list(state.active_connections.items()):
            pid = conn_info.get("pid")
            if pid:
                try:
                    process_supervisor.terminate_tree(pid)
                    stopped = True
                except Exception as e:
                    print(f"Failed to kill external process {pid}: {e}")

        # Always reset process and active states cleanly
        state.current_process = None
        state.active_connections.clear()
        session_repo.mark_all_running_cancelled()

        # Broadcast session_ended event so all SSE streams and subscribers update immediately
        if stopped_session_id:
            cls._broadcast_event(
                "session_ended",
                {
                    "session_id": stopped_session_id,
                    "status": "cancelled",
                    "was_stopped_manually": True,
                },
            )
            stopped = True

        state.active_session_id = None
        state.current_goal = None
        state.current_profile = None

        # Ensure any running or stopped task is removed from queue_items so worker_loop is unblocked
        if clear_all:
            state.queue_items = []
        else:
            state.queue_items = [
                t
                for t in state.queue_items
                if not (
                    isinstance(t, dict)
                    and (
                        t.get("status") == "running"
                        or (stopped_session_id and t.get("session_id") == stopped_session_id)
                    )
                )
            ]

        if PAUSE_FILE.exists():
            try:
                PAUSE_FILE.unlink()
            except Exception:
                pass

        # Wake up worker to process the next pending task or transition cleanly
        cls.ensure_worker_running()
        state.wake_event.set()

        return stopped or True

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
