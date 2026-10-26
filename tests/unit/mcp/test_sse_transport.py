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

"""SSE startup must bind the requested address on the pinned mcp release."""

from unittest.mock import patch

from mcp.server.fastmcp import FastMCP

from artemis.mcp.transport import run_sse


def test_run_sse_binds_the_requested_host_and_port():
    server = FastMCP("sse-test")
    bound: list[tuple[str, int]] = []

    async def fake_run_sse_async(mount_path=None):
        bound.append((server.settings.host, server.settings.port))

    with patch.object(server, "run_sse_async", fake_run_sse_async):
        run_sse(server, "0.0.0.0", 8123)

    assert bound == [("0.0.0.0", 8123)]


def test_mcp_server_main_starts_sse_without_a_type_error():
    import mcp_server.server as server_module

    with (
        patch.object(server_module, "run_sse") as run_sse_mock,
        patch.object(server_module, "start_awake_service"),
        patch.object(server_module, "shutdown_awake_service"),
    ):
        server_module.main(transport="sse", host="0.0.0.0", port=8124)

    run_sse_mock.assert_called_once_with(server_module.mcp, "0.0.0.0", 8124)
