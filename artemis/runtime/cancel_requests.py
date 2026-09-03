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

"""Cross-process cooperative cancellation for task workers.

Workers use ``CREATE_NO_WINDOW`` on Windows, so console signals cannot reach
them reliably. The daemon writes a cancel marker in the shared temp directory;
the worker polls it and cancels its asyncio task, allowing recording, trace
and device-lease cleanup to finish. The daemon kills unresponsive workers
after a deadline.

Markers use the session id, falling back to the PID and process creation time
to avoid cancelling a different process after PID reuse.
"""

from __future__ import annotations

from collections.abc import Callable
import json
import os
from pathlib import Path
import time
from typing import Any
import uuid

from artemis.config.paths import get_temp_dir

CANCEL_DIR_NAME = "cancel-requests"
MARKER_TTL_SECONDS = 3600.0
DEFAULT_POLL_SECONDS = 0.5


def cancel_request_dir() -> Path:
    """Directory holding cancel markers (created on demand)."""
    directory = get_temp_dir(CANCEL_DIR_NAME)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _session_marker(session_id: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(session_id).strip())
    return cancel_request_dir() / f"session-{safe}.cancel"


def _pid_marker(pid: int) -> Path:
    return cancel_request_dir() / f"pid-{int(pid)}.cancel"


def _write_marker(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


def _read_marker(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _is_expired(payload: dict[str, Any] | None, path: Path, now: float) -> bool:
    requested_at = None
    if payload is not None:
        try:
            requested_at = float(payload.get("requested_at"))
        except (TypeError, ValueError):
            requested_at = None
    if requested_at is None:
        try:
            requested_at = path.stat().st_mtime
        except OSError:
            return True
    return (now - requested_at) > MARKER_TTL_SECONDS


def request_cancel(
    *,
    session_id: str | None = None,
    pid: int | None = None,
    process_created_at: float = 0.0,
    reason: str = "",
) -> list[Path]:
    """Ask the worker identified by ``session_id`` and/or ``pid`` to cancel.

    Returns the marker paths that were written. Writing is best-effort: an
    unwritable temp directory yields an empty list and the caller falls back
    to a hard kill.
    """
    written: list[Path] = []
    now = time.time()
    payload = {
        "requested_at": now,
        "reason": reason,
        "session_id": str(session_id) if session_id else None,
        "pid": int(pid) if pid else None,
        "process_created_at": float(process_created_at or 0.0),
    }
    targets: list[Path] = []
    if session_id:
        targets.append(_session_marker(str(session_id)))
    if pid:
        targets.append(_pid_marker(int(pid)))
    for target in targets:
        try:
            _write_marker(target, payload)
            written.append(target)
        except OSError:
            continue
    purge_expired(now)
    return written


def is_cancel_requested(
    *,
    session_id: str | None = None,
    pid: int | None = None,
    process_created_at: float | None = None,
) -> bool:
    """Return True when a live cancel marker targets this worker.

    A PID marker is honoured only when its recorded process creation time is
    absent or matches ``process_created_at`` (within one second), which
    protects a fresh worker from a marker left behind for a dead one.
    """
    now = time.time()
    if session_id:
        path = _session_marker(str(session_id))
        if path.exists():
            payload = _read_marker(path)
            if not _is_expired(payload, path, now):
                return True
    if pid:
        path = _pid_marker(int(pid))
        if path.exists():
            payload = _read_marker(path)
            if payload is None:
                # Partially written or unreadable marker: retry on the next poll.
                return False
            if _is_expired(payload, path, now):
                return False
            try:
                recorded = float(payload.get("process_created_at") or 0.0)
            except (TypeError, ValueError):
                recorded = 0.0
            if (
                recorded > 0
                and process_created_at is not None
                and process_created_at > 0
                and abs(recorded - process_created_at) >= 1.0
            ):
                return False
            return True
    return False


def clear_cancel_request(*, session_id: str | None = None, pid: int | None = None) -> None:
    """Remove the markers for a worker (best-effort)."""
    targets: list[Path] = []
    if session_id:
        targets.append(_session_marker(str(session_id)))
    if pid:
        targets.append(_pid_marker(int(pid)))
    for target in targets:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            continue


def purge_expired(now: float | None = None) -> int:
    """Delete markers older than the TTL. Returns the number removed."""
    now = time.time() if now is None else now
    removed = 0
    try:
        entries = list(cancel_request_dir().glob("*.cancel"))
    except OSError:
        return 0
    for path in entries:
        payload = _read_marker(path)
        if _is_expired(payload, path, now):
            try:
                path.unlink(missing_ok=True)
                removed += 1
            except OSError:
                continue
    return removed


def current_process_created_at() -> float:
    """Creation time of the current process, or 0.0 when unavailable."""
    try:
        import psutil

        return float(psutil.Process(os.getpid()).create_time())
    except Exception:
        return 0.0


async def watch_for_cancel_request(
    on_cancel: Callable[[], Any],
    *,
    session_id: str | None = None,
    pid: int | None = None,
    process_created_at: float | None = None,
    poll_seconds: float | None = None,
) -> bool:
    """Poll for a cancel marker and invoke ``on_cancel`` once it appears.

    Returns True when a cancellation was delivered. The coroutine runs until
    it delivers one or is itself cancelled, so callers should run it as a
    background task alongside the work it guards.
    """
    import asyncio

    if poll_seconds is None:
        try:
            poll_seconds = float(os.getenv("ARTEMIS_CANCEL_POLL_SECONDS", DEFAULT_POLL_SECONDS))
        except ValueError:
            poll_seconds = DEFAULT_POLL_SECONDS
    poll_seconds = max(0.05, poll_seconds)
    if pid is None:
        pid = os.getpid()
    if process_created_at is None:
        process_created_at = current_process_created_at()

    while True:
        try:
            requested = is_cancel_requested(
                session_id=session_id,
                pid=pid,
                process_created_at=process_created_at,
            )
        except Exception:
            requested = False
        if requested:
            clear_cancel_request(session_id=session_id, pid=pid)
            result = on_cancel()
            if asyncio.iscoroutine(result):
                await result
            return True
        await asyncio.sleep(poll_seconds)
