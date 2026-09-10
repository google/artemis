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

"""System, environment, and device diagnostics (artemis doctor).

The command renders the same readiness probes the web console's device wizard
and the ``mobile_diagnose`` MCP tool run (:mod:`artemis.core.diagnostics`), so
the terminal, the browser and the IDE agree on one verdict. Only two rows are
CLI-specific and not engine probes: Node.js/npm and the Showcase UI build.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import platform as sys_platform
import shutil
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
import typer

from artemis.config.paths import ROOT_DIR
from artemis.core.diagnostics.engine import readiness_engine
from artemis.core.diagnostics.readiness import (
    Verdict,
    adb_keys_corrupted,
    base_verdict,
    collect_readiness,
    sort_by_fix_order,
)
from artemis.core.diagnostics.schema import ProbeResult, ProbeStatus
from artemis.platform import OSType, platform
from artemis.runtime.device_lock import DeviceExecutionLock


@dataclass(frozen=True)
class ExtraRow:
    """A CLI-only diagnostic row that is not backed by an engine probe."""

    key: str
    title: str
    status: str  # "pass" | "missing"
    status_markup: str
    summary: str
    detail: str


@dataclass(frozen=True)
class FixOutcome:
    """Result of one ``--fix`` repair step."""

    fix: str
    success: bool
    message: str


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


async def _collect() -> list[ProbeResult]:
    report, host = await collect_readiness()
    return sort_by_fix_order([*report.probes, host])


async def _apply_fixes(results: list[ProbeResult]) -> list[FixOutcome]:
    """Repair corrupted ADB keys and sweep stale device locks left by dead runners."""
    outcomes: list[FixOutcome] = []

    if adb_keys_corrupted(results):
        result = await readiness_engine.heal_adb_keys()
        outcomes.append(
            FixOutcome(
                fix="heal_adb_keys",
                success=bool(result.get("success")),
                message=str(result.get("message") or "")
                or ("Corrupted RSA keys regenerated." if result.get("success") else "Heal failed."),
            )
        )
    else:
        outcomes.append(
            FixOutcome(
                fix="heal_adb_keys",
                success=True,
                message="ADB authentication keys are healthy; nothing to repair.",
            )
        )

    removed = await asyncio.to_thread(DeviceExecutionLock.cleanup_stale_locks)
    outcomes.append(
        FixOutcome(
            fix="cleanup_stale_locks",
            success=True,
            message=(
                f"Removed {removed} stale device lock(s)/queue ticket(s)."
                if removed
                else "No stale device locks or queue tickets found."
            ),
        )
    )
    return outcomes


async def _diagnose(fix: bool) -> tuple[list[ProbeResult], list[FixOutcome]]:
    results = await _collect()
    fixes: list[FixOutcome] = []
    if fix:
        fixes = await _apply_fixes(results)
        results = await _collect()
    return results, fixes


# ---------------------------------------------------------------------------
# CLI-only extra rows (not engine probes)
# ---------------------------------------------------------------------------


def _npm_row() -> ExtraRow:
    npm_path = shutil.which("npm")
    if npm_path:
        return ExtraRow(
            key="nodejs_npm",
            title="Node.js / npm",
            status="pass",
            status_markup="[bold green]✔ Installed[/bold green]",
            summary="Installed",
            detail=f"{npm_path} (Ready to build Showcase UI)",
        )
    hint = (
        "winget install OpenJS.NodeJS.LTS"
        if platform.os_type == OSType.WINDOWS
        else "brew install node"
        if platform.os_type == OSType.MACOS
        else "sudo apt-get install -y nodejs npm"
    )
    return ExtraRow(
        key="nodejs_npm",
        title="Node.js / npm",
        status="missing",
        status_markup="[dim]⚪ Optional[/dim]",
        summary="Not Found",
        detail=f"Not found in PATH. ({hint})",
    )


def _showcase_row() -> ExtraRow:
    from artemis.resources import get_bundled_showcase_dist

    base_dist = ROOT_DIR / "apps" / "showcase_ui" / "dist"
    candidates = [
        base_dist / "frontend" / "browser" / "index.html",
        base_dist / "browser" / "index.html",
        base_dist / "frontend" / "index.html",
        base_dist / "index.html",
    ]
    found_showcase = next((p for p in candidates if p.exists()), None)
    if found_showcase is None:
        bundled_showcase = get_bundled_showcase_dist()
        if bundled_showcase is not None:
            found_showcase = bundled_showcase / "index.html"
    if found_showcase:
        return ExtraRow(
            key="showcase_ui",
            title="Showcase UI",
            status="pass",
            status_markup="[bold green]✔ Compiled[/bold green]",
            summary="Compiled",
            detail=f"{found_showcase.parent} (Angular ready)",
        )
    return ExtraRow(
        key="showcase_ui",
        title="Showcase UI",
        status="missing",
        status_markup="[bold yellow]○ Not Compiled[/bold yellow]",
        summary="Not Compiled",
        detail="Run ./start.sh or artemis ui to auto-compile.",
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _status_markup(result: ProbeResult) -> str:
    status = result.status
    if status is ProbeStatus.PASS:
        return "[bold green]✔ OK[/bold green]"
    if status is ProbeStatus.SKIPPED:
        return "[dim]○ Skipped[/dim]"
    summary = escape(result.summary)
    if status is ProbeStatus.WARN:
        # A warning keeps its summary whether or not it blocks (the host
        # probe's warnings degrade the verdict without blocking it).
        return f"[bold yellow]⚠ {summary}[/bold yellow]"
    if not result.is_blocker:
        return "[dim]⚪ Optional[/dim]"
    return f"[bold red]✖ {summary}[/bold red]"


def _action_lines(result: ProbeResult) -> list[str]:
    """One plain-text line per remediation; ``&&`` chains split for PowerShell 5.1."""
    lines: list[str] = []
    for action in result.actions:
        payload = action.payload.strip()
        if not payload:
            continue
        if action.action_type == "command":
            for part in payload.split("&&"):
                if part.strip():
                    lines.append(f"Run: {part.strip()}")
        else:
            lines.append(payload)
    return lines


def _details_markup(result: ProbeResult) -> str:
    # Only summary/description/actions are ever rendered. Probe metadata can
    # carry raw credentials (the console prefills its settings form from it)
    # and must never reach a terminal or a log.
    parts = [escape(result.description)]
    if result.status is not ProbeStatus.PASS:
        for line in _action_lines(result):
            if line.startswith("Run: "):
                parts.append(f"[bold cyan]Run:[/bold cyan] {escape(line[len('Run: ') :])}")
            else:
                parts.append(escape(line))
    return "\n".join(parts)


def _render_table(results: list[ProbeResult], extras: list[ExtraRow]) -> Table:
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Component", style="dim", min_width=22, max_width=30)
    table.add_column("Status", min_width=12, max_width=28)
    table.add_column("Details & Recommendations", overflow="fold")
    for result in results:
        table.add_row(escape(result.title), _status_markup(result), _details_markup(result))
    for extra in extras:
        table.add_row(extra.title, extra.status_markup, escape(extra.detail))
    return table


def _render_fixes(console: Console, fixes: list[FixOutcome]) -> None:
    lines = []
    for outcome in fixes:
        mark = "[bold green]✔[/bold green]" if outcome.success else "[bold red]✖[/bold red]"
        lines.append(f"{mark} {escape(outcome.fix)}: {escape(outcome.message)}")
    console.print(Panel("\n".join(lines), title="Repairs (--fix)", expand=False))
    console.print()


def _render_footer(console: Console, verdict: Verdict, results: list[ProbeResult]) -> None:
    if verdict == "ready":
        console.print(
            Panel(
                "[bold green]🎉 All system checks passed![/bold green]\n\n"
                "Run your first automation task now:\n"
                '  [bold cyan]artemis run "Open Settings and check Battery level"[/bold cyan]',
                title="Status: Ready",
                expand=False,
            )
        )
        return

    if verdict == "degraded":
        console.print(
            Panel(
                "[bold green]Required checks passed.[/bold green] Optional components are "
                "missing; tasks can run, but some features (video replay, OCR) are off.\n\n"
                "Run your first automation task now:\n"
                '  [bold cyan]artemis run "Open Settings and check Battery level"[/bold cyan]',
                title="Status: Ready (optional gaps)",
                expand=False,
            )
        )
        return

    failing = [r.title for r in results if r.is_blocker and r.status is not ProbeStatus.PASS]
    tips = [
        "[bold yellow]💡 Blocking issues:[/bold yellow] " + escape(", ".join(failing)),
        "",
        "• API keys & device setup: run [bold cyan]artemis init[/bold cyan].",
        "• Follow the [bold cyan]Run:[/bold cyan] lines above, then re-run "
        "[bold cyan]artemis doctor[/bold cyan].",
        "• Using an IDE (Claude Code, Cursor, ...)? Ask it to call "
        "[bold cyan]mobile_diagnose[/bold cyan] - it runs these same checks and can "
        "apply safe fixes from the assistant.",
    ]
    if adb_keys_corrupted(results):
        tips.append(
            "• Corrupted ADB keys detected: run [bold cyan]artemis doctor --fix[/bold cyan]."
        )
    console.print(Panel("\n".join(tips), title="Action Required", expand=False))


def _json_document(
    results: list[ProbeResult],
    extras: list[ExtraRow],
    fixes: list[FixOutcome],
    verdict: Verdict,
) -> dict[str, Any]:
    # Metadata is intentionally omitted: it may hold raw API keys.
    checks = [
        {
            "id": r.id,
            "title": r.title,
            "status": r.status.value,
            "required": r.is_blocker,
            "summary": r.summary,
            "detail": r.description,
            "fix": []
            if r.status is ProbeStatus.PASS
            else [
                {"type": a.action_type, "label": a.label, "payload": a.payload} for a in r.actions
            ],
        }
        for r in results
    ]
    return {
        "verdict": verdict,
        "checks": checks,
        "extras": {
            e.key: {
                "title": e.title,
                "status": e.status,
                "summary": e.summary,
                "detail": e.detail,
            }
            for e in extras
        },
        "fixes": [{"fix": f.fix, "success": f.success, "message": f.message} for f in fixes],
    }


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def doctor_command(
    fix: bool = typer.Option(
        False,
        "--fix",
        "-f",
        help=(
            "Automatically repair fixable environment issues (corrupted ADB authentication "
            "keys, stale device locks left by crashed runners)."
        ),
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Print the diagnosis as a JSON document instead of a table.",
    ),
) -> None:
    """Run diagnostics to inspect system dependencies, device connectivity, and configuration."""
    results, fixes = asyncio.run(_diagnose(fix))
    extras = [_npm_row(), _showcase_row()]
    verdict = base_verdict(results)

    if json_output:
        typer.echo(
            json.dumps(
                _json_document(results, extras, fixes, verdict), indent=2, ensure_ascii=False
            )
        )
    else:
        console = Console()
        console.print()
        os_name = sys_platform.system()
        arch_name = sys_platform.machine()
        console.print(
            Panel(
                f"[bold cyan]☕ Artemis System & Environment Doctor[/bold cyan]\n"
                f"[dim]Platform: {os_name} ({arch_name}) | PAL: {platform.os_type.value}[/dim]",
                expand=False,
            )
        )
        if fixes:
            _render_fixes(console, fixes)
        console.print(_render_table(results, extras))
        console.print()
        _render_footer(console, verdict, results)
        console.print()

    if verdict == "blocked":
        raise typer.Exit(code=1)
