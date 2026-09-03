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

"""Client utilities for communicating with and auto-spawning the Artemis Daemon."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any
import urllib.error
import urllib.request

from artemis.config.paths import ROOT_DIR
from artemis.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_DAEMON_HOST = os.environ.get("ARTEMIS_DAEMON_HOST", "127.0.0.1")
DEFAULT_DAEMON_PORT = int(os.environ.get("ARTEMIS_DAEMON_PORT", "8000"))


def is_standalone_forced() -> bool:
    """Return True if standalone/embedded execution is explicitly configured."""
    return os.environ.get("ARTEMIS_STANDALONE", "").lower() in ("1", "true", "yes")


def is_daemon_running(
    host: str = DEFAULT_DAEMON_HOST,
    port: int = DEFAULT_DAEMON_PORT,
    timeout: float = 0.3,
) -> bool:
    """Fast probe to check if the Artemis Daemon HTTP server is responding."""
    url = f"http://{host}:{port}/api/system/readiness"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Artemis-Daemon-Probe"})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status in (200, 400, 404, 503)
    except Exception:
        # Fallback probe to root endpoint
        try:
            root_url = f"http://{host}:{port}/"
            req = urllib.request.Request(root_url, headers={"User-Agent": "Artemis-Daemon-Probe"})
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.status in (200, 301, 302, 404)
        except Exception:
            return False


def daemon_log_path(port: int = DEFAULT_DAEMON_PORT) -> Path:
    """Return the log file path used by a background-spawned Daemon on the given port."""
    from artemis.config.paths import get_app_dir

    return get_app_dir() / "logs" / f"daemon-{port}.log"


def spawn_daemon(
    host: str = DEFAULT_DAEMON_HOST,
    port: int = DEFAULT_DAEMON_PORT,
) -> subprocess.Popen | None:
    """Silently spawn the Artemis Daemon server in the background as a detached process.

    The server is always launched via ``sys.executable -m`` (never through the
    ``artemis``/``artemis-admin`` console-script shims) so the long-lived process
    chain never holds a lock on ``Scripts/artemis.exe`` — on Windows, a resident
    shim blocks ``uv sync``/reinstalls with "os error 32" until it exits.
    """
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"

    cmd = [
        sys.executable,
        "-m",
        "apps.admin_console.server",
        "--host",
        host,
        "--port",
        str(port),
    ]

    # Persist daemon output for post-mortem debugging (daemon mode is the
    # default for `artemis restart`, so silent DEVNULL would hide crashes).
    log_handle: Any = subprocess.DEVNULL
    try:
        log_file = daemon_log_path(port)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_handle = open(log_file, "ab")
    except Exception:
        log_handle = subprocess.DEVNULL

    kwargs: dict[str, Any] = {
        "cwd": str(ROOT_DIR),
        "env": env,
        "stdout": log_handle,
        "stderr": subprocess.STDOUT if log_handle is not subprocess.DEVNULL else subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }

    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )
    else:
        kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(cmd, **kwargs)
        logger.info(
            f"Spawned Artemis Daemon in background (PID {proc.pid}) at http://{host}:{port}"
        )
        return proc
    except Exception as exc:
        logger.warning(f"Could not auto-spawn Artemis Daemon: {exc}")
        return None
    finally:
        # The child inherited the handle; the parent's copy is no longer needed.
        if log_handle is not subprocess.DEVNULL:
            try:
                log_handle.close()
            except OSError:
                pass


def ensure_daemon_running(
    host: str = DEFAULT_DAEMON_HOST,
    port: int = DEFAULT_DAEMON_PORT,
    timeout: float = 2.0,
    wait_ready: bool = False,
) -> tuple[bool, str | None]:
    """Ensure the Artemis Daemon is running, auto-spawning it if necessary.

    Args:
        host: Daemon host to probe.
        port: Daemon port to probe.
        timeout: Maximum seconds to wait if wait_ready is True.
        wait_ready: If True, blocks until Daemon is healthy or timeout reached.
                    If False, spawns asynchronously and returns immediately.

    Returns:
        (True, base_url) if Daemon is accessible.
        (False, None) if standalone mode is forced or Daemon could not be reached.
    """
    if is_standalone_forced():
        return False, None

    if is_daemon_running(host, port):
        return True, f"http://{host}:{port}"

    # Auto-spawn Daemon in background
    spawn_daemon(host, port)

    if not wait_ready:
        return True, f"http://{host}:{port}"

    # Poll until ready or timeout
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        time.sleep(0.1)
        if is_daemon_running(host, port):
            return True, f"http://{host}:{port}"

    logger.warning(f"Daemon did not respond within {timeout}s. Falling back to standalone mode.")
    return False, None


def submit_task_to_daemon(
    goal: str,
    *,
    profile: str = "flash",
    device_serial: str | None = None,
    expected_output: str | None = None,
    enable_outputter: bool | None = None,
    locked_app_package: str | None = None,
    app_path: str | None = None,
    session_id: str | None = None,
    ingress: str = "client",
    conversation_id: str | None = None,
    verification_level: str | None = None,
    explorer_mode: str | None = None,
    base_url: str | None = None,
    timeout: float = 15.0,
) -> dict[str, Any] | None:
    """Submit a task to the running Daemon via REST API.

    ``verification_level`` ('off' | 'final' | 'checkpoints' | 'strict') and
    ``explorer_mode`` ('flash' | 'pro' | 'ultra') are the Pro-profile tuning
    knobs of ``/api/run``; they are forwarded verbatim and ignored by Flash.

    Returns the response JSON dict if successfully enqueued, or None on error.
    """
    url = f"{base_url or f'http://{DEFAULT_DAEMON_HOST}:{DEFAULT_DAEMON_PORT}'}/api/run"
    payload = {
        "goal": goal,
        "profile": profile,
        "device_serial": device_serial,
        "expected_output": expected_output,
        "enable_outputter": enable_outputter,
        "verification_level": verification_level,
        "explorer_mode": explorer_mode,
        "locked_app_package": locked_app_package,
        "app_path": app_path,
        "session_id": session_id,
        "ingress": ingress,
        "conversation_id": conversation_id,
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "Artemis-Client"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status in (200, 201):
                return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        logger.debug(f"Failed to submit task to Daemon at {url}: {exc}")
        return None
    return None


def stop_task_on_daemon(
    session_id: str,
    *,
    base_url: str | None = None,
    timeout: float = 5.0,
) -> bool:
    """Request the running Artemis Daemon to stop a task by session ID."""
    url = f"{base_url or f'http://{DEFAULT_DAEMON_HOST}:{DEFAULT_DAEMON_PORT}'}/api/stop"
    payload = {"session_id": session_id}
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "Artemis-Client"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status == 200:
                resp_data = json.loads(response.read().decode("utf-8"))
                return resp_data.get("status") == "stopped"
    except Exception as exc:
        logger.debug(f"Failed to request stop from Daemon at {url}: {exc}")
        return False
    return False


def get_daemon_session(
    session_id: str,
    *,
    base_url: str | None = None,
    timeout: float = 5.0,
) -> dict[str, Any] | None:
    """Fetch details of a single session from the Artemis Daemon."""
    url = f"{base_url or f'http://{DEFAULT_DAEMON_HOST}:{DEFAULT_DAEMON_PORT}'}/api/sessions/{session_id}"
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Artemis-Client"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status == 200:
                return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        logger.debug(f"Failed to get session {session_id} from Daemon at {url}: {exc}")
        return None
    return None


def get_daemon_status(
    *,
    base_url: str | None = None,
    timeout: float = 5.0,
) -> dict[str, Any] | None:
    """Fetch global scheduler and device status from the Artemis Daemon."""
    url = f"{base_url or f'http://{DEFAULT_DAEMON_HOST}:{DEFAULT_DAEMON_PORT}'}/api/status"
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Artemis-Client"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status == 200:
                return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        logger.debug(f"Failed to get status from Daemon at {url}: {exc}")
        return None
    return None


def submit_batch_to_daemon(
    goals: list[str],
    *,
    profile: str = "flash",
    device_serial: str | None = None,
    ingress: str = "cli",
    verification_level: str | None = None,
    explorer_mode: str | None = None,
    base_url: str | None = None,
    timeout: float = 15.0,
) -> dict[str, Any] | None:
    """Submit a batch of goals to the running Daemon.

    ``verification_level`` / ``explorer_mode`` apply to every goal of the batch
    (see :func:`submit_task_to_daemon`).
    """
    url = f"{base_url or f'http://{DEFAULT_DAEMON_HOST}:{DEFAULT_DAEMON_PORT}'}/api/run"
    payload = {
        "goals": goals,
        "profile": profile,
        "device_serial": device_serial,
        "ingress": ingress,
        "verification_level": verification_level,
        "explorer_mode": explorer_mode,
    }
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "Artemis-Client"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status in (200, 201):
                return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        logger.debug(f"Failed to submit batch to Daemon at {url}: {exc}")
        return None
    return None


def wait_for_daemon_task(
    session_id: str,
    *,
    base_url: str | None = None,
    timeout: float = 600.0,
    poll_interval: float = 1.0,
    on_status_update: Any = None,
) -> dict[str, Any]:
    """Poll the Daemon until the task reaches a terminal status (completed, failed, cancelled)."""
    started = time.monotonic()
    last_status = None

    while time.monotonic() - started < timeout:
        sess = get_daemon_session(session_id, base_url=base_url)
        if sess:
            status = sess.get("status")
            if status != last_status:
                last_status = status
                if callable(on_status_update):
                    on_status_update(sess)

            if status in ("completed", "success", "failed", "cancelled"):
                return sess
        else:
            # Check if task is queued in daemon status
            st = get_daemon_status(base_url=base_url)
            if st:
                queue = st.get("queue") or []
                is_queued = any(
                    isinstance(item, dict) and str(item.get("session_id")) == str(session_id)
                    for item in queue
                )
                current_st = "queued" if is_queued else "launching"
                if current_st != last_status:
                    last_status = current_st
                    if callable(on_status_update):
                        on_status_update({"status": current_st, "session_id": session_id})

        time.sleep(poll_interval)

    return {"session_id": session_id, "status": "timeout", "error": f"Timed out after {timeout}s"}
