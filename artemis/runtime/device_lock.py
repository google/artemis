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

"""Cross-process exclusive ownership for a local mobile device."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from artemis.config.paths import get_temp_dir


class DeviceBusyError(RuntimeError):
    """Raised when another Artemis process already owns a device."""


@dataclass(frozen=True)
class DeviceLockOwner:
    pid: int
    process_created_at: float
    token: str
    device_id: str
    description: str
    acquired_at: str

    @classmethod
    def from_dict(cls, value: dict) -> DeviceLockOwner:
        return cls(
            pid=int(value["pid"]),
            process_created_at=float(value["process_created_at"]),
            token=str(value["token"]),
            device_id=str(value["device_id"]),
            description=str(value.get("description", "unknown task")),
            acquired_at=str(value.get("acquired_at", "unknown time")),
        )


class DeviceExecutionLock:
    """A PID-aware lock shared by UI, CLI, and MCP task processes.

    Atomic file creation provides the inter-process exclusion. PID creation
    time prevents a stale lock from blocking a device after a process exits or
    after the operating system reuses the same PID.
    """

    _MALFORMED_LOCK_GRACE_SECONDS = 5.0

    def __init__(self, device_id: str, description: str = "Artemis task"):
        self.device_id = device_id
        self.description = description
        self.token = uuid.uuid4().hex
        digest = hashlib.sha256(device_id.encode("utf-8", errors="replace")).hexdigest()[:20]
        self.path = get_temp_dir("device-locks") / f"device-{digest}.lock"
        self._acquired = False

    @staticmethod
    def _current_process_created_at() -> float:
        try:
            import psutil

            return float(psutil.Process(os.getpid()).create_time())
        except Exception:
            return 0.0

    @staticmethod
    def _owner_is_alive(owner: DeviceLockOwner) -> bool:
        try:
            import psutil

            process = psutil.Process(owner.pid)
            if not process.is_running():
                return False
            if owner.process_created_at <= 0:
                return True
            return abs(process.create_time() - owner.process_created_at) < 1.0
        except Exception:
            return False

    @staticmethod
    def _read_owner(path: Path) -> DeviceLockOwner | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return DeviceLockOwner.from_dict(payload)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None

    def _remove_stale_lock(self) -> bool:
        owner = self._read_owner(self.path)
        if owner is not None:
            if self._owner_is_alive(owner):
                return False
        else:
            try:
                if time.time() - self.path.stat().st_mtime < self._MALFORMED_LOCK_GRACE_SECONDS:
                    return False
            except OSError:
                return True

        try:
            self.path.unlink(missing_ok=True)
            return True
        except OSError:
            return False

    def acquire(self) -> None:
        owner_payload = {
            "pid": os.getpid(),
            "process_created_at": self._current_process_created_at(),
            "token": self.token,
            "device_id": self.device_id,
            "description": self.description,
            "acquired_at": datetime.now(UTC).isoformat(),
        }

        for _ in range(4):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self._remove_stale_lock():
                    continue
                owner = self._read_owner(self.path)
                owner_text = (
                    f"PID {owner.pid} ({owner.description}, since {owner.acquired_at})"
                    if owner is not None
                    else "another process"
                )
                raise DeviceBusyError(
                    f"Device '{self.device_id}' is already controlled by {owner_text}. "
                    "Wait for that task to finish or stop it before starting another task."
                )
            else:
                try:
                    os.write(fd, json.dumps(owner_payload, ensure_ascii=False).encode("utf-8"))
                finally:
                    os.close(fd)
                self._acquired = True
                return

        raise DeviceBusyError(f"Could not acquire the execution lock for '{self.device_id}'.")

    def release(self) -> None:
        if not self._acquired:
            return
        try:
            owner = self._read_owner(self.path)
            if owner is not None and owner.token == self.token:
                self.path.unlink(missing_ok=True)
        finally:
            self._acquired = False

    @classmethod
    def cleanup_stale_locks(cls) -> int:
        """Remove lock files whose owning process is no longer alive."""
        removed = 0
        lock_dir = get_temp_dir("device-locks")
        for path in lock_dir.glob("device-*.lock"):
            owner = cls._read_owner(path)
            if owner is not None and cls._owner_is_alive(owner):
                continue
            if owner is None:
                try:
                    if time.time() - path.stat().st_mtime < cls._MALFORMED_LOCK_GRACE_SECONDS:
                        continue
                except OSError:
                    continue
            try:
                path.unlink(missing_ok=True)
                removed += 1
            except OSError:
                pass
        return removed
