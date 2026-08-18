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

import json
from typing import Annotated

from mcp_server.base import mcp as agent_mcp
from mcp_server.utils import env_utils
from artemis.mcp.adb_server import mcp as adb_mcp
from artemis.utils.logger import get_logger
from rich.console import Console
from rich.syntax import Syntax
import typer

logger = get_logger(__name__)
console = Console()


def _get_config_snippet(client: str, python_exe: str, project_root: str) -> dict:
    """Generates MCP configuration dictionary for the specified client."""
    config_body = {
        "command": python_exe,
        "args": ["-m", "mcp_server"],
        "cwd": project_root,
        "env": {
            "PYTHONUNBUFFERED": "1",
        },
    }

    if client == "cursor":
        return {"mcpServers": {"artemis": config_body}}
    elif client in ("claude", "claude_code", "claude_desktop"):
        return {"mcpServers": {"artemis": config_body}}
    elif client == "openclaw":
        return {"plugins": {"artemis_mcp": {"enabled": True, "type": "mcp", "server": config_body}}}
    else:  # generic
        return {"mcpServers": {"artemis": config_body}}


def mcp_command(
    server_type: Annotated[
        str,
        typer.Option(
            "--type",
            "-t",
            help="Type of MCP server to start: 'agent' (default, universal IDE mobile agent), 'adb' (raw adb), 'xml' (xml fuzzy search).",
        ),
    ] = "agent",
    transport: Annotated[
        str,
        typer.Option(
            "--transport",
            help="MCP transport protocol ('stdio' or 'sse').",
        ),
    ] = "stdio",
    host: Annotated[
        str,
        typer.Option(
            "--host",
            help="Host address when running with SSE transport.",
        ),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option(
            "--port",
            "-p",
            help="Port number when running with SSE transport.",
        ),
    ] = 8001,
    generate_config: Annotated[
        str | None,
        typer.Option(
            "--generate-config",
            "-g",
            help="Output ready-to-use MCP configuration JSON for 'cursor', 'claude', 'openclaw', or 'all'.",
        ),
    ] = None,
) -> None:
    """Launch or configure the ARTEMIS Model Context Protocol (MCP) server."""
    project_root = env_utils.get_project_root()
    python_exe = env_utils.resolve_python_executable(project_root)

    if generate_config:
        client = generate_config.lower()
        if client == "all":
            all_configs = {
                "cursor (.cursor/mcp.json)": _get_config_snippet(
                    "cursor", python_exe, project_root
                ),
                "claude (claude_desktop_config.json)": _get_config_snippet(
                    "claude", python_exe, project_root
                ),
                "openclaw (openclaw.json)": _get_config_snippet(
                    "openclaw", python_exe, project_root
                ),
            }
            json_str = json.dumps(all_configs, indent=2)
        else:
            snippet = _get_config_snippet(client, python_exe, project_root)
            json_str = json.dumps(snippet, indent=2)

        syntax = Syntax(json_str, "json", theme="monokai", line_numbers=False)
        console.print(f"[bold cyan]MCP Configuration for {client.upper()}:[/bold cyan]")
        console.print(syntax)
        raise typer.Exit(0)

    st = server_type.lower()
    if st in ("agent", "mobile", "artemis", "default"):
        logger.info(f"Starting Artemis Mobile Agent MCP Server over {transport}...")
        if transport.lower() == "sse":
            agent_mcp.run(transport="sse", host=host, port=port)
        else:
            agent_mcp.run(transport="stdio")
    elif st == "adb":
        logger.info(f"Starting Artemis ADB MCP Server over {transport}...")
        if transport.lower() == "sse":
            adb_mcp.run(transport="sse", host=host, port=port)
        else:
            adb_mcp.run(transport="stdio")
    elif st == "xml":
        from artemis.mcp.xml_search_server import mcp as xml_mcp

        logger.info(f"Starting Artemis XML Fuzzy Search MCP Server over {transport}...")
        if transport.lower() == "sse":
            xml_mcp.run(transport="sse", host=host, port=port)
        else:
            xml_mcp.run(transport="stdio")
    else:
        logger.error(f"Unsupported MCP server type: {server_type}. Use 'agent', 'adb', or 'xml'.")
        raise typer.Exit(1)
