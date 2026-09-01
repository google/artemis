#!/usr/bin/env python3
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

"""Artemis UI Quick Launcher (run_ui.py)

Fastest and simplest way to launch the Artemis Web UI across all platforms:
- Automatically detects if the UI is already running and opens the browser directly.
- Uses uv or existing virtualenv seamlessly.
- Zero-configuration one-click start.
"""

import argparse
import os
from pathlib import Path
import socket
import subprocess
import sys
import urllib.request
import webbrowser

ROOT_DIR = Path(__file__).resolve().parent.parent


def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """Check whether a TCP port is in use."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def is_artemis_ui_responsive(url: str, timeout: float = 1.0) -> bool:
    """Check if Artemis UI server is responding on given URL."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Artemis-QuickLauncher/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status in (200, 304)
    except Exception:
        return False


def find_python_executable() -> str:
    """Find the best Python executable (.venv or system)."""
    # 1. Check local .venv
    venv_py = (
        ROOT_DIR
        / ".venv"
        / ("Scripts" if os.name == "nt" else "bin")
        / ("python.exe" if os.name == "nt" else "python")
    )
    if venv_py.is_file() and os.access(venv_py, os.X_OK if os.name != "nt" else os.F_OK):
        return str(venv_py)

    # 2. Return current sys.executable
    return sys.executable


def main() -> None:
    parser = argparse.ArgumentParser(
        description="✨ One-click launcher for Artemis Mobile Agent UI",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("-p", "--port", type=int, default=8000, help="Port to run UI on")
    parser.add_argument(
        "-H",
        "--host",
        type=str,
        default="127.0.0.1",
        help="Host address to bind (loopback by default; use a tunnel for remote access)",
    )
    parser.add_argument(
        "-r",
        "--restart",
        action="store_true",
        help="Restart the server by terminating any existing instance on the port first",
    )
    parser.add_argument(
        "-s",
        "--stop",
        action="store_true",
        help="Stop running Artemis server on the specified port",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Display status of running Artemis server on the specified port",
    )
    args, unknown = parser.parse_known_args()

    # Ensure project root is on sys.path for lifecycle utility imports
    if str(ROOT_DIR) not in sys.path:
        sys.path.insert(0, str(ROOT_DIR))

    from artemis.runtime.server_lifecycle import get_server_status, stop_server

    def _arm_session_reconciler() -> None:
        # Entry-layer assembly: importing the admin console session repository
        # registers its orphan-session reconciler with the runtime, so a forced
        # stop below can mark sessions whose worker processes were killed.
        try:
            import apps.admin_console.database.repositories.session_repository  # noqa: F401
        except Exception:
            pass

    if args.status:
        st = get_server_status(args.port)
        if st["running"]:
            pid_info = f" (PID: {', '.join(map(str, st['pids']))})" if st["pids"] else ""
            print(f"\033[1;32m● Artemis Server is RUNNING on port {args.port}{pid_info}\033[0m")
            print(f"  URL: {st['url']}")
        else:
            print(f"\033[1;30m○ Artemis Server is STOPPED (Port {args.port} is free)\033[0m")
        return

    if args.stop:
        print(f"\033[1;33m🛑 Stopping Artemis server on port {args.port}...\033[0m")
        _arm_session_reconciler()
        ok, msg, pids = stop_server(args.port)
        print(f"\033[1;32m✓ {msg}\033[0m")
        return

    ui_url = f"http://localhost:{args.port}"
    admin_url = f"http://localhost:{args.port}/admin"

    print("\033[1;36m" + "=" * 56 + "\033[0m")
    print("\033[1;36m      ✨ Artemis Autonomous Mobile Agent UI          \033[0m")
    print("\033[1;36m" + "=" * 56 + "\033[0m\n")

    # Check if restart requested
    if args.restart:
        print(f"\033[1;33m🔄 Restart requested. Recycling port {args.port}...\033[0m")
        _arm_session_reconciler()
        ok, msg, pids = stop_server(args.port)
        if pids:
            print(f"\033[1;32m✓ {msg}\033[0m\n")
        else:
            print(f"Port {args.port} is clear.\n")

    # Check if server is already running
    elif is_port_in_use(args.port):
        if is_artemis_ui_responsive(ui_url):
            print(f"\033[1;32m✓ Artemis UI is already running at:\033[0m \033[1;36m{ui_url}\033[0m")
            print(f"\033[1;35m🛠️ Admin Console:\033[0m \033[1;36m{admin_url}\033[0m\n")

            # In interactive terminal, allow user to restart or open browser
            if sys.stdin.isatty():
                print("Server is active in another session/terminal.")
                print("  [1] Open browser (default)")
                print("  [2] Restart server (stop existing and start here)")
                print("  [3] Stop server")
                try:
                    choice = input("Select [1-3, default 1]: ").strip()
                except (KeyboardInterrupt, EOFError):
                    choice = "1"

                if choice == "2":
                    print(f"\n\033[1;33m🔄 Restarting Artemis server on port {args.port}...\033[0m")
                    _arm_session_reconciler()
                    stop_server(args.port)
                elif choice == "3":
                    print(f"\n\033[1;33m🛑 Stopping Artemis server on port {args.port}...\033[0m")
                    _arm_session_reconciler()
                    ok, msg, _ = stop_server(args.port)
                    print(f"\033[1;32m✓ {msg}\033[0m")
                    return
                else:
                    if not args.no_open:
                        print("🌐 Opening browser...")
                        webbrowser.open(ui_url)
                    return
            else:
                if not args.no_open:
                    print("🌐 Opening browser...")
                    webbrowser.open(ui_url)
                return
        else:
            print(f"\033[1;33m⚠️ Port {args.port} is in use by another process.\033[0m")
            print("Attempting to connect or start on specified port...")

    # Choose execution engine: uv run artemis ui > direct python
    has_uv = (
        subprocess.run(
            ["which", "uv"] if os.name != "nt" else ["where", "uv"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )

    cmd = []
    if has_uv and (ROOT_DIR / "pyproject.toml").exists():
        # Launch via `python -m artemis` (not the `artemis` console-script shim) so the
        # long-running server never locks .venv/Scripts/artemis.exe against reinstalls.
        cmd = ["uv", "run", "python", "-m", "artemis", "ui", "--port", str(args.port), "--host", args.host]
        if args.no_open:
            cmd.append("--no-open")
        if args.reload:
            cmd.append("--reload")
    else:
        python_bin = find_python_executable()
        # Set PYTHONPATH so apps and artemis are importable
        env = os.environ.copy()
        env["PYTHONPATH"] = (
            f"{ROOT_DIR}{os.pathsep}{ROOT_DIR / 'apps'}{os.pathsep}{env.get('PYTHONPATH', '')}"
        )
        cmd = [python_bin, "-m", "artemis", "ui", "--port", str(args.port), "--host", args.host]
        if args.no_open:
            cmd.append("--no-open")
        if args.reload:
            cmd.append("--reload")

    try:
        # Run the server
        subprocess.run(cmd, cwd=str(ROOT_DIR))
    except KeyboardInterrupt:
        print("\n\033[1;33m🛑 Artemis UI server stopped.\033[0m")


if __name__ == "__main__":
    main()
