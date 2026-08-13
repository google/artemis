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
import signal
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
except ImportError:
    from apps.admin_console.core.config import (
        PAUSE_FILE,
        TEST_DATA_DIR,
        TEST_OUTPUTS_DIR,
        WORKSPACE_ROOT,
    )
    from apps.admin_console.core.state import state
    from apps.admin_console.database.repositories.session_repository import session_repo


class TaskQueueService:
    """Service managing FIFO task execution, background worker, subprocess lifecycle,
    and startup tasks.
    """

    @classmethod
    async def queue_worker(cls):
        while True:
            goal = None
            sess_id = None
            try:
                item = await state.task_queue.get()
                if isinstance(item, dict):
                    goal = item.get("goal")
                    profile = item.get("profile", "flash")
                    sess_id = item.get("session_id")
                else:
                    goal = item
                    profile = "flash"
                    sess_id = None

                state.current_goal = goal
                state.current_profile = profile
                if sess_id:
                    state.active_session_id = sess_id

                # Remove from state.queue_tasks
                state.queue_tasks = [
                    t
                    for t in state.queue_tasks
                    if not (
                        isinstance(t, dict)
                        and (t.get("session_id") == sess_id or t.get("goal") == goal)
                    )
                ]

                test_name = f"web_{int(time.time())}"
                env = os.environ.copy()
                pythonpath_parts = [
                    str(WORKSPACE_ROOT),
                    str(WORKSPACE_ROOT / "apps/admin_console"),
                    str(WORKSPACE_ROOT / "apps/cloud_service"),
                    env.get("PYTHONPATH", ""),
                ]
                if state.ipc_port is not None:
                    env["ARTEMIS_IPC_PORT"] = str(state.ipc_port)
                if sess_id:
                    env["ARTEMIS_SESSION_ID"] = str(sess_id)

                print(f"[QueueWorker] Starting task: {goal} with profile: {profile}")
                state.current_process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "artemis.main",
                    goal,
                    "--profile",
                    profile,
                    "--test-name",
                    test_name,
                    cwd=str(WORKSPACE_ROOT),
                    env=env,
                )
                await state.current_process.wait()
                print(
                    f"[QueueWorker] Task finished with code "
                    f"{state.current_process.returncode}: {goal}"
                )

                # Fallback status update
                if state.active_session_id:
                    curr_status = session_repo.get_session_status(state.active_session_id)
                    if curr_status == "running":
                        if state.was_stopped_manually:
                            new_status = "cancelled"
                        else:
                            new_status = (
                                "completed" if state.current_process.returncode == 0 else "failed"
                            )
                        session_repo.update_session_status(
                            state.active_session_id, new_status, time.time()
                        )
                        print(
                            f"[QueueWorker] Fallback: Updated session "
                            f"{state.active_session_id} status to {new_status}"
                        )
                    state.active_session_id = None

                state.current_profile = None
                state.was_stopped_manually = False

            except asyncio.CancelledError:
                print("[QueueWorker] Background worker task was cancelled.")
                break
            except Exception as e:
                print(f"[QueueWorker] Error executing task: {e}")
            finally:
                if goal is not None:
                    state.task_queue.task_done()

    @classmethod
    async def enqueue_tasks(cls, goals: list[str], profile: str = "flash") -> dict[str, Any]:
        enqueued_tasks = []
        now = time.time()
        for i, goal in enumerate(goals):
            sess_id = str(uuid.uuid4())
            task_item = {
                "session_id": sess_id,
                "goal": goal,
                "profile": profile or "flash",
                "status": "pending",
                "created_at": now + i * 0.001,
                "start_time": now + i * 0.001,
            }
            await state.task_queue.put(task_item)
            state.queue_tasks.append(task_item)
            enqueued_tasks.append(task_item)

        return {
            "status": "queued" if state.is_running else "started",
            "tasks": enqueued_tasks,
            "enqueued_count": len(goals),
            "total_queued": len(state.queue_tasks),
        }

    @classmethod
    def stop_tasks(cls, clear_all: bool = False) -> bool:
        stopped = False
        if clear_all:
            state.clear_queue()

        state.was_stopped_manually = True
        if state.is_running and state.current_process:
            try:
                state.current_process.kill()
                stopped = True
            except Exception:
                pass

        for _, conn_info in list(state.active_connections.items()):
            pid = conn_info.get("pid")
            if pid:
                try:
                    os.kill(pid, signal.SIGINT)
                    stopped = True
                except Exception as e:
                    print(f"Failed to kill external process {pid}: {e}")

        session_repo.mark_all_running_cancelled()

        if PAUSE_FILE.exists():
            try:
                PAUSE_FILE.unlink()
            except Exception:
                pass

        return stopped

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
