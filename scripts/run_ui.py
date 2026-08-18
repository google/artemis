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
    parser.add_argument("-H", "--host", type=str, default="0.0.0.0", help="Host address to bind")
    parser.add_argument("--no-open", action="store_true", help="Do not automatically open browser")
    parser.add_argument(
        "--reload", action="store_true", help="Enable uvicorn auto-reload for development"
    )
    args, unknown = parser.parse_known_args()

    ui_url = f"http://localhost:{args.port}"
    admin_url = f"http://localhost:{args.port}/admin"

    print("\033[1;36m" + "=" * 56 + "\033[0m")
    print("\033[1;36m      ✨ Artemis Autonomous Mobile Agent UI          \033[0m")
    print("\033[1;36m" + "=" * 56 + "\033[0m\n")

    # Check if server is already running
    if is_port_in_use(args.port):
        if is_artemis_ui_responsive(ui_url):
            print(f"\033[1;32m✓ Artemis UI is already running at:\033[0m \033[1;36m{ui_url}\033[0m")
            print(f"\033[1;35m🛠️ Admin Console:\033[0m \033[1;36m{admin_url}\033[0m\n")
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
        cmd = ["uv", "run", "artemis", "ui", "--port", str(args.port), "--host", args.host]
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
