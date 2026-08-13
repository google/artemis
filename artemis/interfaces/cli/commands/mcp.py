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

"""Model Context Protocol (MCP) server command (artemis mcp)."""

from typing import Annotated

from artemis.mcp.adb_server import mcp as adb_mcp
from artemis.utils.logger import get_logger
import typer

logger = get_logger(__name__)


def mcp_command(
    server_type: Annotated[
        str,
        typer.Option(
            "--type",
            "-t",
            help="Type of MCP server to start ('adb' or 'xml').",
        ),
    ] = "adb",
) -> None:
    """Launch a ARTEMIS Model Context Protocol (MCP) server."""
    if server_type.lower() == "adb":
        logger.info("Starting Artemis ADB MCP Server over stdio...")
        adb_mcp.run(transport="stdio")
    else:
        logger.error(f"Unsupported MCP server type: {server_type}")
        raise typer.Exit(1)
