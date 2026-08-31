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

"""Cross-process ownership for Android's shell screen-bright wake lock."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

from artemis.config.paths import get_temp_dir
from artemis.utils.logger import get_logger

logger = get_logger(__name__)

_WAKE_LOCK_TYPE = "SCREEN_BRIGHT_WAKE_LOCK"
_WAKE_LOCK_RE = re.compile(
    rf"{_WAKE_LOCK_TYPE}:.*\bheld=(true|false)\b.*\brefCount=(\d+)\b",
    re.IGNORECASE,
)


class ScreenAwakeLease:
    """Share exactly one Android shell wake-lock reference across clients.

    ``cmd power set-wakelock`` exposes one ref-counted shell wake lock for a
    display/type pair. Calling it independently from every process leaks
    references whenever a process crashes. PID-aware lease files make the
    Android reference a process-shared resource instead: the first live client
    normalizes stale references and acquires one, later clients only register,
    and the last client drains the shared reference on disconnect.
    """

    _MUTEX_TIMEOUT_SECONDS = 10.0
    _MUTEX_POLL_SECONDS = 0.05
    _MALFORMED_GRACE_SECONDS = 5.0
    _MAX_REFERENCE_DRAIN = 256

    def __init__(self, device_id: str):
        self.device_id = device_id
        self.token = uuid.uuid4().hex
        device_hash = hashlib.sha256(device_id.encode("utf-8")).hexdigest()[:16]
        root = get_temp_dir("awake-leases")
        self.lease_dir = root / f"{device_hash}.leases"
        self.mutex_path = root / f"{device_hash}.mutex"
        self.lease_path = self.lease_dir / f"{self.token}.lease"
        self._acquired = False

    @staticmethod
    def _process_created_at(pid: int | None = None) -> float:
        try:
            import psutil

            return float(psutil.Process(pid or os.getpid()).create_time())
        except Exception:
            return 0.0

    @staticmethod
    def _owner_is_alive(payload: dict) -> bool:
        try:
            import psutil

            pid = int(payload["pid"])
            process = psutil.Process(pid)
            if not process.is_running():
                return False
            created_at = float(payload.get("process_created_at", 0.0))
            return created_at <= 0 or abs(process.create_time() - created_at) < 1.0
        except Exception:
            return False

    @staticmethod
    def _read_payload(path: Path) -> dict | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    @classmethod
    def _path_is_stale(cls, path: Path) -> bool:
        payload = cls._read_payload(path)
        if payload is not None:
            return not cls._owner_is_alive(payload)
        try:
            return time.time() - path.stat().st_mtime >= cls._MALFORMED_GRACE_SECONDS
        except OSError:
            return True

    def _payload(self) -> dict:
        return {
            "pid": os.getpid(),
            "process_created_at": self._process_created_at(),
            "token": self.token,
            "device_id": self.device_id,
            "created_at": datetime.now(UTC).isoformat(),
        }

    @contextmanager
    def _mutex(self):
        self.mutex_path.parent.mkdir(parents=True, exist_ok=True)
        started_at = time.monotonic()
        acquired = False
        while not acquired:
            try:
                fd = os.open(self.mutex_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self._path_is_stale(self.mutex_path):
                    try:
                        self.mutex_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    continue
                if time.monotonic() - started_at >= self._MUTEX_TIMEOUT_SECONDS:
                    raise TimeoutError("Timed out acquiring the Artemis awake-lease mutex.")
                time.sleep(self._MUTEX_POLL_SECONDS)
            else:
                try:
                    os.write(fd, json.dumps(self._payload()).encode("utf-8"))
                finally:
                    os.close(fd)
                acquired = True
        try:
            yield
        finally:
            payload = self._read_payload(self.mutex_path)
            if payload is not None and payload.get("token") == self.token:
                try:
                    self.mutex_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _run(self, args: list[str], description: str) -> subprocess.CompletedProcess[str] | None:
        try:
            result = subprocess.run(
                ["adb", "-s", self.device_id, *args],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()
                logger.warning(
                    f"Could not {description} on {self.device_id}: "
                    f"{detail or 'ADB command failed'}"
                )
            return result
        except Exception as exc:
            logger.warning(f"Could not {description} on {self.device_id}: {exc}")
            return None

    def _wake_lock_state(self) -> tuple[bool, int] | None:
        result = self._run(
            ["shell", "cmd", "power", "set-wakelock", "list"],
            "inspect the shared screen-bright wake lock",
        )
        if result is None or result.returncode != 0:
            return None
        match = _WAKE_LOCK_RE.search(result.stdout)
        if match is None:
            return (False, 0)
        return (match.group(1).lower() == "true", int(match.group(2)))

    def _set_wake_lock(self, action: str) -> bool:
        result = self._run(
            [
                "shell",
                "cmd",
                "power",
                "set-wakelock",
                action,
                "-d",
                "0",
                _WAKE_LOCK_TYPE,
            ],
            f"{action} the shared screen-bright wake lock",
        )
        return result is not None and result.returncode == 0

    def _drain_references(self) -> bool:
        """Release legacy or crashed-process references until none remain."""
        for _ in range(self._MAX_REFERENCE_DRAIN):
            state = self._wake_lock_state()
            if state is None:
                return False
            held, count = state
            if not held or count <= 0:
                return True
            if not self._set_wake_lock("release"):
                return False
        logger.warning(
            f"Refused to drain more than {self._MAX_REFERENCE_DRAIN} shared wake-lock "
            f"references on {self.device_id}"
        )
        return False

    def _live_leases(self) -> list[Path]:
        self.lease_dir.mkdir(parents=True, exist_ok=True)
        live: list[Path] = []
        for path in self.lease_dir.glob("*.lease"):
            if self._path_is_stale(path):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            else:
                live.append(path)
        return live

    def acquire(self) -> bool:
        if self._acquired:
            return True
        try:
            with self._mutex():
                live = self._live_leases()
                state = self._wake_lock_state()
                if state is None:
                    return False

                if not live:
                    # No registered owner means every existing reference is
                    # legacy or belongs to a crashed process.
                    if not self._drain_references():
                        return False
                    state = (False, 0)

                held, count = state
                if not held or count <= 0:
                    if not self._set_wake_lock("acquire"):
                        return False
                    verified = self._wake_lock_state()
                    if verified is None or not verified[0] or verified[1] <= 0:
                        return False

                self.lease_path.write_text(
                    json.dumps(self._payload(), ensure_ascii=False), encoding="utf-8"
                )
                self._acquired = True
                return True
        except Exception as exc:
            logger.warning(f"Could not acquire shared awake lease on {self.device_id}: {exc}")
            return False

    def release(self) -> None:
        if not self._acquired:
            return
        try:
            with self._mutex():
                self.lease_path.unlink(missing_ok=True)
                if not self._live_leases():
                    self._drain_references()
        except Exception as exc:
            logger.warning(f"Could not release shared awake lease on {self.device_id}: {exc}")
        finally:
            self._acquired = False

    def cleanup_unowned_references(self) -> bool:
        """Drain legacy shell wake locks only when no live old host owns them."""
        try:
            with self._mutex():
                if self._live_leases():
                    return False
                return self._drain_references()
        except Exception as exc:
            logger.warning(
                f"Could not clean unowned screen wake locks on {self.device_id}: {exc}"
            )
            return False
