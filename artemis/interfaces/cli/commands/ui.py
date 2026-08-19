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

"""UI Launch and Web Console CLI Command (artemis ui)."""

import threading
import time
from typing import Annotated
import urllib.request
import webbrowser

from rich.console import Console
from rich.panel import Panel
import typer


def _poll_and_open_browser(url: str, stop_event: threading.Event, timeout: float = 15.0) -> None:
    """Poll the UI server until it responds, then launch the user's default browser."""
    start = time.time()
    while not stop_event.is_set() and (time.time() - start < timeout):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Artemis-HealthCheck/1.0"})
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                if resp.status == 200:
                    time.sleep(0.3)
                    webbrowser.open(url)
                    return
        except Exception:
            time.sleep(0.4)


def ui_command(
    host: Annotated[
        str,
        typer.Option("--host", "-h", help="Bind host address for the UI server."),
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
    reload: Annotated[
        bool,
        typer.Option(
            "--reload/--no-reload",
            help="Enable uvicorn live auto-reload (for development).",
        ),
    ] = False,
) -> None:
    """Launch the unified Artemis Showcase UI & Admin Console in one click."""
    console = Console()
    local_url = f"http://localhost:{port}"

    console.print()
    msg = (
        f"[bold cyan]✨ Artemis Autonomous Mobile Agent UI[/bold cyan]\n\n"
        f"📱 [bold green]Showcase UI & Onboarding:[/bold green] [cyan]{local_url}[/cyan]\n"
        f"🛠️ [bold magenta]Admin Debug Console:[/bold magenta]     [cyan]{local_url}/admin[/cyan]\n"
        f"📖 [dim]API Documentation:[/dim]         [dim]{local_url}/docs[/dim]\n\n"
        f"[dim]Press Ctrl+C to stop the server.[/dim]"
    )
    console.print(Panel(msg, title="🚀 Web Server Active", border_style="cyan", expand=False))
    # Ensure .env exists in workspace root
    from artemis.config.paths import ROOT_DIR

    env_file = ROOT_DIR / ".env"
    env_example = ROOT_DIR / ".env.example"
    if not env_file.exists():
        if env_example.exists():
            try:
                env_file.write_text(env_example.read_text(encoding="utf-8"), encoding="utf-8")
            except Exception:
                pass
        else:
            try:
                env_file.touch()
            except Exception:
                pass

    base_dist = ROOT_DIR / "apps" / "showcase_ui" / "dist"
    candidates = [
        base_dist / "frontend" / "browser" / "index.html",
        base_dist / "browser" / "index.html",
        base_dist / "frontend" / "index.html",
        base_dist / "index.html",
    ]
    if not any(p.exists() for p in candidates):
        import shutil
        import subprocess

        if shutil.which("npm"):
            console.print(
                "   [yellow]🎨 Showcase UI build not found. Compiling Angular Showcase UI...[/yellow]"
            )
            showcase_dir = ROOT_DIR / "apps" / "showcase_ui"
            try:
                subprocess.run(["npm", "install", "--silent"], cwd=showcase_dir, check=True)
                subprocess.run(["npm", "run", "build"], cwd=showcase_dir, check=True)
                console.print("   [green]✓ Showcase UI built successfully.[/green]\n")
            except Exception as e:
                console.print(f"   [red]⚠ Failed to auto-build Showcase UI: {e}[/red]\n")

    stop_event = threading.Event()
    if open_browser:
        t = threading.Thread(
            target=_poll_and_open_browser,
            args=(local_url, stop_event),
            daemon=True,
        )
        t.start()

    try:
        import uvicorn
        from apps.admin_console.server import app

        uvicorn.run(app, host=host, port=port, reload=reload)
    except KeyboardInterrupt:
        console.print("\n[yellow]🛑 UI Server stopped.[/yellow]")
    finally:
        stop_event.set()
