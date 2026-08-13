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

"""System, environment, and device diagnostics (artemis doctor)."""

import shutil
import subprocess
import sys

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from artemis.config import (
    parse_llm_config,
    settings,
)


def doctor_command() -> None:
    """Run diagnostics to inspect system dependencies, device connectivity, and configuration."""
    console = Console()
    console.print()
    console.print(
        Panel(
            "[bold cyan]☕ Artemis System & Environment Doctor[/bold cyan]\n"
            "[dim]Checking prerequisites for autonomous mobile testing...[/dim]",
            expand=False,
        )
    )

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Component", style="dim", width=22)
    table.add_column("Status", width=12)
    table.add_column("Details & Recommendations")

    all_passed = True

    # 1. Python Version
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info >= (3, 12):
        table.add_row(
            "Python Runtime",
            "[bold green]✔ OK[/bold green]",
            f"Python {py_ver} (64-bit)",
        )
    else:
        all_passed = False
        table.add_row(
            "Python Runtime",
            "[bold red]✖ Warning[/bold red]",
            f"Python {py_ver} detected. Python >= 3.12 is strongly recommended.",
        )

    # 2. ADB Installation & Server
    adb_path = shutil.which("adb")
    if adb_path:
        try:
            res = subprocess.run(
                [adb_path, "version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            adb_info = res.stdout.splitlines()[0] if res.stdout else "Installed"
            table.add_row(
                "Android ADB", "[bold green]✔ OK[/bold green]", f"{adb_path} ({adb_info})"
            )
        except Exception:
            table.add_row("Android ADB", "[bold green]✔ OK[/bold green]", f"{adb_path}")
    else:
        all_passed = False
        table.add_row(
            "Android ADB",
            "[bold red]✖ Missing[/bold red]",
            "adb not found in PATH. Install Android Platform-Tools.",
        )

    # 3. Connected Android Devices
    devices_info: list[tuple[str, str, str]] = []
    if adb_path:
        try:
            res = subprocess.run(
                [adb_path, "devices", "-l"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            lines = [line_str.strip() for line_str in res.stdout.splitlines() if line_str.strip()]
            for line in lines[1:]:  # skip 'List of devices attached'
                parts = line.split()
                if len(parts) >= 2:
                    serial = parts[0]
                    state = parts[1]
                    extra = " ".join(parts[2:])
                    devices_info.append((serial, state, extra))
        except Exception:
            pass

    if devices_info:
        active_devices = [d for d in devices_info if d[1] == "device"]
        if active_devices:
            summary = "\n".join(
                [
                    f"• [bold cyan]{d[0]}[/bold cyan] ({d[2] or 'Authorized'})"
                    for d in active_devices
                ]
            )
            table.add_row("Android Devices", "[bold green]✔ Connected[/bold green]", summary)
        else:
            all_passed = False
            table.add_row(
                "Android Devices",
                "[bold yellow]⚠ Unauthorized[/bold yellow]",
                "Device detected but unauthorized. Check phone screen for USB Debugging permission prompt.",
            )
    else:
        table.add_row(
            "Android Devices",
            "[bold yellow]⚠ None Found[/bold yellow]",
            "No Android device or emulator detected. Connect phone via USB or start emulator.",
        )

    # 4. LLM & Vision OCR Key Configuration
    gemini_key = settings.get_api_key("google")
    ocr_key = settings.get_api_key("ocr")
    openai_key = settings.get_api_key("openai")
    claude_key = settings.get_api_key("anthropic")
    openrouter_key = settings.get_api_key("openrouter")

    configured_keys = []
    if gemini_key:
        val = gemini_key.get_secret_value()
        masked = f"{val[:6]}...{val[-4:]}" if len(val) > 10 else "***"
        configured_keys.append(f"Gemini LLM ({masked})")
    if openai_key:
        configured_keys.append("OpenAI")
    if claude_key:
        configured_keys.append("Anthropic")
    if openrouter_key:
        configured_keys.append("OpenRouter")

    if configured_keys:
        table.add_row(
            "LLM Providers", "[bold green]✔ Configured[/bold green]", ", ".join(configured_keys)
        )
    else:
        all_passed = False
        table.add_row(
            "LLM Providers",
            "[bold red]✖ Missing[/bold red]",
            "No LLM key found in .env. Run [bold cyan]artemis init[/bold cyan] to configure in 10 seconds.",
        )

    # 5. Vision OCR Key Check
    if ocr_key:
        val = ocr_key.get_secret_value()
        masked = f"{val[:6]}...{val[-4:]}" if len(val) > 10 else "***"
        table.add_row(
            "Vision OCR",
            "[bold green]✔ Configured[/bold green]",
            f"Google Cloud Vision API ({masked})",
        )
    else:
        table.add_row(
            "Vision OCR",
            "[bold yellow]⚠ Missing[/bold yellow]",
            "OCR_API_KEY / API_KEY not set. Perception fast path will use fallback.",
        )

    # 6. Configuration File Health
    try:
        cfg = parse_llm_config()
        planner_model = cfg.planner.model
        table.add_row(
            "Configuration",
            "[bold green]✔ Valid[/bold green]",
            f"config/artemis.jsonc parsed (Planner: {planner_model})",
        )
    except Exception as e:
        all_passed = False
        table.add_row(
            "Configuration",
            "[bold red]✖ Error[/bold red]",
            f"Failed to parse config/artemis.jsonc: {e}",
        )

    # 7. Video Recording Tools (FFmpeg & scrcpy)
    ffmpeg_path = shutil.which("ffmpeg")
    scrcpy_path = shutil.which("scrcpy")

    if ffmpeg_path:
        table.add_row(
            "FFmpeg Tools",
            "[bold green]✔ Installed[/bold green]",
            f"{ffmpeg_path} (Video encoding/trimming ready)",
        )
    else:
        table.add_row(
            "FFmpeg Tools",
            "[dim]⚪ Optional[/dim]",
            "Not found. Install ffmpeg for video compression and audio analysis.",
        )

    if scrcpy_path:
        table.add_row(
            "Screen Recorder",
            "[bold green]✔ Installed[/bold green]",
            f"{scrcpy_path} (Live screen recording ready)",
        )
    else:
        table.add_row(
            "Screen Recorder",
            "[dim]⚪ Optional[/dim]",
            "scrcpy not found. Install scrcpy for --with-video-recording-tools.",
        )

    console.print(table)
    console.print()

    if all_passed:
        console.print(
            Panel(
                "[bold green]🎉 All system checks passed![/bold green]\n\n"
                "Run your first automation task now:\n"
                '  [bold cyan]artemis run "Open Settings and check Battery level"[/bold cyan]',
                title="Status: Ready",
                expand=False,
            )
        )
    else:
        console.print(
            Panel(
                "[bold yellow]💡 Tip:[/bold yellow] Run [bold cyan]artemis init[/bold cyan] to quickly set up your API key and device.",
                title="Action Required",
                expand=False,
            )
        )
    console.print()
