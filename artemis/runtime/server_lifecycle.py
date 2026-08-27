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
from typing import Any

from artemis.config.paths import ROOT_DIR, get_server_info_file
from artemis.runtime.device_lock import DeviceExecutionLock
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
    host: str = "0.0.0.0",
    pid: int | None = None,
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


def clear_server_info() -> None:
    """Remove the persisted server metadata file if it exists."""
    info_file = get_server_info_file()
    try:
        if info_file.exists():
            info_file.unlink()
            logger.debug(f"Cleared server info file at {info_file}")
    except Exception as e:
        logger.debug(f"Error clearing server info file {info_file}: {e}")


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
            try:
                import psutil

                if psutil.pid_exists(saved_pid):
                    discovered.add(saved_pid)
            except Exception:
                try:
                    os.kill(saved_pid, 0)
                    discovered.add(saved_pid)
                except OSError:
                    pass

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
    timeout: float = 4.0,
    force: bool = False,
) -> tuple[bool, str, list[int]]:
    """Gracefully terminate any running Artemis server on `port`.

    Args:
        port: TCP port to inspect and terminate.
        timeout: Maximum seconds to wait for graceful process exit.
        force: If True, immediately send SIGKILL without waiting.

    Returns:
        tuple (success, message, affected_pids)
    """
    pids = find_server_pids(port)

    if not pids and not is_port_in_use(port):
        clear_server_info()
        DeviceExecutionLock.cleanup_stale_locks()
        return True, f"No active server detected on port {port}.", []

    # If no PID was discovered but port is still in use, try one more aggressive discovery
    if not pids and is_port_in_use(port):
        time.sleep(0.2)
        pids = find_server_pids(port)

    stopped_pids: list[int] = []
    current_pid = os.getpid()

    for pid in pids:
        if pid == current_pid:
            # Don't terminate self during stop_server call
            continue
        try:
            if force:
                ProcessSupervisor.terminate_tree(pid, timeout_seconds=0.5)
            else:
                ProcessSupervisor.terminate_tree(pid, timeout_seconds=timeout)
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
    clear_server_info()

    port_free = not is_port_in_use(port)
    if port_free:
        pid_str = f" (PID: {', '.join(map(str, stopped_pids))})" if stopped_pids else ""
        msg = f"Artemis server stopped{pid_str}. Port {port} is released."
        if cleaned_locks:
            msg += f" Cleaned up {cleaned_locks} device lock(s)."
        return True, msg, stopped_pids
    else:
        return (
            False,
            f"Port {port} remains occupied after stop attempt. Some background process may require manual termination.",
            stopped_pids,
        )
