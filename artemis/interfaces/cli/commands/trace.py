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

"""Trace inspection and replay commands (artemis trace)."""

import datetime
import json
from pathlib import Path
from typing import Annotated

from artemis.config import settings
from artemis.utils.logger import get_logger
from rich.console import Console
from rich.table import Table
import typer

logger = get_logger(__name__)
trace_app = typer.Typer(help="Inspect, list, and query task execution traces.")


@trace_app.command("list")
def list_traces(
    traces_path: Annotated[
        Path | None,
        typer.Option("--path", "-p", help="Custom traces directory path."),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", "-l", help="Max sessions to list.")] = 20,
) -> None:
    """List recorded execution trace sessions."""
    base_dir = traces_path or settings.TRACES_PATH
    if not base_dir.exists():
        typer.secho(f"No traces found at: {base_dir}", fg=typer.colors.YELLOW)
        return

    sessions = [d for d in base_dir.iterdir() if d.is_dir() and not d.name.startswith(".")]
    sessions.sort(key=lambda x: x.stat().st_mtime, reverse=True)

    console = Console()
    table = Table(title=f"Recorded Traces ({len(sessions)} total)")
    table.add_column("Session Name", style="cyan")
    table.add_column("Last Modified", style="green")
    table.add_column("Artifacts", style="white")

    for s in sessions[:limit]:
        artifacts = []
        if (s / "recording.mp4").exists() or (s / "recording.mkv").exists():
            artifacts.append("🎬 Video")
        if (s / "steps.json").exists():
            artifacts.append("📋 Steps")
        if (s / "notes").exists():
            artifacts.append("📝 Notes")
        mtime = time_str = time_to_str(s.stat().st_mtime)
        table.add_row(s.name, mtime, ", ".join(artifacts) or "Screenshots")

    console.print(table)


def time_to_str(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


@trace_app.command("view")
def view_trace(
    session_name: Annotated[str, typer.Argument(help="Name of the trace session directory.")],
    traces_path: Annotated[Path | None, typer.Option("--path", "-p")] = None,
) -> None:
    """View step-by-step summary of a recorded trace session."""
    base_dir = traces_path or settings.TRACES_PATH
    session_dir = base_dir / session_name

    if not session_dir.exists():
        typer.secho(f"Session '{session_name}' not found in {base_dir}", fg=typer.colors.RED)
        raise typer.Exit(1)

    steps_file = session_dir / "steps.json"
    console = Console()

    if steps_file.exists():
        try:
            steps_data = json.loads(steps_file.read_text(encoding="utf-8"))
            table = Table(title=f"Trace Steps: {session_name}")
            table.add_column("Step", justify="right", style="cyan")
            table.add_column("Action", style="green")
            table.add_column("Reasoning / Motivation", style="white")

            for step in steps_data:
                table.add_row(
                    str(step.get("step", "-")),
                    step.get("action", "-"),
                    step.get("motivation", step.get("thought", "-")),
                )
            console.print(table)
            return
        except Exception as e:
            logger.warning(f"Could not parse steps.json: {e}")

    # If steps.json not present, list files
    typer.secho(f"Session files in {session_dir}:", fg=typer.colors.CYAN)
    for f in session_dir.iterdir():
        typer.echo(f"  - {f.name}")
