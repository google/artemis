# Copyright 2025-2026 Minitap, Inc.
# Modifications Copyright 2026 Google LLC
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
#
# Originally from mobile-use (https://github.com/minitap-ai/mobile-use).
# See third_party/mobile_use/METADATA for the upstream source and local modifications.

from shutil import which
import sys

from adbutils import AdbClient
from rich.console import Console

from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)


def display_device_status(console: Console, adb_client: AdbClient | None = None):
    """Checks for connected devices and displays the status."""
    console.print("\n[bold]📱 Device Status[/bold]")
    devices = None
    if adb_client is not None:
        devices = adb_client.device_list()
    if devices:
        console.print("✅ [bold green]Android device(s) connected:[/bold green]")
        for device in devices:
            console.print(f"  - {device.serial}")
    else:
        console.print("❌ [bold red]No Android device found.[/bold red]")
        command = "emulator -avd <avd_name>"
        if sys.platform not in ["win32", "darwin"]:
            command = f"./{command}"
            console.print(
                f"You can start an emulator using a command like: [bold]'{command}'[/bold]"
            )


def display_local_device_status(console: Console, host: str | None, port: int | None) -> None:
    """Display connected Android devices, using a local ADB client when ``adb`` is available."""
    adb_client = None
    try:
        if which("adb"):
            adb_client = AdbClient(host=host or "localhost", port=port or 5037)
    except Exception as exc:
        # Optional cosmetic device-status display; run continues without it.
        logger.debug(f"Could not create ADB client for device status display: {exc}")

    display_device_status(console, adb_client=adb_client)
