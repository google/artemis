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

"""Trace storage and lifecycle management for MCP background automation tasks.

status.json is written and updated concurrently by real independent processes
(the admin console daemon, MCP server tools, and spawned worker subprocesses),
so this module provides:

- Atomic writes (temp file + fsync + ``os.replace``) so readers never observe
  a torn/truncated file.
- A PID-aware ``status.json.lock`` file (O_CREAT | O_EXCL with bounded retry
  and stale-lock detection) serializing read-modify-write updates.
- Three-state reads: missing file (silent ``None``), corrupt JSON (WARNING +
  the file is quarantined as ``status.json.corrupt``), other IO errors
  (WARNING).
"""

from contextlib import contextmanager
import json
import logging
import os
import time
from typing import Any
import uuid

from artemis.runtime.process_probe import pid_is_alive

logger = logging.getLogger(__name__)

try:
    from artemis.config.paths import get_traces_dir

    TRACES_DIR = str(get_traces_dir())
except Exception:
    PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    TRACES_DIR = os.path.join(PROJECT_ROOT, "traces")

_LOCK_TIMEOUT_SECONDS = 5.0
_LOCK_POLL_SECONDS = 0.05
# A lock younger than this is assumed to be held by a live writer and is not
# probed for staleness. Probing opens the lock file, and on Windows an open
# reader handle blocks the owner's unlink (no FILE_SHARE_DELETE), so eager
# probing can leak the lock forever under contention.
_LOCK_STALE_PROBE_MIN_AGE_SECONDS = 0.5
_LOCK_STALE_PROBE_INTERVAL_SECONDS = 1.0
_MALFORMED_LOCK_GRACE_SECONDS = 10.0
_REPLACE_RETRIES = 5
_REPLACE_RETRY_DELAY_SECONDS = 0.02
_UNLINK_RETRIES = 10
_UNLINK_RETRY_DELAY_SECONDS = 0.01


def _own_create_time() -> float:
    try:
        import psutil

        return float(psutil.Process(os.getpid()).create_time())
    except Exception:
        return 0.0


def _replace_with_retry(src: str, dst: str) -> None:
    """``os.replace`` with retries for transient Windows sharing violations."""
    last_error: OSError | None = None
    for attempt in range(_REPLACE_RETRIES):
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            last_error = exc
            if attempt < _REPLACE_RETRIES - 1:
                time.sleep(_REPLACE_RETRY_DELAY_SECONDS)
    raise last_error  # type: ignore[misc]


def _atomic_write_json(path: str, data: dict[str, Any]) -> None:
    """Write JSON atomically: temp file in the same directory + fsync + replace."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = f"{path}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        _replace_with_retry(temp_path, path)
    finally:
        # A leftover temp file (e.g. after a crash between write and replace)
        # never affects readers; remove it best-effort on the success path.
        try:
            os.unlink(temp_path)
        except OSError:
            pass


def _unlink_with_retry(path: str) -> bool:
    """Best-effort unlink retrying transient Windows sharing violations."""
    for attempt in range(_UNLINK_RETRIES):
        try:
            os.unlink(path)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            if attempt < _UNLINK_RETRIES - 1:
                time.sleep(_UNLINK_RETRY_DELAY_SECONDS)
    return False


def _lock_is_stale(lock_path: str) -> bool:
    """A status lock is stale when its owner process is gone or it is malformed and old."""
    try:
        if time.time() - os.path.getmtime(lock_path) < _LOCK_STALE_PROBE_MIN_AGE_SECONDS:
            # Young lock: assume a live writer holds it. Do not open the file
            # (an open reader handle would block the owner's unlink on Windows).
            return False
    except OSError:
        return False
    try:
        with open(lock_path, encoding="utf-8") as f:
            payload = json.load(f)
        pid = int(payload["pid"])
        created_at = float(payload.get("process_created_at", 0.0) or 0.0)
    except FileNotFoundError:
        return False
    except Exception:
        try:
            return time.time() - os.path.getmtime(lock_path) >= _MALFORMED_LOCK_GRACE_SECONDS
        except OSError:
            return False
    return not pid_is_alive(pid, created_at if created_at > 0 else None)


@contextmanager
def _status_lock(path: str):
    """Bounded cross-process mutex for read-modify-write updates of ``path``.

    Yields True when the lock was acquired. On timeout the caller proceeds
    without mutual exclusion (with a WARNING) rather than deadlocking: a
    last-writer-wins update is still strictly better than blocking MCP tools
    forever on a leaked lock.
    """
    lock_path = f"{path}.lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    payload = json.dumps({"pid": os.getpid(), "process_created_at": _own_create_time()})
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    acquired = False
    last_stale_probe = 0.0
    while not acquired:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            now = time.monotonic()
            if now - last_stale_probe >= _LOCK_STALE_PROBE_INTERVAL_SECONDS:
                last_stale_probe = now
                if _lock_is_stale(lock_path):
                    _unlink_with_retry(lock_path)
                    continue
            if now >= deadline:
                logger.warning(
                    f"Timed out acquiring trace status lock {lock_path}; "
                    "proceeding without mutual exclusion"
                )
                break
            time.sleep(_LOCK_POLL_SECONDS)
        except OSError as exc:
            logger.warning(f"Could not create trace status lock {lock_path}: {exc}")
            break
        else:
            try:
                os.write(fd, payload.encode("utf-8"))
            finally:
                os.close(fd)
            acquired = True
    try:
        yield acquired
    finally:
        if acquired and not _unlink_with_retry(lock_path):
            logger.warning(
                f"Could not remove trace status lock {lock_path}; it will be "
                "reaped as stale once its owner exits"
            )


def get_trace_dir(trace_id: str) -> str:
    """Returns the absolute path to the directory for a given trace_id."""
    return os.path.join(TRACES_DIR, trace_id)


def get_status_path(trace_id: str) -> str:
    """Returns the absolute path to the status.json file for a given trace_id."""
    return os.path.join(get_trace_dir(trace_id), "status.json")


def get_trace_notes_dir(trace_id: str) -> str:
    """Returns the absolute path to the notes directory for a given trace_id."""
    return os.path.join(get_trace_dir(trace_id), "notes")


def get_trace_stdout_log_path(trace_id: str) -> str:
    """Returns the absolute path to the stdout.log file for a given trace_id."""
    return os.path.join(get_trace_dir(trace_id), "stdout.log")


def get_trace_stderr_log_path(trace_id: str) -> str:
    """Returns the absolute path to the stderr.log file for a given trace_id."""
    return os.path.join(get_trace_dir(trace_id), "stderr.log")


def init_trace(
    trace_id: str,
    task_desc: str,
    model: str,
    conversation_id: str | None = None,
    device_serial: str | None = None,
) -> dict[str, Any]:
    """Initializes the trace directory and creates the initial status.json file."""
    trace_dir = get_trace_dir(trace_id)
    os.makedirs(trace_dir, exist_ok=True)

    status_data: dict[str, Any] = {
        "trace_id": trace_id,
        "task_desc": task_desc,
        "model": model,
        "conversation_id": conversation_id,
        "status": "running",
        "device_serial": device_serial,
        "start_time": time.time(),
        "end_time": None,
        "error": None,
        "result": None,
    }

    write_status(trace_id, status_data)
    return status_data


def read_status(trace_id: str) -> dict[str, Any] | None:
    """Read the status.json for a trace_id, distinguishing three failure states.

    - Missing file: normal (a trace that was never started) -> silent ``None``.
    - Corrupt JSON: WARNING with the path; the damaged file is renamed to
      ``status.json.corrupt`` as evidence, then ``None`` is returned.
    - Other IO errors: WARNING, ``None``.
    """
    path = get_status_path(trace_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        logger.warning(
            f"Corrupt status.json for trace {trace_id} at {path}: {exc}; "
            f"quarantining as {path}.corrupt"
        )
        try:
            _replace_with_retry(path, f"{path}.corrupt")
        except OSError as move_exc:
            logger.warning(f"Could not quarantine corrupt status file {path}: {move_exc}")
        return None
    except OSError as exc:
        logger.warning(f"Could not read status.json for trace {trace_id} at {path}: {exc}")
        return None


def write_status(trace_id: str, data: dict[str, Any]) -> None:
    """Atomically writes the given status dictionary to the trace's status.json."""
    _atomic_write_json(get_status_path(trace_id), data)


def update_trace_status(
    trace_id: str,
    status: str,
    error: str | None = None,
    result: Any | None = None,
    device_serial: str | None = None,
) -> dict[str, Any] | None:
    """Updates specific fields of the status.json for a given trace_id.

    The read-modify-write cycle holds a cross-process lock so concurrent
    updates from the daemon, MCP tools, and worker processes do not lose each
    other's fields.
    """
    path = get_status_path(trace_id)
    with _status_lock(path):
        data = read_status(trace_id)
        if not data:
            if os.path.exists(path) or os.path.exists(f"{path}.corrupt"):
                logger.warning(
                    f"Dropping status update ({status!r}) for trace {trace_id}: "
                    "existing status.json is unreadable"
                )
            return None

        # "success" is a legacy alias for the canonical "completed" terminal
        # status and must never be persisted into status.json.
        if status == "success":
            status = "completed"

        data["status"] = status
        if status in ("completed", "failed", "cancelled"):
            data["end_time"] = time.time()

        if error is not None:
            data["error"] = error
        if result is not None:
            data["result"] = result
        if device_serial is not None:
            data["device_serial"] = device_serial

        write_status(trace_id, data)
        return data


def update_trace_device_serial(trace_id: str, device_serial: str) -> dict[str, Any] | None:
    """Updates the device_serial field of the status.json for a given trace_id."""
    path = get_status_path(trace_id)
    with _status_lock(path):
        data = read_status(trace_id)
        if not data:
            if os.path.exists(path) or os.path.exists(f"{path}.corrupt"):
                logger.warning(
                    f"Dropping device_serial update for trace {trace_id}: "
                    "existing status.json is unreadable"
                )
            return None

        data["device_serial"] = device_serial
        write_status(trace_id, data)
        return data
