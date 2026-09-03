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

"""Cross-process FIFO execution queue and per-device mutex for Artemis devices."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import time
from typing import Any
import uuid

from artemis.config.paths import get_temp_dir
from artemis.runtime.process_probe import pid_is_alive


class DeviceBusyError(RuntimeError):
    """Raised when another Artemis process already owns a device."""


class ConcurrencyMode:
    """Supported concurrency strategies for Artemis task execution."""

    GLOBAL = "global"  # Strict serial mode: 1 task globally across all devices
    PER_DEVICE = "per_device"  # 1 task per device: concurrent across different devices


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
    lock_scope: str | None = None

    @classmethod
    def from_dict(cls, value: dict) -> DeviceLockOwner:
        return cls(
            pid=int(value["pid"]),
            process_created_at=float(value["process_created_at"]),
            token=str(value["token"]),
            device_id=str(value["device_id"]),
            description=str(value.get("description", "unknown task")),
            acquired_at=str(value.get("acquired_at", "unknown time")),
            session_id=(str(value["session_id"]) if value.get("session_id") is not None else None),
            ingress=(str(value["ingress"]) if value.get("ingress") is not None else None),
            lock_scope=(str(value["lock_scope"]) if value.get("lock_scope") is not None else None),
        )


class DeviceExecutionLock:
    """A PID-aware FIFO lease supporting per-device mutexes and global concurrency limits.

    Artemis enforces a single active task per mobile device at any time.
    Different devices can execute tasks concurrently unless a global concurrency
    limit (e.g. concurrency_mode="global" or ARTEMIS_MAX_CONCURRENT_TASKS=1) is configured.

    Atomic file creation provides exclusion per device serial. PID creation time
    prevents stale owners or queue tickets from blocking execution after a process
    exits or after the operating system reuses the same PID.
    """

    _MALFORMED_LOCK_GRACE_SECONDS = 5.0
    _DEFAULT_POLL_INTERVAL_SECONDS = 0.1
    QUEUE_TICKET_ENV = "ARTEMIS_DEVICE_QUEUE_TICKET"
    LOCK_SCOPE_ENV = "ARTEMIS_ADB_ENDPOINT_ID"

    @staticmethod
    def _normalize_device_id(device_id: str | None) -> str:
        """Normalize device serial/identifier to a filesystem-safe string."""
        if not device_id or str(device_id).strip() in ("", "default-device", "default"):
            return "default"
        raw = str(device_id).strip()
        if raw.lower() in ("pending", "any"):
            return raw.lower()
        clean = re.sub(r"[^a-zA-Z0-9_-]", "_", raw)
        return clean or "default"

    @classmethod
    def _normalize_lock_id(
        cls,
        device_id: str | None,
        lock_scope: str | None = None,
    ) -> str:
        clean_device = cls._normalize_device_id(device_id)
        clean_scope = cls._normalize_device_id(lock_scope) if lock_scope else None
        return f"{clean_scope}__{clean_device}" if clean_scope else clean_device

    def __init__(
        self,
        device_id: str = "default",
        description: str = "Artemis task",
        queue_ticket: str | None = None,
        max_concurrency: int | None = None,
        concurrency_mode: str | None = None,
        session_id: str | None = None,
        ingress: str | None = None,
        lock_scope: str | None = None,
    ):
        self.device_id = device_id or "default"
        self.lock_scope = lock_scope or os.getenv(self.LOCK_SCOPE_ENV) or None
        self.clean_device_id = self._normalize_lock_id(self.device_id, self.lock_scope)
        self.description = description
        self.max_concurrency = max_concurrency
        self.concurrency_mode = (
            str(concurrency_mode).strip().lower() if concurrency_mode is not None else None
        )
        self.token = uuid.uuid4().hex

        lock_dir = get_temp_dir("device-locks")
        # Per-device lock file
        self.path = lock_dir / f"artemis-device-{self.clean_device_id}.lock"
        # Shared FIFO wait queue directory across all processes
        self.queue_dir = lock_dir / "artemis-global-device.queue"
        self._initial_queue_ticket = queue_ticket or os.environ.pop(self.QUEUE_TICKET_ENV, None)
        self.queue_ticket = self._initial_queue_ticket
        self.session_id = (
            session_id or os.getenv("ARTEMIS_SESSION_ID") or os.getenv("ARTEMIS_CLOUD_SESSION_ID")
        )
        self.ingress = ingress or os.getenv("ARTEMIS_TASK_INGRESS") or "sdk"
        self._queue_path: Path | None = None
        self._acquired = False

    @classmethod
    def _device_lock_path(
        cls,
        device_id: str | None,
        lock_scope: str | None = None,
    ) -> Path:
        clean_id = cls._normalize_lock_id(
            device_id,
            lock_scope or os.getenv(cls.LOCK_SCOPE_ENV) or None,
        )
        return get_temp_dir("device-locks") / f"artemis-device-{clean_id}.lock"

    @classmethod
    def _global_lock_path(cls) -> Path:
        # Kept for backward compatibility and fallback detection
        return get_temp_dir("device-locks") / "artemis-global-device.lock"

    @staticmethod
    def _current_process_created_at() -> float:
        try:
            import psutil

            return float(psutil.Process(os.getpid()).create_time())
        except Exception:
            return 0.0

    @staticmethod
    def _safe_unlink(path: Path, max_retries: int = 3, delay: float = 0.02) -> bool:
        """Safely unlink a file with retries on Windows PermissionError / sharing violations."""
        for i in range(max_retries):
            try:
                path.unlink(missing_ok=True)
                return True
            except OSError:
                if i < max_retries - 1:
                    time.sleep(delay)
        return False

    @staticmethod
    def _safe_replace(src: Path, dst: Path, max_retries: int = 3, delay: float = 0.02) -> bool:
        """Safely replace a file with retries on Windows PermissionError / sharing violations."""
        for i in range(max_retries):
            try:
                os.replace(src, dst)
                return True
            except OSError:
                if i < max_retries - 1:
                    time.sleep(delay)
        return False

    @staticmethod
    def _owner_is_alive(owner: DeviceLockOwner) -> bool:
        if owner.pid <= 0:
            return False
        if owner.process_created_at <= 0:
            # Legacy owner payloads without a create time cannot use PID-reuse
            # protection; require an Artemis-looking process before trusting
            # the record (a recycled PID must not keep a lock alive forever).
            try:
                import psutil

                process = psutil.Process(owner.pid)
                if not process.is_running():
                    return False
                name = process.name().lower()
                cmdline = " ".join(process.cmdline()).lower()
                return any(k in name or k in cmdline for k in ("python", "artemis", "pytest"))
            except Exception:
                return False
        return pid_is_alive(owner.pid, owner.process_created_at)

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

        return self._safe_unlink(self.path)

    @classmethod
    def _owner_payload(
        cls,
        token: str,
        device_id: str,
        description: str,
        *,
        session_id: str | None = None,
        ingress: str | None = None,
        lock_scope: str | None = None,
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
        if lock_scope:
            payload["lock_scope"] = str(lock_scope)
        return payload

    @classmethod
    def _ticket_targets_device(
        cls,
        ticket_owner: DeviceLockOwner,
        *,
        target_lock_id: str,
        target_scope: str | None,
        include_pending: bool,
    ) -> bool:
        """Return whether a queue ticket belongs to the given device's queue.

        A concrete ticket matches only its own device. A ticket whose device id
        is still ``pending``/``any`` is a submission placeholder that no worker
        has claimed yet; with ``include_pending=True`` it matches any device
        with a compatible lock scope (used by listings, and by
        ``_build_device_queue`` which then narrows the match down to a single
        parked device), while ``include_pending=False`` excludes it.
        """
        ticket_dev = cls._normalize_device_id(ticket_owner.device_id)
        ticket_lock_id = cls._normalize_lock_id(
            ticket_owner.device_id,
            ticket_owner.lock_scope,
        )
        if ticket_lock_id == target_lock_id:
            return True
        if not include_pending:
            return False
        same_scope = (
            not ticket_owner.lock_scope
            or not target_scope
            or ticket_owner.lock_scope == target_scope
        )
        return ticket_dev in ("pending", "any") and same_scope

    @classmethod
    def _build_device_queue(
        cls,
        wait_files: list[Path],
        *,
        target_lock_id: str,
        target_scope: str | None,
    ) -> list[Path]:
        """Return the FIFO wait-ticket queue that gates ``target_lock_id``.

        Concrete tickets belong to exactly their own device's queue. An
        unclaimed ``pending``/``any`` ticket is *parked* on at most one device:
        pending tickets, taken in FIFO order, occupy the idle scope-compatible
        candidate devices in sorted lock-id order. Historically such a ticket
        was counted into every device's queue, so a single unclaimed
        head-of-queue ticket blocked all devices at once. Parking preserves
        submission order on the one device the ticket is presumed to claim
        while leaving every other idle device schedulable.

        The pending submission itself cannot starve: its ticket file keeps its
        original timestamp, so once a worker claims a concrete device the
        ticket sorts ahead of every younger ticket for that device.
        """
        entries: list[tuple[Path, str | None]] = []
        known_lock_ids: set[str] = set()
        for wait_path in wait_files:
            owner = cls._read_owner(wait_path)
            if owner is None:
                continue
            if cls._normalize_device_id(owner.device_id) in ("pending", "any"):
                if cls._ticket_targets_device(
                    owner,
                    target_lock_id=target_lock_id,
                    target_scope=target_scope,
                    include_pending=True,
                ):
                    entries.append((wait_path, None))
                continue
            same_scope = (
                not owner.lock_scope or not target_scope or owner.lock_scope == target_scope
            )
            if not same_scope:
                continue
            lock_id = cls._normalize_lock_id(owner.device_id, owner.lock_scope)
            known_lock_ids.add(lock_id)
            entries.append((wait_path, lock_id))

        lock_dir = get_temp_dir("device-locks")
        idle_candidates = sorted(
            lock_id
            for lock_id in known_lock_ids
            if not (lock_dir / f"artemis-device-{lock_id}.lock").exists()
        )
        parked: dict[Path, str] = {}
        next_candidate = 0
        for wait_path, lock_id in entries:
            if lock_id is None and next_candidate < len(idle_candidates):
                parked[wait_path] = idle_candidates[next_candidate]
                next_candidate += 1

        return [
            wait_path
            for wait_path, lock_id in entries
            if lock_id == target_lock_id or parked.get(wait_path) == target_lock_id
        ]

    @classmethod
    def reserve(
        cls,
        description: str = "Artemis task",
        device_id: str = "pending",
        *,
        session_id: str | None = None,
        ingress: str | None = None,
        lock_scope: str | None = None,
    ) -> str:
        """Reserve a FIFO position in the global queue before a worker process is spawned."""
        token = uuid.uuid4().hex
        queue_dir = get_temp_dir("device-locks") / "artemis-global-device.queue"
        queue_dir.mkdir(parents=True, exist_ok=True)
        payload = cls._owner_payload(
            token,
            device_id,
            description,
            session_id=session_id,
            ingress=ingress,
            lock_scope=lock_scope or os.getenv(cls.LOCK_SCOPE_ENV) or None,
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
        if queue_dir.exists():
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
        lock_scope: str | None = None,
    ) -> bool:
        """Transfer a submission ticket to its worker process."""
        queue_dir = get_temp_dir("device-locks") / "artemis-global-device.queue"
        if not queue_dir.exists():
            return False
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
        effective_scope = lock_scope or os.getenv(cls.LOCK_SCOPE_ENV) or None
        if effective_scope:
            payload["lock_scope"] = str(effective_scope)
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
                lock_scope=self.lock_scope,
            )
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            return path

        self.queue_ticket = self.reserve(
            self.description,
            self.device_id,
            session_id=self.session_id,
            ingress=self.ingress,
            lock_scope=self.lock_scope,
        )
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
            lock_scope=self.lock_scope,
        )
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            current_owner = self._read_owner(self.path)
            if (
                current_owner is not None
                and current_owner.pid == os.getpid()
                and (
                    current_owner.token == self.token
                    or (self.session_id and current_owner.session_id == self.session_id)
                )
            ):
                self._acquired = True
                return True
            self._remove_stale_lock()
            return False
        try:
            os.write(fd, json.dumps(owner_payload, ensure_ascii=False).encode("utf-8"))
        finally:
            os.close(fd)
        self._acquired = True
        return True

    @property
    def effective_concurrency(self) -> int:
        """Resolve effective concurrency limit:
        0: per-device concurrency (multiple devices run concurrently, FIFO per device)
        1: global concurrency (strict single-task serialization across all devices)
        >1: global concurrency capped at N tasks across all devices
        """
        if self.concurrency_mode is not None:
            mode = self.concurrency_mode
            if mode in ("global", "serial", "1"):
                return 1
            if mode in ("per_device", "device", "parallel", "0"):
                return 0
        if self.max_concurrency is not None:
            return self.max_concurrency
        env_mode = os.environ.get("ARTEMIS_CONCURRENCY_MODE", "").strip().lower()
        if env_mode in ("global", "serial", "1"):
            return 1
        if env_mode in ("per_device", "device", "parallel", "0"):
            return 0
        try:
            return int(os.environ.get("ARTEMIS_MAX_CONCURRENT_TASKS", 0))
        except (ValueError, TypeError):
            return 0

    def acquire(
        self,
        *,
        blocking: bool = True,
        timeout: float | None = None,
        poll_interval: float = _DEFAULT_POLL_INTERVAL_SECONDS,
        cancel_event=None,
    ) -> None:
        """Wait in FIFO order and acquire exclusive access to the target Artemis device."""
        self._queue_path = self._claim_or_create_queue_ticket()
        started_at = time.monotonic()
        effective_concurrency = self.effective_concurrency
        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise DeviceBusyError("Waiting for the Artemis device queue was cancelled.")
                self._remove_stale_queue_entries()

                if not self._queue_path.exists():
                    raise DeviceBusyError("The Artemis queue reservation was cancelled.")

                # 1. Check global concurrency limit if configured (> 0)
                if effective_concurrency > 0:
                    active_owners = self.get_active_owners()
                    active_other_owners = [
                        o
                        for o in active_owners.values()
                        if not (
                            o.pid == os.getpid()
                            and (
                                (self.session_id and o.session_id == self.session_id)
                                or o.token == self.token
                            )
                        )
                    ]
                    if len(active_other_owners) >= effective_concurrency:
                        if not blocking:
                            owner_desc = (
                                active_other_owners[0].description
                                if active_other_owners
                                else "active task"
                            )
                            raise DeviceBusyError(
                                f"Global task concurrency limit ({effective_concurrency}) reached ({owner_desc})."
                            )
                        if timeout is not None and time.monotonic() - started_at >= timeout:
                            raise DeviceBusyError(
                                f"Timed out waiting for a concurrency slot after {timeout:.1f}s."
                            )
                        time.sleep(max(0.01, poll_interval))
                        continue

                # 2. Determine FIFO eligibility for this specific device
                all_wait_files = sorted(self.queue_dir.glob("*.wait"))
                if effective_concurrency == 1:
                    # In strict serial mode, global FIFO applies; re-entrant parent processes are always eligible
                    active_owners = self.get_active_owners()
                    is_reentrant = any(
                        o.pid == os.getpid()
                        and (
                            (self.session_id and o.session_id == self.session_id)
                            or o.token == self.token
                        )
                        for o in active_owners.values()
                    )
                    is_eligible = is_reentrant or bool(
                        all_wait_files and all_wait_files[0] == self._queue_path
                    )
                else:
                    # In multi-device parallel mode, per-device FIFO applies.
                    # Unclaimed pending/any tickets are parked on at most one
                    # idle device instead of gating the head of every device's
                    # queue (see _build_device_queue).
                    device_queue = self._build_device_queue(
                        all_wait_files,
                        target_lock_id=self.clean_device_id,
                        target_scope=self.lock_scope,
                    )
                    is_eligible = bool(device_queue and device_queue[0] == self._queue_path)

                if is_eligible and self._try_acquire_owner_lock():
                    self._safe_unlink(self._queue_path)
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
                self._safe_unlink(self._queue_path)
                self._queue_path = None
                if not self._initial_queue_ticket:
                    self.queue_ticket = None
            raise

    def release(self) -> None:
        if not self._acquired:
            return
        try:
            owner = self._read_owner(self.path)
            if owner is not None and (
                owner.token == self.token
                or (self.session_id and owner.session_id == self.session_id)
            ):
                self._safe_unlink(self.path)
        finally:
            self._acquired = False

    @classmethod
    def _get_active_owner_for_path(cls, path: Path) -> DeviceLockOwner | None:
        owner = None
        for attempt in range(3):
            owner = cls._read_owner(path)
            if owner is not None or not path.exists():
                break
            if attempt < 2:
                time.sleep(0.01)
        if owner is None:
            try:
                if (
                    path.exists()
                    and time.time() - path.stat().st_mtime >= cls._MALFORMED_LOCK_GRACE_SECONDS
                ):
                    cls._safe_unlink(path)
            except OSError:
                pass
            return None
        if cls._owner_is_alive(owner):
            return owner
        cls._safe_unlink(path)
        return None

    @classmethod
    def get_active_owners(cls) -> dict[str, DeviceLockOwner]:
        """Return a mapping of clean_device_id -> live DeviceLockOwner across all devices."""
        lock_dir = get_temp_dir("device-locks")
        if not lock_dir.exists():
            return {}
        owners: list[DeviceLockOwner] = []
        seen_tokens: set[str] = set()

        for lock_path in sorted(lock_dir.glob("artemis-device-*.lock")):
            owner = cls._get_active_owner_for_path(lock_path)
            if owner is not None and owner.token not in seen_tokens:
                owners.append(owner)
                seen_tokens.add(owner.token)

        # Also check legacy global lock if still present
        global_lock = lock_dir / "artemis-global-device.lock"
        if global_lock.exists():
            owner = cls._get_active_owner_for_path(global_lock)
            if owner is not None and owner.token not in seen_tokens:
                owners.append(owner)
                seen_tokens.add(owner.token)

        active_map: dict[str, DeviceLockOwner] = {}
        owners_by_device: dict[str, list[DeviceLockOwner]] = {}
        for owner in owners:
            key = cls._normalize_device_id(owner.device_id)
            owners_by_device.setdefault(key, []).append(owner)
        for device_key, device_owners in owners_by_device.items():
            if len(device_owners) == 1:
                active_map[device_key] = device_owners[0]
                continue
            for owner in device_owners:
                scoped_key = cls._normalize_lock_id(owner.device_id, owner.lock_scope)
                active_map[scoped_key] = owner
        return active_map

    @classmethod
    def get_active_owner(
        cls,
        device_id: str | None = None,
        lock_scope: str | None = None,
    ) -> DeviceLockOwner | None:
        """Return the live process owning the lock for a specific device, or any device if None."""
        lock_dir = get_temp_dir("device-locks")
        if device_id is not None:
            effective_scope = lock_scope or os.getenv(cls.LOCK_SCOPE_ENV) or None
            clean_id = cls._normalize_lock_id(device_id, effective_scope)
            dev_path = lock_dir / f"artemis-device-{clean_id}.lock"
            owner = cls._get_active_owner_for_path(dev_path)
            if owner is not None:
                return owner
            if effective_scope:
                legacy_path = lock_dir / (
                    f"artemis-device-{cls._normalize_device_id(device_id)}.lock"
                )
                owner = cls._get_active_owner_for_path(legacy_path)
                if owner is not None:
                    return owner
            # Check legacy global lock as fallback
            global_path = lock_dir / "artemis-global-device.lock"
            if global_path.exists():
                return cls._get_active_owner_for_path(global_path)
            return None

        # Return any active owner for single-device caller backward compatibility
        active_owners = cls.get_active_owners()
        for owner in active_owners.values():
            return owner
        return None

    @classmethod
    def get_queued_tasks(cls, device_id: str | None = None) -> list[dict[str, Any]]:
        """Return all live pending queue tickets across all devices in FIFO order."""
        lock_dir = get_temp_dir("device-locks")
        queue_dir = lock_dir / "artemis-global-device.queue"
        if not queue_dir.exists():
            return []

        target_scope = os.getenv(cls.LOCK_SCOPE_ENV) or None
        clean_target = cls._normalize_lock_id(device_id, target_scope) if device_id else None
        active_tokens = {o.token for o in cls.get_active_owners().values()}
        queued: list[dict[str, Any]] = []

        for path in sorted(queue_dir.glob("*.wait")):
            owner = cls._read_owner(path)
            if owner is None:
                continue
            # If the owner process died, ignore
            if not cls._owner_is_alive(owner):
                continue
            # If this token already acquired the lock, it is actively running, not pending
            if owner.token in active_tokens:
                continue

            if clean_target is not None and not cls._ticket_targets_device(
                owner,
                target_lock_id=clean_target,
                target_scope=target_scope,
                include_pending=True,
            ):
                continue

            try:
                created_at = path.stat().st_mtime
            except OSError:
                created_at = time.time()

            queued.append(
                {
                    "session_id": owner.session_id or f"queued-{owner.token[:8]}",
                    "goal": owner.description,
                    "device_id": owner.device_id,
                    "device_serial": owner.device_id,
                    "adb_endpoint_id": owner.lock_scope,
                    "pid": owner.pid,
                    "token": owner.token,
                    "ingress": owner.ingress or "unknown",
                    "status": "pending",
                    "created_at": created_at,
                    "start_time": created_at,
                }
            )
        return queued

    @classmethod
    def has_owner_record(cls, device_id: str | None = None) -> bool:
        """Return whether an owner record exists, including a record being written."""
        lock_dir = get_temp_dir("device-locks")
        if not lock_dir.exists():
            return False
        if device_id is not None:
            clean_id = cls._normalize_lock_id(
                device_id,
                os.getenv(cls.LOCK_SCOPE_ENV) or None,
            )
            return (lock_dir / f"artemis-device-{clean_id}.lock").exists() or (
                lock_dir / "artemis-global-device.lock"
            ).exists()
        return (
            bool(list(lock_dir.glob("artemis-device-*.lock")))
            or (lock_dir / "artemis-global-device.lock").exists()
        )

    @classmethod
    def is_active_owner(cls, expected: DeviceLockOwner, device_id: str | None = None) -> bool:
        """Revalidate a previously read owner before performing a destructive action."""
        target_dev = device_id or expected.device_id
        lock_dir = get_temp_dir("device-locks")
        clean_id = cls._normalize_lock_id(target_dev, expected.lock_scope)
        path = lock_dir / f"artemis-device-{clean_id}.lock"
        if not path.exists():
            path = lock_dir / "artemis-global-device.lock"
        current = cls._read_owner(path)
        return bool(
            current
            and current.token == expected.token
            and current.pid == expected.pid
            and abs(current.process_created_at - expected.process_created_at) < 1.0
            and cls._owner_is_alive(current)
        )

    @classmethod
    def _find_lock_by_pid(cls, pid: int) -> Path | None:
        lock_dir = get_temp_dir("device-locks")
        if not lock_dir.exists():
            return None
        for path in sorted(lock_dir.glob("artemis-device-*.lock")):
            owner = cls._read_owner(path)
            if owner is not None and owner.pid == pid:
                return path
        global_path = lock_dir / "artemis-global-device.lock"
        if global_path.exists():
            owner = cls._read_owner(global_path)
            if owner is not None and owner.pid == pid:
                return global_path
        return None

    @classmethod
    def annotate_active_owner(
        cls,
        *,
        session_id: str,
        ingress: str | None = None,
        device_id: str | None = None,
    ) -> bool:
        """Attach session metadata after tracing starts inside the lease owner."""
        lock_dir = get_temp_dir("device-locks")
        if device_id is not None:
            clean_id = cls._normalize_device_id(device_id)
            path = lock_dir / f"artemis-device-{clean_id}.lock"
            if not path.exists():
                path = lock_dir / "artemis-global-device.lock"
        else:
            path = cls._find_lock_by_pid(os.getpid())
            if path is None:
                path = lock_dir / "artemis-global-device.lock"

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
            if not cls._safe_replace(temp_path, path):
                return False
        except OSError:
            return False
        finally:
            cls._safe_unlink(temp_path)
        return True

    @classmethod
    def cleanup_stale_locks(cls, device_id: str | None = None) -> int:
        """Remove execution leases and queue tickets whose owners are gone."""
        removed = 0
        lock_dir = get_temp_dir("device-locks")
        if not lock_dir.exists():
            return removed

        patterns = (
            [f"artemis-device-{cls._normalize_device_id(device_id)}.lock"]
            if device_id
            else ["artemis-device-*.lock", "artemis-global-device.lock", "*.lock"]
        )
        seen_paths: set[Path] = set()
        for pat in patterns:
            for path in lock_dir.glob(pat):
                if path in seen_paths:
                    continue
                seen_paths.add(path)
                owner = cls._read_owner(path)
                if owner is not None and cls._owner_is_alive(owner):
                    continue
                if owner is None:
                    try:
                        if time.time() - path.stat().st_mtime < cls._MALFORMED_LOCK_GRACE_SECONDS:
                            continue
                    except OSError:
                        continue
                if cls._safe_unlink(path):
                    removed += 1

        for q_dir in lock_dir.glob("*.queue"):
            if q_dir.is_dir():
                for path in q_dir.glob("*.wait"):
                    owner = cls._read_owner(path)
                    if owner is not None and cls._owner_is_alive(owner):
                        continue
                    if owner is None:
                        try:
                            if (
                                time.time() - path.stat().st_mtime
                                < cls._MALFORMED_LOCK_GRACE_SECONDS
                            ):
                                continue
                        except OSError:
                            continue
                    if cls._safe_unlink(path):
                        removed += 1
        return removed
