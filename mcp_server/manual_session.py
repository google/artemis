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

"""In-process registry of host-agent-driven manual device sessions.

A manual session pairs one connected Android device with a lazily created
``McpActionExecutor`` -- the same "hands" ``FlashRunner`` uses internally --
driven turn-by-turn by whichever MCP host is calling ``mobile_observe`` /
``mobile_act`` instead of an Artemis-internal LLM loop.

Idle sessions are reaped opportunistically on the next registry access rather
than by a background thread: this keeps the registry single-threaded and
simple, at the cost of a session only being released the next time *any*
device's session is touched (or explicitly via ``mobile_session_end``, which
is the expected common path for a host agent that knows it is done).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import time
from typing import Any

from artemis.mcp.action_executor import McpActionExecutor
from artemis.mcp.adb_server import _get_controller
from artemis.runtime.device_lock import DeviceBusyError, DeviceExecutionLock

__all__ = ["ManualSession", "ManualSessionRegistry", "DEFAULT_IDLE_TIMEOUT_S"]

#: Default seconds a session may sit unused before the next registry access
#: reaps it and releases its device lock. Overridable per-registry for tests.
DEFAULT_IDLE_TIMEOUT_S = 600.0


@dataclass
class ManualSession:
    """One device's live manual-mode state.

    Doubles as the ``state`` argument ``McpActionExecutor.execute()`` expects:
    it reads/writes exactly ``indexed_elements``, ``indexed_points``, and
    ``latest_screenshot`` (see ``artemis/mcp/action_executor.py``), and reads
    ``latest_ui_hierarchy`` (via ``getattr(..., None)``) for direction-swipe
    smart targeting.
    """

    executor: McpActionExecutor
    lock: DeviceExecutionLock
    last_used: float
    indexed_elements: list[dict] = field(default_factory=list)
    indexed_points: list[list[int]] = field(default_factory=list)
    latest_screenshot: str | None = None
    latest_ui_hierarchy: str | None = None


def _default_executor_factory(device_serial: str | None) -> McpActionExecutor:
    """Builds a real-device executor the same way ``mobile_get_device_state`` does."""
    controller = _get_controller(device_serial=device_serial)
    return McpActionExecutor(controller.ctx, controller, agent_name="manual")


class ManualSessionRegistry:
    """Creates, reuses, and reaps manual sessions, one per device key."""

    def __init__(
        self,
        idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S,
        executor_factory: Callable[[str | None], McpActionExecutor] = _default_executor_factory,
    ):
        self._sessions: dict[str, ManualSession] = {}
        self._idle_timeout_s = idle_timeout_s
        self._executor_factory = executor_factory

    @staticmethod
    def _key(device_serial: str | None) -> str:
        return device_serial or "default"

    def _reap_idle(self) -> None:
        now = time.monotonic()
        stale_keys = [
            key
            for key, session in self._sessions.items()
            if now - session.last_used >= self._idle_timeout_s
        ]
        for key in stale_keys:
            self._end_key(key)

    def _end_key(self, key: str) -> bool:
        session = self._sessions.pop(key, None)
        if session is None:
            return False
        session.lock.release()
        return True

    def get_or_create(self, device_serial: str | None) -> ManualSession:
        """Returns the device's live session, creating and locking it if needed.

        Raises:
            DeviceBusyError: the device is already owned by another Artemis
                task (Flash/Pro run or a different manual session).
        """
        self._reap_idle()
        key = self._key(device_serial)
        session = self._sessions.get(key)
        if session is not None:
            session.last_used = time.monotonic()
            return session

        lock = DeviceExecutionLock(
            device_id=key,
            description="Artemis manual session (mobile_observe/mobile_act)",
            session_id=f"manual-{key}",
            ingress="mcp",
        )
        try:
            lock.acquire(blocking=False)
        except DeviceBusyError as exc:
            raise DeviceBusyError(
                f"Device '{key}' is busy with another Artemis task or manual session: {exc}"
            ) from exc

        executor = self._executor_factory(device_serial)
        session = ManualSession(executor=executor, lock=lock, last_used=time.monotonic())
        self._sessions[key] = session
        return session

    def end(self, device_serial: str | None) -> bool:
        """Releases the device lock and drops the session. Idempotent."""
        ended = self._end_key(self._key(device_serial))
        self._reap_idle()
        return ended
