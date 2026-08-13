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
from collections.abc import Callable
from typing import Any

try:
    from admin_console.core.config import PAUSE_FILE
except ImportError:
    from apps.admin_console.core.config import PAUSE_FILE


class ServerState:
    """Encapsulates all runtime states of the debug server."""

    def __init__(self):
        self.ipc_subscribers: list[Callable[[str, Any], None]] = []
        self.ipc_server: asyncio.Server | None = None
        self.ipc_port: int | None = None

        self.current_process: asyncio.subprocess.Process | None = None
        self.current_goal: str | None = None
        self.current_profile: str | None = None
        self.active_connections: dict[str, dict[str, Any]] = {}
        self.active_session_id: str | None = None
        self.was_stopped_manually: bool = False

        # FIFO Task queue
        self.task_queue: asyncio.Queue = asyncio.Queue()
        self.queue_tasks: list[dict[str, Any]] = []
        self.worker_task: asyncio.Task | None = None

    @property
    def queue_goals(self) -> list[str]:
        """Backward compatibility helper for queue goals."""
        return [t.get("goal", "") for t in self.queue_tasks if isinstance(t, dict)]

    @queue_goals.setter
    def queue_goals(self, val: list[Any]):
        # Allow setting if legacy code updates it
        pass

    @property
    def is_running(self) -> bool:
        return self.current_process is not None and self.current_process.returncode is None

    @property
    def is_paused(self) -> bool:
        return PAUSE_FILE.exists()

    def add_subscriber(self, callback: Callable[[str, Any], None]):
        if callback not in self.ipc_subscribers:
            self.ipc_subscribers.append(callback)

    def remove_subscriber(self, callback: Callable[[str, Any], None]):
        if callback in self.ipc_subscribers:
            self.ipc_subscribers.remove(callback)

    def clear_queue(self):
        while not self.task_queue.empty():
            try:
                self.task_queue.get_nowait()
                self.task_queue.task_done()
            except (asyncio.QueueEmpty, ValueError):
                break
        self.queue_tasks.clear()


# Global shared instance
state = ServerState()
