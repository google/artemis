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

"""Artemis Server Lifecycle and Process Management.

Provides cross-platform facilities to discover, inspect, terminate,
and restart running Artemis UI / backend server instances regardless
of which terminal or process spawned them.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from typing import Any, Callable
import urllib.error
import urllib.request

from artemis.config.paths import ROOT_DIR, get_server_info_file
from artemis.runtime.device_lock import DeviceExecutionLock
from artemis.runtime.process_probe import pid_is_alive
from artemis.runtime.supervisor import ProcessSupervisor
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


def is_port_in_use(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    """Check whether a TCP port is currently listening / occupied."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            return s.connect_ex((host, port)) == 0
        except Exception:
            return False


def write_server_info(
    port: int,
    host: str = "127.0.0.1",
    pid: int | None = None,
    lifecycle_token: str | None = None,
) -> Path:
    """Persist current Artemis server runtime metadata to well-known path."""
    info_file = get_server_info_file()
    try:
        info_file.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "pid": pid or os.getpid(),
            "port": port,
            "host": host,
            "started_at": time.time(),
            "cwd": str(ROOT_DIR),
            "sys_executable": sys.executable,
            "cmdline": sys.argv,
        }
        if lifecycle_token:
            data["lifecycle_token"] = lifecycle_token
        info_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        logger.debug(f"Saved Artemis server info (PID: {data['pid']}, port: {port}) to {info_file}")
    except Exception as e:
        logger.warning(f"Could not persist server metadata to {info_file}: {e}")
    return info_file


def read_server_info() -> dict[str, Any] | None:
    """Read and validate the persisted server runtime metadata if present."""
    info_file = get_server_info_file()
    if not info_file.exists():
        return None
    try:
        content = info_file.read_text(encoding="utf-8").strip()
        if not content:
            return None
        data = json.loads(content)
        if isinstance(data, dict) and "pid" in data:
            return data
    except Exception as e:
        logger.debug(f"Failed to read server info from {info_file}: {e}")
    return None


def clear_server_info(
    *,
    port: int | None = None,
    lifecycle_token: str | None = None,
) -> None:
    """Remove persisted metadata, optionally only for the matching server.

    Scoped removal keeps ``artemis stop --port <unused>`` and a late shutdown
    finalizer from deleting metadata written by a different server instance.
    """
    info_file = get_server_info_file()
    try:
        if info_file.exists():
            if port is not None or lifecycle_token is not None:
                info = read_server_info()
                if not info:
                    return
                if port is not None and info.get("port") != port:
                    return
                if lifecycle_token is not None and info.get("lifecycle_token") != lifecycle_token:
                    return
            info_file.unlink()
            logger.debug(f"Cleared server info file at {info_file}")
    except Exception as e:
        logger.debug(f"Error clearing server info file {info_file}: {e}")


def request_graceful_shutdown(port: int, timeout: float = 1.5) -> bool:
    """Ask a locally managed Artemis server to run its shutdown lifecycle.

    The per-process token in the server metadata prevents this control request
    from being accepted remotely or accidentally sent to an unrelated service
    using the same port. A missing/stale token simply makes the caller fall back
    to process termination.
    """
    info = read_server_info()
    if not info or info.get("port") != port:
        return False
    token = info.get("lifecycle_token")
    if not isinstance(token, str) or not token:
        return False

    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/system/shutdown",
        method="POST",
        headers={
            "User-Agent": "Artemis-Lifecycle-Client/1.0",
            "X-Artemis-Lifecycle-Token": token,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status in (200, 202)
    except (OSError, urllib.error.URLError, urllib.error.HTTPError):
        return False
    except Exception:
        return False


# Inverted dependency: the session database lives in the application layer
# (apps.admin_console), which this base runtime package must not import.
# The application layer registers its orphan-session reconciler here
# (apps/admin_console/database/repositories/session_repository.py does so on
# import); entry points that stop servers without loading the admin console
# perform that import as part of their assembly. Without a registration,
# reconciliation is skipped -- the admin console re-runs it on next startup.
_session_reconciler: Callable[[], int] | None = None


def register_session_reconciler(reconciler: Callable[[], int]) -> None:
    """Register the app-layer callback that marks orphaned sessions as failed."""
    global _session_reconciler
    _session_reconciler = reconciler


def _reconcile_orphaned_sessions() -> int:
    """Immediately mark sessions whose worker PIDs died during forced stop."""
    if _session_reconciler is None:
        return 0
    try:
        return _session_reconciler()
    except Exception as exc:
        logger.debug(f"Could not reconcile orphaned sessions after server stop: {exc}")
        return 0


def _any_pid_alive(pids: list[int]) -> bool:
    """Return whether any target process is still running (zombies excluded)."""
    return any(pid_is_alive(pid) for pid in pids)


def find_server_pids(port: int = 8000) -> list[int]:
    """Find all process IDs associated with the Artemis server listening on `port`.

    Uses a multi-tiered discovery strategy:
    1. Check persisted server metadata file.
    2. Check port listeners via lsof (macOS/Linux), fuser (Linux), or netstat (Windows).
    3. Fallback to psutil net_connections and process table scan.
    """
    discovered: set[int] = set()

    # 1. Check metadata file
    info = read_server_info()
    if info and info.get("port") == port:
        saved_pid = info.get("pid")
        if saved_pid and isinstance(saved_pid, int):
            if pid_is_alive(saved_pid):
                discovered.add(saved_pid)

    # 2. Check port listeners with platform-native tools
    # 2a. lsof (macOS & Linux)
    if shutil.which("lsof"):
        try:
            res = subprocess.run(
                ["lsof", "-ti", f":{port}", "-sTCP:LISTEN"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            if res.returncode == 0 and res.stdout.strip():
                for token in res.stdout.strip().splitlines():
                    token = token.strip()
                    if token.isdigit():
                        discovered.add(int(token))
        except Exception:
            pass

    # 2b. fuser (Linux fallback)
    if not discovered and shutil.which("fuser"):
        try:
            res = subprocess.run(
                ["fuser", f"{port}/tcp"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            for token in res.stdout.strip().split():
                token = token.strip()
                if token.isdigit():
                    discovered.add(int(token))
        except Exception:
            pass

    # 2c. netstat (Windows fallback)
    if not discovered and sys.platform == "win32":
        try:
            res = subprocess.run(
                ["netstat", "-ano"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=3.0,
            )
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    if f":{port}" in line and "LISTENING" in line.upper():
                        parts = line.strip().split()
                        if parts and parts[-1].isdigit():
                            discovered.add(int(parts[-1]))
        except Exception:
            pass

    # 2d. psutil net_connections fallback
    if not discovered:
        try:
            import psutil

            for conn in psutil.net_connections(kind="inet"):
                if conn.laddr and conn.laddr.port == port:
                    if conn.pid:
                        discovered.add(conn.pid)
        except Exception:
            pass

    # 3. Process table scan if still nothing found on port but port is in use
    if not discovered and is_port_in_use(port):
        try:
            import psutil

            for proc in psutil.process_iter(["pid", "cmdline"]):
                try:
                    cmdline = " ".join(proc.info.get("cmdline") or [])
                    if "apps.admin_console.server" in cmdline or (
                        "artemis" in cmdline and "ui" in cmdline
                    ):
                        if proc.info.get("pid"):
                            discovered.add(proc.info["pid"])
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception:
            pass

    return sorted(discovered)


def get_server_status(port: int = 8000) -> dict[str, Any]:
    """Retrieve detailed status of the Artemis server on given port."""
    in_use = is_port_in_use(port)
    pids = find_server_pids(port)
    info = read_server_info()

    uptime_seconds: float | None = None
    if info and "started_at" in info and isinstance(info["started_at"], (int, float)):
        uptime_seconds = max(0.0, time.time() - info["started_at"])

    is_running = in_use or bool(pids)

    return {
        "running": is_running,
        "port": port,
        "pids": pids,
        "active_pid": pids[0] if pids else (info.get("pid") if info else None),
        "uptime_seconds": uptime_seconds,
        "url": f"http://localhost:{port}" if is_running else None,
        "admin_url": f"http://localhost:{port}/admin" if is_running else None,
        "metadata": info,
    }


def stop_server(
    port: int = 8000,
    timeout: float = 12.0,
    force: bool = False,
) -> tuple[bool, str, list[int]]:
    """Gracefully terminate any running Artemis server on `port`.

    Args:
        port: TCP port to inspect and terminate.
        timeout: Maximum seconds to wait for the server shutdown lifecycle.
        force: If True, skip the shutdown API and terminate the process tree.

    Returns:
        tuple (success, message, affected_pids)
    """
    pids = find_server_pids(port)

    if not pids and not is_port_in_use(port):
        clear_server_info(port=port)
        DeviceExecutionLock.cleanup_stale_locks()
        return True, f"No active server detected on port {port}.", []

    # If no PID was discovered but port is still in use, try one more aggressive discovery
    if not pids and is_port_in_use(port):
        time.sleep(0.2)
        pids = find_server_pids(port)

    stopped_pids: list[int] = []
    current_pid = os.getpid()

    # A normal stop is an application lifecycle request, not an OS signal.
    # This matters on Windows where taskkill /F and os.kill(..., SIGTERM) both
    # bypass FastAPI's shutdown hook. Give Uvicorn enough time to cancel task
    # workers, persist terminal session state, and release IPC/device resources.
    graceful_requested = False
    if not force:
        graceful_requested = request_graceful_shutdown(
            port,
            timeout=min(2.0, max(0.25, timeout)),
        )
        if graceful_requested:
            deadline = time.time() + max(0.25, timeout)
            while time.time() < deadline:
                if not is_port_in_use(port) and not _any_pid_alive(pids):
                    break
                time.sleep(0.1)

            if not is_port_in_use(port) and not _any_pid_alive(pids):
                cleaned_locks = DeviceExecutionLock.cleanup_stale_locks()
                clear_server_info(port=port)
                pid_str = f" (PID: {', '.join(map(str, pids))})" if pids else ""
                msg = f"Artemis server stopped gracefully{pid_str}. Port {port} is released."
                if cleaned_locks:
                    msg += f" Cleaned up {cleaned_locks} device lock(s)."
                return True, msg, pids

            logger.warning(
                f"Artemis server on port {port} did not exit within {timeout}s; "
                "falling back to process-tree termination."
            )

    for pid in pids:
        if pid == current_pid:
            # Don't terminate self during stop_server call
            continue
        try:
            ProcessSupervisor.terminate_tree(
                pid,
                timeout_seconds=0.5 if force or graceful_requested else timeout,
            )
            stopped_pids.append(pid)
        except Exception as e:
            logger.warning(f"Failed to terminate process {pid}: {e}")

    # Wait for the port to actually become free
    deadline = time.time() + max(1.5, timeout)
    while time.time() < deadline:
        if not is_port_in_use(port):
            break
        time.sleep(0.15)

    # Force kill if port is still occupied
    if is_port_in_use(port) and not force:
        remaining_pids = find_server_pids(port)
        for pid in remaining_pids:
            if pid != current_pid:
                try:
                    ProcessSupervisor.terminate_tree(pid, timeout_seconds=0.5)
                except Exception:
                    pass
        time.sleep(0.3)

    # Cleanup stale device execution locks left by terminated server
    cleaned_locks = DeviceExecutionLock.cleanup_stale_locks()
    reconciled_sessions = _reconcile_orphaned_sessions()
    clear_server_info(port=port)

    port_free = not is_port_in_use(port)
    if port_free:
        pid_str = f" (PID: {', '.join(map(str, stopped_pids))})" if stopped_pids else ""
        msg = f"Artemis server stopped{pid_str}. Port {port} is released."
        if cleaned_locks:
            msg += f" Cleaned up {cleaned_locks} device lock(s)."
        if reconciled_sessions:
            msg += f" Reconciled {reconciled_sessions} interrupted session(s)."
        return True, msg, stopped_pids
    else:
        return (
            False,
            f"Port {port} remains occupied after stop attempt. Some background process may require manual termination.",
            stopped_pids,
        )
