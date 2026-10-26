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

"""Transport helpers shared by every Artemis FastMCP server."""

from mcp.server.fastmcp import FastMCP


def run_sse(server: FastMCP, host: str, port: int) -> None:
    """Serve ``server`` over SSE on ``host:port``.

    ``FastMCP.run()`` stopped accepting ``host``/``port`` in mcp 1.29; the SSE
    app reads the bind address from ``server.settings`` instead.
    """
    server.settings.host = host
    server.settings.port = port
    server.run(transport="sse")
