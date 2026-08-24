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
try:
    _mcp_log_dir = os.path.join(PROJECT_ROOT, "scratch")
    os.makedirs(_mcp_log_dir, exist_ok=True)

    with open(os.path.join(_mcp_log_dir, "mcp_launch_debug.log"), "a", encoding="utf-8") as _f:
        _f.write(
            f"[{datetime.datetime.now()}] Artemis MCP Server launched! sys.argv: {sys.argv}, CWD: {os.getcwd()}\n"
        )

    class StderrTee:
        def __init__(self, original, log_path):
            self.original = original
            self.log_file = open(log_path, "a", encoding="utf-8")

        def write(self, data):
            self.original.write(data)
            self.log_file.write(data)
            self.log_file.flush()

        def flush(self):
            self.original.flush()
            self.log_file.flush()

    sys.stderr = StderrTee(sys.stderr, os.path.join(_mcp_log_dir, "mcp_stderr.log"))
except Exception:
    pass

# 1. Import shared FastMCP instance
from mcp_server.base import mcp

# 2. Import tools to register them
import mcp_server.tools  # noqa: F401
from artemis.runtime import shutdown_awake_service, start_awake_service


def main(transport: str = "stdio", host: str = "127.0.0.1", port: int = 8001):
    """Main entrypoint to run the Artemis Mobile Agent MCP server."""
    start_awake_service()
    try:
        if transport.lower() == "sse":
            mcp.run(transport="sse", host=host, port=port)
        else:
            mcp.run(transport="stdio")
    finally:
        shutdown_awake_service()


if __name__ == "__main__":
    main()
