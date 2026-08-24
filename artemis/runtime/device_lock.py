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

"""Cross-process FIFO execution queue for the local Artemis device."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
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
    session_id: str | None = None
    ingress: str | None = None

    @classmethod
    def from_dict(cls, value: dict) -> DeviceLockOwner:
        return cls(
            pid=int(value["pid"]),
            process_created_at=float(value["process_created_at"]),
            token=str(value["token"]),
            device_id=str(value["device_id"]),
            description=str(value.get("description", "unknown task")),
            acquired_at=str(value.get("acquired_at", "unknown time")),
            session_id=(
                str(value["session_id"])
                if value.get("session_id") is not None
                else None
            ),
            ingress=(str(value["ingress"]) if value.get("ingress") is not None else None),
        )


class DeviceExecutionLock:
    """A PID-aware FIFO lease shared by UI, CLI, SDK, and MCP tasks.

    Artemis intentionally supports one active local device. A single global
    queue therefore orders every local task regardless of which API or process
    submitted it. Submission layers may reserve a queue ticket before spawning
    a worker; the worker receives that token through the environment and claims
    the same position. Tasks without a submission layer reserve at acquire time.

    Atomic file creation provides exclusion. PID creation time prevents stale
    owners or queue tickets from blocking execution after a process exits or
    after the operating system reuses the same PID.
    """

    _MALFORMED_LOCK_GRACE_SECONDS = 5.0
    _DEFAULT_POLL_INTERVAL_SECONDS = 0.1
    QUEUE_TICKET_ENV = "ARTEMIS_DEVICE_QUEUE_TICKET"

    def __init__(
        self,
        device_id: str,
        description: str = "Artemis task",
        queue_ticket: str | None = None,
    ):
        self.device_id = device_id
        self.description = description
        self.token = uuid.uuid4().hex
        lock_dir = get_temp_dir("device-locks")
        self.path = lock_dir / "artemis-global-device.lock"
        self.queue_dir = lock_dir / "artemis-global-device.queue"
        self.queue_ticket = queue_ticket or os.environ.pop(self.QUEUE_TICKET_ENV, None)
        self.session_id = os.getenv("ARTEMIS_SESSION_ID") or os.getenv(
            "ARTEMIS_CLOUD_SESSION_ID"
        )
        self.ingress = os.getenv("ARTEMIS_TASK_INGRESS") or "sdk"
        self._queue_path: Path | None = None
        self._acquired = False

    @classmethod
    def _global_lock_path(cls) -> Path:
        return get_temp_dir("device-locks") / "artemis-global-device.lock"

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

    @classmethod
    def _owner_payload(
        cls,
        token: str,
        device_id: str,
        description: str,
        *,
        session_id: str | None = None,
        ingress: str | None = None,
    ) -> dict:
        payload = {
            "pid": os.getpid(),
            "process_created_at": cls._current_process_created_at(),
            "token": token,
            "device_id": device_id,
            "description": description,
            "acquired_at": datetime.now(UTC).isoformat(),
        }
        if session_id:
            payload["session_id"] = str(session_id)
        if ingress:
            payload["ingress"] = str(ingress)
        return payload

    @classmethod
    def reserve(
        cls,
        description: str = "Artemis task",
        device_id: str = "pending",
        *,
        session_id: str | None = None,
        ingress: str | None = None,
    ) -> str:
        """Reserve a global FIFO position before a worker process is spawned."""
        token = uuid.uuid4().hex
        queue_dir = get_temp_dir("device-locks") / "artemis-global-device.queue"
        queue_dir.mkdir(parents=True, exist_ok=True)
        payload = cls._owner_payload(
            token,
            device_id,
            description,
            session_id=session_id,
            ingress=ingress,
        )
        for _ in range(8):
            path = queue_dir / f"{time.time_ns():020d}-{token}.wait"
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                continue
            else:
                try:
                    os.write(fd, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                finally:
                    os.close(fd)
                return token
        raise DeviceBusyError("Could not reserve a position in the Artemis device queue.")

    @classmethod
    def cancel_reservation(cls, token: str | None) -> bool:
        """Remove a pending queue ticket. Active execution leases are untouched."""
        if not token:
            return False
        queue_dir = get_temp_dir("device-locks") / "artemis-global-device.queue"
        removed = False
        for path in queue_dir.glob(f"*-{token}.wait"):
            try:
                path.unlink(missing_ok=True)
                removed = True
            except OSError:
                pass
        return removed

    @classmethod
    def transfer_reservation(
        cls,
        token: str,
        pid: int,
        *,
        description: str = "Artemis task",
        device_id: str = "pending",
        session_id: str | None = None,
        ingress: str | None = None,
    ) -> bool:
        """Transfer a submission ticket to its worker process.

        This makes a ticket stale as soon as a worker that failed before Agent
        initialization exits, instead of keeping it alive with the submitting
        server process.
        """
        queue_dir = get_temp_dir("device-locks") / "artemis-global-device.queue"
        matches = list(queue_dir.glob(f"*-{token}.wait"))
        path = min(matches, default=None)
        if path is None:
            return False
        try:
            import psutil

            process_created_at = float(psutil.Process(pid).create_time())
        except Exception:
            process_created_at = 0.0
        payload = {
            "pid": pid,
            "process_created_at": process_created_at,
            "token": token,
            "device_id": device_id,
            "description": description,
            "acquired_at": datetime.now(UTC).isoformat(),
        }
        if session_id:
            payload["session_id"] = str(session_id)
        if ingress:
            payload["ingress"] = str(ingress)
        try:
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError:
            return False
        return True

    def _find_reserved_ticket(self) -> Path | None:
        if not self.queue_ticket:
            return None
        matches = list(self.queue_dir.glob(f"*-{self.queue_ticket}.wait"))
        return min(matches, default=None)

    def _claim_or_create_queue_ticket(self) -> Path:
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        if self.queue_ticket:
            path = self._find_reserved_ticket()
            if path is None:
                raise DeviceBusyError(
                    "The Artemis queue reservation was cancelled or expired before execution."
                )
            payload = self._owner_payload(
                self.queue_ticket,
                self.device_id,
                self.description,
                session_id=self.session_id,
                ingress=self.ingress,
            )
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            return path

        self.queue_ticket = self.reserve(self.description, self.device_id)
        path = self._find_reserved_ticket()
        if path is None:
            raise DeviceBusyError("The Artemis device queue ticket could not be created.")
        return path

    def _remove_stale_queue_entries(self) -> int:
        removed = 0
        if not self.queue_dir.exists():
            return removed
        for path in self.queue_dir.glob("*.wait"):
            if self._queue_path is not None and path == self._queue_path:
                continue
            owner = self._read_owner(path)
            if owner is not None and self._owner_is_alive(owner):
                continue
            if owner is None:
                try:
                    if time.time() - path.stat().st_mtime < self._MALFORMED_LOCK_GRACE_SECONDS:
                        continue
                except OSError:
                    continue
            try:
                path.unlink(missing_ok=True)
                removed += 1
            except OSError:
                pass
        return removed

    def _try_acquire_owner_lock(self) -> bool:
        owner_payload = self._owner_payload(
            self.token,
            self.device_id,
            self.description,
            session_id=self.session_id,
            ingress=self.ingress,
        )
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            self._remove_stale_lock()
            return False
        try:
            os.write(fd, json.dumps(owner_payload, ensure_ascii=False).encode("utf-8"))
        finally:
            os.close(fd)
        self._acquired = True
        return True

    def acquire(
        self,
        *,
        blocking: bool = True,
        timeout: float | None = None,
        poll_interval: float = _DEFAULT_POLL_INTERVAL_SECONDS,
        cancel_event=None,
    ) -> None:
        """Wait in FIFO order and acquire exclusive access to the Artemis device."""
        self._queue_path = self._claim_or_create_queue_ticket()
        started_at = time.monotonic()
        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise DeviceBusyError("Waiting for the Artemis device queue was cancelled.")
                self._remove_stale_queue_entries()
                queue = sorted(self.queue_dir.glob("*.wait"))
                if not self._queue_path.exists():
                    raise DeviceBusyError("The Artemis queue reservation was cancelled.")

                if queue and queue[0] == self._queue_path and self._try_acquire_owner_lock():
                    self._queue_path.unlink(missing_ok=True)
                    self._queue_path = None
                    return

                if not blocking:
                    owner = self._read_owner(self.path)
                    owner_text = (
                        f"PID {owner.pid} ({owner.description}, since {owner.acquired_at})"
                        if owner is not None
                        else "an earlier queued task"
                    )
                    raise DeviceBusyError(
                        f"Device '{self.device_id}' is already controlled by {owner_text}."
                    )
                if timeout is not None and time.monotonic() - started_at >= timeout:
                    raise DeviceBusyError(
                        f"Timed out waiting for the Artemis device queue after {timeout:.1f}s."
                    )
                time.sleep(max(0.01, poll_interval))
        except BaseException:
            if self._queue_path is not None:
                self._queue_path.unlink(missing_ok=True)
                self._queue_path = None
            raise

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
    def get_active_owner(cls) -> DeviceLockOwner | None:
        """Return the live process that currently owns the global device lease."""
        path = cls._global_lock_path()
        owner = None
        for attempt in range(3):
            owner = cls._read_owner(path)
            if owner is not None or not path.exists():
                break
            if attempt < 2:
                time.sleep(0.01)
        if owner is None:
            try:
                if path.exists() and time.time() - path.stat().st_mtime >= cls._MALFORMED_LOCK_GRACE_SECONDS:
                    path.unlink(missing_ok=True)
            except OSError:
                pass
            return None
        if cls._owner_is_alive(owner):
            return owner
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return None

    @classmethod
    def has_owner_record(cls) -> bool:
        """Return whether an owner record exists, including a record being written."""
        return cls._global_lock_path().exists()

    @classmethod
    def is_active_owner(cls, expected: DeviceLockOwner) -> bool:
        """Revalidate a previously read owner before performing a destructive action."""
        current = cls._read_owner(cls._global_lock_path())
        return bool(
            current
            and current.token == expected.token
            and current.pid == expected.pid
            and abs(current.process_created_at - expected.process_created_at) < 1.0
            and cls._owner_is_alive(current)
        )

    @classmethod
    def annotate_active_owner(
        cls,
        *,
        session_id: str,
        ingress: str | None = None,
    ) -> bool:
        """Attach session metadata after tracing starts inside the lease owner."""
        path = cls._global_lock_path()
        owner = cls._read_owner(path)
        if owner is None or owner.pid != os.getpid() or not cls._owner_is_alive(owner):
            return False
        payload = {
            "pid": owner.pid,
            "process_created_at": owner.process_created_at,
            "token": owner.token,
            "device_id": owner.device_id,
            "description": owner.description,
            "acquired_at": owner.acquired_at,
            "session_id": str(session_id),
            "ingress": ingress or owner.ingress or "sdk",
        }
        temp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(temp_path, path)
        except OSError:
            return False
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        return True

    @classmethod
    def cleanup_stale_locks(cls) -> int:
        """Remove execution leases and queue tickets whose owners are gone."""
        removed = 0
        lock_dir = get_temp_dir("device-locks")
        for path in lock_dir.glob("*.lock"):
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
        queue_dir = lock_dir / "artemis-global-device.queue"
        for path in queue_dir.glob("*.wait"):
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
