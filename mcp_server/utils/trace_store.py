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

"""Trace storage and lifecycle management for MCP background automation tasks."""

import json
import os
import time
from typing import Any

try:
    from artemis.config.paths import get_traces_dir

    TRACES_DIR = str(get_traces_dir())
except Exception:
    PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    TRACES_DIR = os.path.join(PROJECT_ROOT, "traces")


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
    """Reads and returns the status.json for a given trace_id, or None if it doesn't exist."""
    path = get_status_path(trace_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def write_status(trace_id: str, data: dict[str, Any]) -> None:
    """Writes the given status dictionary to the trace's status.json."""
    path = get_status_path(trace_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def update_trace_status(
    trace_id: str,
    status: str,
    error: str | None = None,
    result: Any | None = None,
    device_serial: str | None = None,
) -> dict[str, Any] | None:
    """Updates specific fields of the status.json for a given trace_id."""
    data = read_status(trace_id)
    if not data:
        return None

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
    data = read_status(trace_id)
    if not data:
        return None

    data["device_serial"] = device_serial
    write_status(trace_id, data)
    return data
