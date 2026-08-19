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
import os
from pathlib import Path
import sys
from typing import Annotated

from mcp_server.base import mcp as agent_mcp
import mcp_server.tools  # noqa: F401
from mcp_server.utils import env_utils
from artemis.mcp.adb_server import mcp as adb_mcp
from artemis.utils.logger import get_logger
from rich.console import Console
from rich.syntax import Syntax
import typer

logger = get_logger(__name__)
console = Console()


def _get_vscode_user_dir() -> Path:
    """Returns the platform-specific VS Code user configuration directory."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Code" / "User"
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(appdata) / "Code" / "User"
    else:
        return Path.home() / ".config" / "Code" / "User"


def _get_config_snippet(client: str, python_exe: str, project_root: str) -> dict:
    """Generates MCP configuration dictionary for the specified client."""
    config_body = {
        "command": python_exe,
        "args": ["-m", "mcp_server"],
        "cwd": project_root,
        "env": {
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": project_root,
        },
    }

    if client in ("antigravity", "jetski"):
        return {
            "mcpServers": {
                "artemis": {
                    **config_body,
                    "tools": {
                        "mobile_run_task": {"eager": True},
                        "mobile_manage_task": {"eager": True},
                        "mobile_get_device_state": {"eager": True},
                        "mobile_inspect_trace": {"eager": True},
                    },
                }
            }
        }
    elif client == "openclaw":
        return {"plugins": {"artemis_mcp": {"enabled": True, "type": "mcp", "server": config_body}}}
    else:  # cursor, windsurf, claude, vscode, cline, roo, generic
        return {"mcpServers": {"artemis": config_body}}


def _parse_json_lenient(text: str) -> dict:
    """Parses JSON or JSONC (JSON with comments / trailing commas) safely."""
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        pass
    import re
    # Remove // single-line comments
    cleaned = re.sub(r"//.*", "", text)
    # Remove /* ... */ multi-line comments
    cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)
    # Remove trailing commas before } or ]
    cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
    try:
        data = json.loads(cleaned)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _merge_json_file(file_path: Path, server_name: str, server_config: dict, key_name: str = "mcpServers") -> bool:
    """Merges server configuration into a target JSON file without overwriting existing servers."""
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        data: dict = {}
        if file_path.exists() and file_path.stat().st_size > 0:
            raw_text = file_path.read_text(encoding="utf-8")
            data = _parse_json_lenient(raw_text)
            if not data and raw_text.strip():
                # Backup unparseable existing file to prevent accidental loss
                backup_path = file_path.with_suffix(file_path.suffix + ".bak")
                try:
                    backup_path.write_text(raw_text, encoding="utf-8")
                    logger.warning(f"Existing JSON in {file_path} could not be parsed; backup created at {backup_path}")
                except Exception:
                    pass

        if key_name not in data or not isinstance(data.get(key_name), dict):
            data[key_name] = {}

        data[key_name][server_name] = server_config
        file_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return True
    except Exception as e:
        logger.warning(f"Could not update MCP config file {file_path}: {e}")
        return False


def _inject_rules_block(file_path: Path, content: str) -> bool:
    """Injects or updates a managed ARTEMIS block in a global rules file without overwriting user content."""
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        begin_marker = "<!-- BEGIN ARTEMIS MOBILE TESTING RULES -->"
        end_marker = "<!-- END ARTEMIS MOBILE TESTING RULES -->"
        new_block = f"{begin_marker}\n{content.strip()}\n{end_marker}\n"

        existing = ""
        if file_path.exists():
            try:
                existing = file_path.read_text(encoding="utf-8")
            except Exception:
                existing = ""

        if begin_marker in existing and end_marker in existing:
            prefix = existing.split(begin_marker)[0]
            suffix = existing.split(end_marker)[1]
            updated = f"{prefix.rstrip()}\n\n{new_block}\n{suffix.lstrip()}".strip() + "\n"
        else:
            updated = f"{existing.rstrip()}\n\n{new_block}".strip() + "\n"

        if existing == updated:
            return True

        file_path.write_text(updated, encoding="utf-8")
        return True
    except Exception as e:
        logger.warning(f"Could not inject rules block into {file_path}: {e}")
        return False


def _write_rule_file(file_path: Path, content: str) -> bool:
    """Writes or overwrites a standalone rule file."""
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        new_text = content.strip() + "\n"
        if file_path.exists():
            try:
                if file_path.read_text(encoding="utf-8") == new_text:
                    return True
            except Exception:
                pass
        file_path.write_text(new_text, encoding="utf-8")
        return True
    except Exception as e:
        logger.warning(f"Could not write rule file {file_path}: {e}")
        return False


def _write_cursor_mdc(file_path: Path, content: str) -> bool:
    """Writes a Cursor .mdc rule file with required YAML frontmatter."""
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        mdc_header = (
            "---\n"
            "description: Artemis Mobile Testing Mindset & Rules\n"
            "globs: **/*\n"
            "alwaysApply: true\n"
            "---\n\n"
        )
        new_text = mdc_header + content.strip() + "\n"
        if file_path.exists():
            try:
                if file_path.read_text(encoding="utf-8") == new_text:
                    return True
            except Exception:
                pass
        file_path.write_text(new_text, encoding="utf-8")
        return True
    except Exception as e:
        logger.warning(f"Could not write Cursor rule file {file_path}: {e}")
        return False


def install_rules(client: str, project_root: str) -> list[str]:
    """Auto-installs ARTEMIS testing rules globally into user IDE rule directories across any OS."""
    installed_paths: list[str] = []
    rules_src = Path(project_root) / "mcp_server" / "rules.md"
    if not rules_src.exists():
        alt_src = Path(__file__).resolve().parents[4] / "mcp_server" / "rules.md"
        if alt_src.exists():
            rules_src = alt_src
        else:
            logger.warning(f"Could not locate rules.md at {rules_src}")
            return []

    try:
        raw_rules = rules_src.read_text(encoding="utf-8").strip()
    except Exception as e:
        logger.warning(f"Could not read rules file {rules_src}: {e}")
        return []

    targets = (
        ["antigravity", "cursor", "claude", "windsurf", "vscode", "cline", "roo", "openclaw"]
        if client == "all"
        else [client]
    )

    for target in targets:
        if target in ("antigravity", "jetski"):
            gemini_md = Path.home() / ".gemini" / "GEMINI.md"
            global_rule = Path.home() / ".gemini" / "rules" / "artemis.md"
            if _inject_rules_block(gemini_md, raw_rules):
                installed_paths.append(str(gemini_md))
            if _write_rule_file(global_rule, raw_rules):
                installed_paths.append(str(global_rule))
        elif target == "cursor":
            cursor_rules_file = Path.home() / ".cursorrules"
            global_mdc = Path.home() / ".cursor" / "rules" / "artemis.mdc"
            if _inject_rules_block(cursor_rules_file, raw_rules):
                installed_paths.append(str(cursor_rules_file))
            if _write_cursor_mdc(global_mdc, raw_rules):
                installed_paths.append(str(global_mdc))
        elif target in ("claude", "claude_code", "claude_desktop"):
            claude_md = Path.home() / ".claude" / "CLAUDE.md"
            global_rule = Path.home() / ".claude" / "rules" / "artemis.md"
            if _inject_rules_block(claude_md, raw_rules):
                installed_paths.append(str(claude_md))
            if _write_rule_file(global_rule, raw_rules):
                installed_paths.append(str(global_rule))
        elif target == "windsurf":
            global_rule = Path.home() / ".codeium" / "windsurf" / "rules" / "artemis.md"
            global_mem = Path.home() / ".codeium" / "windsurf" / "memories" / "global_rules.md"
            if _inject_rules_block(global_mem, raw_rules):
                installed_paths.append(str(global_mem))
            if _write_rule_file(global_rule, raw_rules):
                installed_paths.append(str(global_rule))
        elif target == "vscode":
            global_rule = Path.home() / ".vscode" / "rules" / "artemis.md"
            user_rule = _get_vscode_user_dir() / "rules" / "artemis.md"
            if _write_rule_file(global_rule, raw_rules):
                installed_paths.append(str(global_rule))
            if _write_rule_file(user_rule, raw_rules):
                installed_paths.append(str(user_rule))
        elif target == "cline":
            clinerules = Path.home() / ".clinerules"
            global_rule = Path.home() / ".cline" / "rules" / "artemis.md"
            if _inject_rules_block(clinerules, raw_rules):
                installed_paths.append(str(clinerules))
            if _write_rule_file(global_rule, raw_rules):
                installed_paths.append(str(global_rule))
        elif target in ("roo", "roo_code"):
            roorules = Path.home() / ".roorules"
            global_rule = Path.home() / ".roo" / "rules" / "artemis.md"
            if _inject_rules_block(roorules, raw_rules):
                installed_paths.append(str(roorules))
            if _write_rule_file(global_rule, raw_rules):
                installed_paths.append(str(global_rule))
        elif target == "openclaw":
            openclaw_md = Path.home() / ".openclaw" / "OPENCLAW.md"
            global_rule = Path.home() / ".openclaw" / "rules" / "artemis.md"
            if _inject_rules_block(openclaw_md, raw_rules):
                installed_paths.append(str(openclaw_md))
            if _write_rule_file(global_rule, raw_rules):
                installed_paths.append(str(global_rule))

    return installed_paths


def install_mcp_config(client: str, python_exe: str, project_root: str) -> list[str]:
    """Auto-installs/merges ARTEMIS MCP configuration and testing rules into IDE config files across any OS."""
    installed_paths: list[str] = []
    targets = (
        ["antigravity", "cursor", "claude", "windsurf", "vscode", "cline", "roo", "openclaw"]
        if client == "all"
        else [client]
    )

    for target in targets:
        snippet = _get_config_snippet(target, python_exe, project_root)
        if target in ("antigravity", "jetski"):
            server_cfg = snippet["mcpServers"]["artemis"]
            jetski_path = Path.home() / ".gemini" / "jetski" / "mcp_config.json"
            config_path = Path.home() / ".gemini" / "config" / "mcp_config.json"
            if _merge_json_file(jetski_path, "artemis", server_cfg):
                installed_paths.append(str(jetski_path))
            if _merge_json_file(config_path, "artemis", server_cfg):
                installed_paths.append(str(config_path))
        elif target in ("claude", "claude_code", "claude_desktop"):
            server_cfg = snippet["mcpServers"]["artemis"]
            if sys.platform == "darwin":
                claude_path = Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
            elif sys.platform == "win32":
                appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
                claude_path = Path(appdata) / "Claude" / "claude_desktop_config.json"
            else:
                claude_path = Path.home() / ".config" / "Claude" / "claude_desktop_config.json"
            if _merge_json_file(claude_path, "artemis", server_cfg):
                installed_paths.append(str(claude_path))

            # Also install to Claude Code CLI global config (~/.claude.json)
            claude_code_path = Path.home() / ".claude.json"
            if _merge_json_file(claude_code_path, "artemis", server_cfg):
                installed_paths.append(str(claude_code_path))
        elif target == "cursor":
            server_cfg = snippet["mcpServers"]["artemis"]
            cursor_path = Path.home() / ".cursor" / "mcp.json"
            if _merge_json_file(cursor_path, "artemis", server_cfg):
                installed_paths.append(str(cursor_path))
        elif target == "windsurf":
            server_cfg = snippet["mcpServers"]["artemis"]
            windsurf_path = Path.home() / ".codeium" / "windsurf" / "mcp_config.json"
            if _merge_json_file(windsurf_path, "artemis", server_cfg):
                installed_paths.append(str(windsurf_path))
        elif target == "vscode":
            server_cfg = snippet["mcpServers"]["artemis"]
            vscode_path = _get_vscode_user_dir() / "mcp.json"
            if _merge_json_file(vscode_path, "artemis", server_cfg):
                installed_paths.append(str(vscode_path))
        elif target == "cline":
            server_cfg = snippet["mcpServers"]["artemis"]
            cline_path = _get_vscode_user_dir() / "globalStorage" / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json"
            if _merge_json_file(cline_path, "artemis", server_cfg):
                installed_paths.append(str(cline_path))
        elif target in ("roo", "roo_code"):
            server_cfg = snippet["mcpServers"]["artemis"]
            roo_path = _get_vscode_user_dir() / "globalStorage" / "rooveterinaryinc.roo-cline" / "settings" / "cline_mcp_settings.json"
            if _merge_json_file(roo_path, "artemis", server_cfg):
                installed_paths.append(str(roo_path))
        elif target == "openclaw":
            plugin_cfg = snippet["plugins"]["artemis_mcp"]
            openclaw_path = Path.home() / ".openclaw" / "openclaw.json"
            if _merge_json_file(openclaw_path, "artemis_mcp", plugin_cfg, key_name="plugins"):
                installed_paths.append(str(openclaw_path))

    rules_paths = install_rules(client, project_root)
    installed_paths.extend(rules_paths)

    unique_paths: list[str] = []
    for p in installed_paths:
        if p not in unique_paths:
            unique_paths.append(p)
    return unique_paths


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
    install_config: Annotated[
        str | None,
        typer.Option(
            "--install",
            "-i",
            help="Auto-install and merge ARTEMIS MCP configuration and testing rules into 'antigravity', 'claude', 'cursor', 'windsurf', 'vscode', 'cline', 'roo', 'openclaw', or 'all'.",
        ),
    ] = None,
    generate_config: Annotated[
        str | None,
        typer.Option(
            "--generate-config",
            "-g",
            help="Output ready-to-use MCP configuration JSON for 'antigravity', 'cursor', 'claude', 'windsurf', 'vscode', 'cline', 'roo', 'openclaw', or 'all'.",
        ),
    ] = None,
) -> None:
    """Launch or configure the ARTEMIS Model Context Protocol (MCP) server."""
    project_root = env_utils.get_project_root()
    python_exe = env_utils.resolve_python_executable(project_root)

    if install_config:
        client = install_config.lower()
        if client not in (
            "antigravity",
            "jetski",
            "claude",
            "claude_code",
            "claude_desktop",
            "cursor",
            "windsurf",
            "vscode",
            "cline",
            "roo",
            "roo_code",
            "openclaw",
            "all",
        ):
            console.print(
                f"[bold red]Unsupported install target: '{install_config}'. Use 'antigravity', 'claude', 'cursor', 'windsurf', 'vscode', 'cline', 'roo', 'openclaw', or 'all'.[/bold red]"
            )
            raise typer.Exit(1)
        installed_paths = install_mcp_config(client, python_exe, project_root)
        console.print("[bold green]✔ Successfully installed ARTEMIS MCP server configuration & rules to:[/bold green]")
        for path in installed_paths:
            console.print(f"  • [cyan]{path}[/cyan]")
        console.print("\n[dim]Please restart or reload your IDE window to activate the Artemis MCP tools.[/dim]")
        raise typer.Exit(0)

    if generate_config:
        client = generate_config.lower()
        if client == "all":
            all_configs = {
                "antigravity (~/.gemini/jetski/mcp_config.json)": _get_config_snippet(
                    "antigravity", python_exe, project_root
                ),
                "cursor (.cursor/mcp.json)": _get_config_snippet(
                    "cursor", python_exe, project_root
                ),
                "claude (claude_desktop_config.json & ~/.claude.json)": _get_config_snippet(
                    "claude", python_exe, project_root
                ),
                "windsurf (~/.codeium/windsurf/mcp_config.json)": _get_config_snippet(
                    "windsurf", python_exe, project_root
                ),
                "vscode (mcp.json)": _get_config_snippet(
                    "vscode", python_exe, project_root
                ),
                "cline (cline_mcp_settings.json)": _get_config_snippet(
                    "cline", python_exe, project_root
                ),
                "roo (cline_mcp_settings.json)": _get_config_snippet(
                    "roo", python_exe, project_root
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
