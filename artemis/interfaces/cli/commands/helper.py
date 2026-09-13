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

"""``artemis helper``: manage the Accessibility Helper APK on attached devices.

Tasks provision the helper themselves when they take a device, so these
commands exist for the cases the automatic path deliberately leaves alone:
inspecting a device, forcing a reinstall, and removing the helper (which is
never done automatically).
"""

from __future__ import annotations

import json
from typing import Annotated

from rich.console import Console
from rich.table import Table
import typer

from artemis.runtime.device_pool import device_pool
from artemis.runtime.helper_manager import helper_manager
from artemis.utils.logger import get_logger

logger = get_logger(__name__)
helper_app = typer.Typer(help="Install, inspect, or remove the Artemis Accessibility Helper APK.")

SerialOption = Annotated[
    str | None,
    typer.Option("--serial", "-s", help="Device serial. Defaults to the only ready device."),
]


def _resolve_serial(serial: str | None) -> str:
    if serial:
        return serial
    ready = [d.serial for d in device_pool.get_ready_devices()]
    if len(ready) == 1:
        return ready[0]
    if not ready:
        typer.secho("No authorized device is attached.", fg=typer.colors.RED)
    else:
        typer.secho(
            "Several devices are ready; pass --serial, or --all to target every idle one: "
            + ", ".join(ready),
            fg=typer.colors.RED,
        )
    raise typer.Exit(code=1)


def _idle_ready_serials() -> list[str]:
    """Every authorized device that no Artemis task currently holds."""
    return [d.serial for d in device_pool.get_ready_devices() if not d.is_busy]


def _bool(value: object) -> str:
    return "[green]yes[/green]" if value else "[red]no[/red]"


@helper_app.command("status")
def helper_status(
    serial: SerialOption = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print raw JSON.")] = False,
) -> None:
    """Show install, version, service and tunnel state of the helper on a device."""
    target = _resolve_serial(serial)
    status = helper_manager.status(target)
    if as_json:
        typer.echo(json.dumps(status, indent=2))
        return
    table = Table(title=f"Accessibility helper on {target}")
    table.add_column("Field", style="cyan")
    table.add_column("Value")
    table.add_row("Installed", _bool(status["installed"]))
    version = str(status["installed_version"])
    if status.get("newer_than_bundled"):
        version += f"  [yellow](newer than the bundled v{status['bundled_version']}; not downgraded)[/yellow]"
    elif status.get("outdated"):
        version += f"  [yellow](bundled is v{status['bundled_version']}; the next task upgrades it)[/yellow]"
    table.add_row("Installed version", version)
    table.add_row("Bundled version", str(status["bundled_version"]))
    table.add_row("Service enabled", _bool(status["enabled"]))
    tunnel = {
        "session": f"this process, host port {status['forward_port']}",
        "shared": f"another Artemis process, host port {status['forward_port']}",
        "probe": "none (probed through a temporary forward)",
        None: "none",
    }[status.get("tunnel")]
    table.add_row("Tunnel", tunnel)
    table.add_row("Service answering", _bool(status["reachable"]))
    if status.get("reachable"):
        protocol = status.get("protocol_version")
        proto_text = str(protocol)
        if not status.get("protocol_supported"):
            proto_text += "  [red](too old for this host; reinstall with --force)[/red]"
        table.add_row("Protocol", proto_text)
        table.add_row("Session token pushed", _bool(status.get("token_set")))
    table.add_row("Auto-install by tasks", _bool(status.get("auto_install", True)))
    table.add_row("Transport id", str(status["transport_id"]))
    Console().print(table)


def _install_one(target: str, force: bool) -> bool:
    def on_event(event: str, details: dict) -> None:
        if event == "installing":
            typer.echo(f"{target}: installing v{details.get('version_name')}...")
        elif event == "upgrading":
            typer.echo(
                f"{target}: upgrading v{details.get('from_version')} -> "
                f"v{details.get('to_version')}..."
            )

    result = helper_manager.provision(target, force=force, on_event=on_event)
    if result.ok:
        typer.secho(
            f"{target}: {result.action} (version {result.installed_version}, service enabled).",
            fg=typer.colors.GREEN,
        )
        return True
    typer.secho(f"{target}: {result.action}: {result.error}", fg=typer.colors.RED)
    return False


@helper_app.command("install")
def helper_install(
    serial: SerialOption = None,
    force: Annotated[bool, typer.Option("--force", help="Reinstall even when up to date.")] = False,
    all_devices: Annotated[
        bool, typer.Option("--all", help="Every authorized device that no task is using.")
    ] = False,
) -> None:
    """Install or upgrade the helper to the bundled version and enable its service."""
    if all_devices:
        targets = _idle_ready_serials()
        if not targets:
            typer.secho("No idle, authorized device is attached.", fg=typer.colors.RED)
            raise typer.Exit(code=1)
    else:
        targets = [_resolve_serial(serial)]
    outcomes = [_install_one(target, force) for target in targets]
    if not all(outcomes):
        raise typer.Exit(code=1)


@helper_app.command("uninstall")
def helper_uninstall(
    serial: SerialOption = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
) -> None:
    """Disable the service and remove the helper package from a device."""
    target = _resolve_serial(serial)
    if not yes and not typer.confirm(f"Remove the Artemis accessibility helper from {target}?"):
        raise typer.Exit(code=0)
    if helper_manager.uninstall(target):
        typer.secho(f"{target}: helper removed.", fg=typer.colors.GREEN)
        return
    typer.secho(f"{target}: uninstall failed (is the package installed?).", fg=typer.colors.RED)
    raise typer.Exit(code=1)


@helper_app.command("parity")
def helper_parity(
    serial: SerialOption = None,
    rounds: Annotated[int, typer.Option("--rounds", help="Timed dumps per backend.")] = 3,
    as_json: Annotated[bool, typer.Option("--json", help="Print raw JSON.")] = False,
) -> None:
    """Dump the current screen with the helper and with UIAutomator2 and compare them.

    The two backends must describe the same visible screen: every labelled
    UIAutomator element should have a helper element with the same label at
    the same place, the helper must emit no off-screen or negative bounds, and
    its dump should not be slower. Run it on a busy screen (a scrolled list, a
    dialog over an app) to catch regressions the unit tests cannot see.
    """
    from artemis.core.diagnostics.hierarchy_parity import compare_backends

    target = _resolve_serial(serial)
    report = compare_backends(target, rounds=max(1, rounds))
    if as_json:
        typer.echo(json.dumps(report, indent=2, ensure_ascii=False))
        raise typer.Exit(code=0 if report["ok"] else 1)

    table = Table(title=f"Hierarchy parity on {target}")
    table.add_column("Metric", style="cyan")
    table.add_column("Helper")
    table.add_column("UIAutomator2")
    h, u = report["helper"], report["uiautomator"]
    table.add_row("Dump time, median", f"{h['median_ms']:.0f} ms", f"{u['median_ms']:.0f} ms")
    table.add_row("Nodes", str(h["nodes"]), str(u["nodes"]))
    table.add_row("Labelled nodes (text / desc)", str(h["labelled"]), str(u["labelled"]))
    table.add_row("Negative bounds", str(h["negative_bounds"]), str(u["negative_bounds"]))
    table.add_row("Off-screen bounds", str(h["offscreen_bounds"]), str(u["offscreen_bounds"]))
    table.add_row("Foreground package", str(h["package"]), str(u["package"]))
    Console().print(table)

    m = report["match"]
    typer.echo(
        f"UIAutomator labels found in helper dump: {m['uiautomator_in_helper']}/{m['uiautomator_labelled']}"
        f" ({m['recall']:.0%}); helper labels found in UIAutomator dump: "
        f"{m['helper_in_uiautomator']}/{m['helper_labelled']} ({m['precision']:.0%})"
    )
    for label in m["missing_in_helper"][:10]:
        typer.echo(f"  helper lacks: {label}")
    for label in m["extra_in_helper"][:10]:
        typer.echo(f"  helper adds:  {label}")
    for problem in report["problems"]:
        typer.secho(f"  ! {problem}", fg=typer.colors.RED)
    typer.secho(
        "PARITY OK" if report["ok"] else "PARITY FAILED",
        fg=typer.colors.GREEN if report["ok"] else typer.colors.RED,
    )
    raise typer.Exit(code=0 if report["ok"] else 1)


__all__ = ["helper_app"]
