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

"""Universal Model Context Protocol (MCP) Server for ARTEMIS."""

import datetime
import os
import sys

# Ensure repository root is in sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Debug logging and stderr tee to capture crashes without breaking MCP stdio JSON-RPC
_LOG_MAX_BYTES = 5 * 1024 * 1024


def _rotate_log(path: str, max_bytes: int = _LOG_MAX_BYTES) -> None:
    """Rotates the log to <path>.old once it exceeds max_bytes, so long-lived
    server processes cannot grow the debug logs without bound."""
    try:
        if os.path.exists(path) and os.path.getsize(path) > max_bytes:
            old_path = path + ".old"
            if os.path.exists(old_path):
                os.remove(old_path)
            os.replace(path, old_path)
    except OSError:
        # Best-effort rotation; a locked or vanished log file is tolerable.
        pass


try:
    _mcp_log_dir = os.path.join(PROJECT_ROOT, "scratch")
    os.makedirs(_mcp_log_dir, exist_ok=True)

    _launch_log = os.path.join(_mcp_log_dir, "mcp_launch_debug.log")
    _rotate_log(_launch_log)
    with open(_launch_log, "a", encoding="utf-8") as _f:
        _f.write(
            f"[{datetime.datetime.now()}] Artemis MCP Server launched! sys.argv: {sys.argv}, CWD: {os.getcwd()}\n"
        )

    class StderrTee:
        def __init__(self, original, log_path, max_bytes=_LOG_MAX_BYTES):
            self.original = original
            self.log_path = log_path
            self.max_bytes = max_bytes
            _rotate_log(log_path, max_bytes)
            self.log_file = open(log_path, "a", encoding="utf-8")

        def write(self, data):
            self.original.write(data)
            self.log_file.write(data)
            self.log_file.flush()
            if self.log_file.tell() > self.max_bytes:
                self.log_file.close()
                _rotate_log(self.log_path, self.max_bytes)
                self.log_file = open(self.log_path, "a", encoding="utf-8")

        def flush(self):
            self.original.flush()
            self.log_file.flush()

    sys.stderr = StderrTee(sys.stderr, os.path.join(_mcp_log_dir, "mcp_stderr.log"))
except Exception:
    # Debug logging and the stderr tee are optional diagnostics; they must
    # never prevent the MCP stdio server from starting.
    pass

# 1. Import shared FastMCP instance
from mcp_server.base import mcp

# 2. Import tools to register them
import mcp_server.tools  # noqa: F401
from artemis.runtime import shutdown_awake_service, start_awake_service


import threading

def main(transport: str = "stdio", host: str = "127.0.0.1", port: int = 8001):
    """Main entrypoint to run the Artemis Mobile Agent MCP server."""
    threading.Thread(
        target=start_awake_service, daemon=True, name="artemis-awake-init"
    ).start()
    try:
        if transport.lower() == "sse":
            mcp.run(transport="sse", host=host, port=port)
        else:
            mcp.run(transport="stdio")
    finally:
        shutdown_awake_service()


if __name__ == "__main__":
    main()
