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

import logging
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from typing import Annotated
import urllib.request
import webbrowser

from rich.console import Console
from rich.panel import Panel
import typer

logger = logging.getLogger(__name__)


def running_via_console_script_shim() -> bool:
    """Return True when this CLI was launched through a console-script ``.exe`` shim.

    On Windows, ``uv``/pip install entry points as trampoline executables
    (``Scripts/artemis.exe``) that stay resident for the lifetime of the CLI
    process and hold a lock on their own file. A long-running foreground server
    started through the shim therefore blocks ``uv sync`` from reinstalling the
    package ("os error 32") until the server exits. Launching via
    ``python -m artemis`` avoids the shim entirely.
    """
    if sys.platform != "win32":
        return False
    argv0 = Path(sys.argv[0] or "")
    return argv0.suffix.lower() == ".exe" and argv0.name.lower() != "python.exe"


def warn_if_console_script_shim(console: Console) -> None:
    """Print a hint when a foreground server run will pin the console-script exe."""
    if running_via_console_script_shim():
        console.print(
            "   [dim]⚠ Running in foreground via the [bold]artemis.exe[/bold] launcher: package "
            "syncs (uv sync / uv run) cannot replace it while this server is running.\n"
            "     Prefer [bold]artemis restart[/bold] (background daemon) or launch with "
            "[bold]uv run python -m artemis ui[/bold] to avoid this.[/dim]\n"
        )


def _resolve_npm_executable(platform_name: str | None = None) -> str | None:
    """Return a directly executable npm path for the current platform."""
    platform_name = platform_name or sys.platform
    candidates = ("npm.cmd", "npm.exe", "npm") if platform_name == "win32" else ("npm",)
    return next((path for candidate in candidates if (path := shutil.which(candidate))), None)


def _showcase_build_required(showcase_dir: Path) -> bool:
    """Return whether the compiled Angular app is missing or older than its inputs."""
    base_dist = showcase_dir / "dist"
    candidates = [
        base_dist / "frontend" / "browser" / "index.html",
        base_dist / "browser" / "index.html",
        base_dist / "frontend" / "index.html",
        base_dist / "index.html",
    ]
    built_indexes = [path for path in candidates if path.exists()]
    if not built_indexes:
        return True

    build_time = max(path.stat().st_mtime for path in built_indexes)
    input_paths = [showcase_dir / "package.json", showcase_dir / "angular.json"]
    source_dir = showcase_dir / "src"
    if source_dir.exists():
        input_paths.extend(path for path in source_dir.rglob("*") if path.is_file())
    return any(path.exists() and path.stat().st_mtime > build_time for path in input_paths)


def ensure_showcase_built(console: Console) -> None:
    """Rebuild the Angular Showcase UI when its sources are newer than the dist build."""
    from artemis.config.paths import ROOT_DIR
    from artemis.resources import get_bundled_showcase_dist

    showcase_dir = ROOT_DIR / "apps" / "showcase_ui"
    # An installed wheel has no frontend source tree; its immutable build is
    # prepared during packaging and must never trigger npm at runtime.
    if not showcase_dir.is_dir() and get_bundled_showcase_dist() is not None:
        return
    if not _showcase_build_required(showcase_dir):
        return
    npm_executable = _resolve_npm_executable()
    if not npm_executable:
        return
    console.print(
        "   [yellow]🎨 Showcase UI sources changed. Compiling Angular Showcase UI...[/yellow]"
    )
    try:
        subprocess.run([npm_executable, "install", "--silent"], cwd=showcase_dir, check=True)
        subprocess.run([npm_executable, "run", "build"], cwd=showcase_dir, check=True)
        console.print("   [green]✓ Showcase UI built successfully.[/green]\n")
    except Exception as e:
        console.print(f"   [red]⚠ Failed to auto-build Showcase UI: {e}[/red]\n")


def load_session_reconciler() -> None:
    """Entry-layer assembly: arm stop_server()'s orphan-session cleanup.

    Importing the admin console session repository registers its reconciler
    with ``artemis.runtime.server_lifecycle`` (inverted dependency: the base
    runtime package never imports the application layer). Guarded so a broken
    or absent admin console install can never block stopping a server.
    """
    try:
        import apps.admin_console.database.repositories.session_repository  # noqa: F401
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.debug("Admin console session reconciler not loaded: %s", exc, exc_info=True)


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
        typer.Option(
            "--host",
            "-h",
            help=(
                "Bind host address for the UI server. Defaults to loopback; for remote "
                "access prefer a Tailscale/SSH tunnel over a wide bind."
            ),
        ),
    ] = "127.0.0.1",
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
    restart: Annotated[
        bool,
        typer.Option(
            "--restart",
            "-r",
            help="Restart server by terminating any existing instance on the port first.",
        ),
    ] = False,
) -> None:
    """Launch the unified Artemis Showcase UI & Admin Console in one click."""
    console = Console()
    local_url = f"http://localhost:{port}"

    from artemis.runtime.server_lifecycle import (
        find_server_pids,
        is_port_in_use,
        stop_server,
    )

    if restart:
        console.print(f"   [yellow]🔄 Restart requested. Recycling port {port}...[/yellow]")
        load_session_reconciler()
        stop_server(port=port, timeout=12.0)
    elif is_port_in_use(port):
        pids = find_server_pids(port)
        pid_str = f" (PID: {', '.join(map(str, pids))})" if pids else ""
        if sys.stdin.isatty():
            console.print(
                f"\n   [yellow]⚠ Port {port} is already in use by an active Artemis server{pid_str}.[/yellow]"
            )
            console.print("   [dim]Choose an action:[/dim]")
            console.print("     [bold]1[/bold] Open browser (default)")
            console.print("     [bold]2[/bold] Restart server (stop existing and start here)")
            console.print("     [bold]3[/bold] Cancel")
            try:
                choice = input("   Select [1-3, default 1]: ").strip()
            except (KeyboardInterrupt, EOFError):
                choice = "1"
            if choice == "2":
                console.print(
                    f"\n   [yellow]🔄 Terminating previous Artemis instance on port {port}...[/yellow]"
                )
                load_session_reconciler()
                stop_server(port=port, timeout=12.0)
            elif choice == "3":
                return
            else:
                if open_browser:
                    webbrowser.open(local_url)
                return
        else:
            console.print(
                f"\n   [yellow]⚠ Port {port} is already in use by Artemis{pid_str}. "
                f"Use '[bold]artemis restart[/bold]' to restart.[/yellow]\n"
            )
            if open_browser:
                webbrowser.open(local_url)
            return

    console.print()
    msg = (
        f"[bold cyan]✨ Artemis Autonomous Mobile Agent UI[/bold cyan]\n\n"
        f"📱 [bold green]Showcase UI & Onboarding:[/bold green] [cyan]{local_url}[/cyan]\n"
        f"🛠️ [bold magenta]Admin Debug Console:[/bold magenta]     [cyan]{local_url}/admin[/cyan]\n"
        f"📖 [dim]API Documentation:[/dim]         [dim]{local_url}/docs[/dim]\n\n"
        + (
            "[dim]Press Ctrl+C to stop while idle; Ctrl+Break forces shutdown during a task.[/dim]"
            if sys.platform == "win32"
            else "[dim]Press Ctrl+C to stop the server.[/dim]"
        )
    )
    console.print(Panel(msg, title="🚀 Web Server Active", border_style="cyan", expand=False))
    # Ensure the canonical writable .env exists. Installed wheels must never
    # attempt to mutate their site-packages directory.
    from artemis.config.paths import ROOT_DIR, get_env_file, is_source_checkout

    env_file = get_env_file()
    env_example = ROOT_DIR / ".env.example"
    if not env_file.exists():
        env_file.parent.mkdir(parents=True, exist_ok=True)
        if is_source_checkout() and env_example.exists():
            try:
                env_file.write_text(env_example.read_text(encoding="utf-8"), encoding="utf-8")
            except (OSError, UnicodeError):
                pass
        else:
            try:
                env_file.touch()
            except OSError:
                pass

    ensure_showcase_built(console)
    warn_if_console_script_shim(console)

    stop_event = threading.Event()
    if open_browser:
        t = threading.Thread(
            target=_poll_and_open_browser,
            args=(local_url, stop_event),
            daemon=True,
        )
        t.start()

    try:
        # Intentional entry-layer lazy load: the CLI assembles the application
        # here, and importing the FastAPI server at module import time would
        # drag the whole admin console into every `artemis <cmd>` invocation.
        from apps.admin_console.server import run_ui_server

        run_ui_server(host=host, port=port, reload=reload)
    except KeyboardInterrupt:
        console.print("\n[yellow]🛑 UI Server stopped.[/yellow]")
    finally:
        stop_event.set()
