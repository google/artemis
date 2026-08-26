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

"""CLI commands for Artemis server lifecycle management: restart, stop, and status."""

from __future__ import annotations

import datetime
from typing import Annotated
import webbrowser

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
import typer

from artemis.interfaces.cli.commands.ui import ui_command
from artemis.runtime.daemon_client import spawn_daemon
from artemis.runtime.server_lifecycle import (
    find_server_pids,
    get_server_status,
    stop_server,
)

console = Console()


def _format_uptime(seconds: float | None) -> str:
    """Format seconds into human-friendly duration (e.g. 1h 23m 45s)."""
    if seconds is None:
        return "Unknown"
    sec = int(seconds)
    hours, remainder = divmod(sec, 3600)
    minutes, remainder_sec = divmod(remainder, 60)
    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0 or hours > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{remainder_sec}s")
    return " ".join(parts)


def restart_command(
    host: Annotated[
        str,
        typer.Option("--host", "-H", help="Bind host address for the UI server."),
    ] = "0.0.0.0",
    port: Annotated[
        int,
        typer.Option("--port", "-p", help="Port to run the unified UI server on."),
    ] = 8000,
    open_browser: Annotated[
        bool,
        typer.Option(
            "--open/--no-open",
            help="Automatically open the Showcase UI in default web browser.",
        ),
    ] = True,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            "-f",
            help="Force kill previous server immediately without waiting for graceful exit.",
        ),
    ] = False,
    reload: Annotated[
        bool,
        typer.Option(
            "--reload/--no-reload",
            help="Enable uvicorn live auto-reload (for development).",
        ),
    ] = False,
    daemon: Annotated[
        bool,
        typer.Option(
            "--daemon",
            "-d",
            help="Run the new server as a detached background process instead of foreground.",
        ),
    ] = False,
) -> None:
    """Restart the Artemis Web UI server, terminating any existing instance on the port."""
    console.print()
    console.print(
        Panel(
            f"[bold cyan]🔄 Artemis Server Restart[/bold cyan]\n"
            f"[dim]Inspecting port {port} and recycling any running Artemis instances...[/dim]",
            border_style="cyan",
            expand=False,
        )
    )

    # 1. Stop existing server
    existing_pids = find_server_pids(port)
    if existing_pids:
        pid_str = ", ".join(map(str, existing_pids))
        console.print(f"   [yellow]🛑 Terminating running Artemis instance (PID: {pid_str})...[/yellow]")
    else:
        console.print(f"   [dim]ℹ No active Artemis server found on port {port}.[/dim]")

    success, msg, stopped_pids = stop_server(port=port, force=force, timeout=4.0)
    if stopped_pids:
        console.print(f"   [green]✓ {msg}[/green]\n")
    elif not success:
        console.print(f"   [red]⚠ {msg}[/red]\n")
    else:
        console.print(f"   [green]✓ Port {port} is clear and ready.[/green]\n")

    # 2. Launch fresh server instance
    local_url = f"http://localhost:{port}"
    if daemon:
        console.print(f"   [cyan]🚀 Starting Artemis in detached background daemon mode...[/cyan]")
        proc = spawn_daemon(host=host, port=port)
        if proc:
            console.print(
                f"   [bold green]✓ Artemis server running in background (PID: {proc.pid})[/bold green]\n"
                f"   🌐 [cyan]{local_url}[/cyan]\n"
                f"   🛠️  [cyan]{local_url}/admin[/cyan]\n"
            )
            if open_browser:
                webbrowser.open(local_url)
        else:
            console.print("   [red]❌ Failed to spawn background daemon.[/red]\n")
            raise typer.Exit(code=1)
        return

    # Default foreground mode
    console.print(f"   [cyan]🚀 Launching unified Artemis Showcase UI in current terminal...[/cyan]\n")
    ui_command(host=host, port=port, open_browser=open_browser, reload=reload)


def stop_command(
    port: Annotated[
        int,
        typer.Option("--port", "-p", help="Port of the Artemis server to stop."),
    ] = 8000,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            "-f",
            help="Force kill immediately without waiting for graceful shutdown.",
        ),
    ] = False,
) -> None:
    """Stop any running Artemis Web UI / server instance on the given port."""
    console.print()
    existing_pids = find_server_pids(port)
    if existing_pids:
        pid_str = ", ".join(map(str, existing_pids))
        console.print(f"   [yellow]🛑 Stopping Artemis server on port {port} (PID: {pid_str})...[/yellow]")
    else:
        console.print(f"   [dim]Scanning port {port}...[/dim]")

    success, msg, stopped_pids = stop_server(port=port, force=force, timeout=4.0)
    if stopped_pids:
        console.print(
            Panel(
                f"[bold green]✓ Artemis server stopped successfully.[/bold green]\n\n"
                f"[dim]{msg}[/dim]",
                title="🛑 Server Stopped",
                border_style="green",
                expand=False,
            )
        )
    elif not success:
        console.print(
            Panel(
                f"[bold red]⚠ Failed to fully stop server on port {port}[/bold red]\n\n{msg}",
                title="⚠ Warning",
                border_style="red",
                expand=False,
            )
        )
        raise typer.Exit(code=1)
    else:
        console.print(
            Panel(
                f"[dim]No active Artemis server found on port {port}. Port is free.[/dim]",
                title="ℹ Status",
                border_style="dim",
                expand=False,
            )
        )


def status_command(
    port: Annotated[
        int,
        typer.Option("--port", "-p", help="Port to query for Artemis server status."),
    ] = 8000,
) -> None:
    """Check whether the Artemis server is currently running and display runtime metadata."""
    console.print()
    status = get_server_status(port=port)

    if status["running"]:
        pids_str = ", ".join(map(str, status["pids"])) if status["pids"] else str(status["active_pid"] or "Unknown")
        uptime_str = _format_uptime(status["uptime_seconds"])
        metadata = status.get("metadata") or {}

        started_at_str = "Unknown"
        if metadata.get("started_at"):
            try:
                dt = datetime.datetime.fromtimestamp(metadata["started_at"])
                started_at_str = dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass

        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_row("[bold cyan]Status[/bold cyan]", "[bold green]● ONLINE / RUNNING[/bold green]")
        table.add_row("[bold cyan]PID(s)[/bold cyan]", f"[yellow]{pids_str}[/yellow]")
        table.add_row("[bold cyan]Port[/bold cyan]", f"[white]{port}[/white]")
        table.add_row("[bold cyan]Uptime[/bold cyan]", f"[white]{uptime_str}[/white]")
        table.add_row("[bold cyan]Started At[/bold cyan]", f"[dim]{started_at_str}[/dim]")
        table.add_row("[bold cyan]Showcase UI[/bold cyan]", f"[cyan]{status['url']}[/cyan]")
        table.add_row("[bold cyan]Admin Debug[/bold cyan]", f"[magenta]{status['admin_url']}[/magenta]")

        if metadata.get("cwd"):
            table.add_row("[bold cyan]Workspace[/bold cyan]", f"[dim]{metadata['cwd']}[/dim]")

        console.print(
            Panel(
                table,
                title="🚀 Artemis Server Status",
                border_style="green",
                expand=False,
            )
        )
        console.print("   [dim]💡 To restart: [bold]artemis restart[/bold] | To stop: [bold]artemis stop[/bold][/dim]\n")
    else:
        console.print(
            Panel(
                f"[dim]○ OFFLINE / STOPPED[/dim]\n\n"
                f"Port [cyan]{port}[/cyan] is currently free.\n"
                f"[dim]No active Artemis server process detected.[/dim]",
                title="ℹ Artemis Server Status",
                border_style="dim",
                expand=False,
            )
        )
        console.print("   [dim]💡 To start: [bold]artemis ui[/bold] or [bold]./start.sh[/bold][/dim]\n")
