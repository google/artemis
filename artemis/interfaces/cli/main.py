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

"""ARTEMIS Unified Command Line Interface (CLI)."""

import importlib.metadata
from typing import Annotated

from artemis.interfaces.cli.commands.batch import batch_command
from artemis.interfaces.cli.commands.bench import bench_command
from artemis.interfaces.cli.commands.doctor import doctor_command
from artemis.interfaces.cli.commands.init import init_command
from artemis.interfaces.cli.commands.mcp import mcp_command
from artemis.interfaces.cli.commands.run import run_command
from artemis.interfaces.cli.commands.server import server_app
from artemis.interfaces.cli.commands.trace import trace_app
from artemis.utils.logger import get_logger
from rich.console import Console
from rich.panel import Panel
import typer

logger = get_logger(__name__)

app = typer.Typer(
    name="artemis",
    help="☕ Artemis: Autonomous Multimodal Android Agent & Testing Framework.",
    add_completion=False,
    pretty_exceptions_enable=False,
    no_args_is_help=True,
)

# Register subcommands
app.command(name="run", help="Execute an autonomous task on a mobile device.")(run_command)
app.command(name="init", help="Interactive quickstart wizard to configure API keys & device.")(
    init_command
)
app.command(name="doctor", help="Check system prerequisites, device status, and configuration.")(
    doctor_command
)
app.command(name="batch", help="Execute a batch sequence of automation tasks.")(batch_command)
app.command(name="bench", help="Run AndroidWorld benchmark task evaluations.")(bench_command)
app.command(name="mcp", help="Start the Artemis Model Context Protocol (MCP) server.")(mcp_command)
app.add_typer(server_app, name="server", help="Cloud Run proxy and web dashboard server.")
app.add_typer(trace_app, name="trace", help="Inspect and query execution traces.")


def version_callback(value: bool):
    if value:
        try:
            ver = importlib.metadata.version("artemis")
        except Exception:
            ver = "3.6.3"
        console = Console()
        console.print(
            Panel(
                f"[bold cyan]Artemis Agent Platform[/bold cyan] v{ver}\n"
                "[dim]Autonomous Multimodal Mobile AI Engine[/dim]",
                title="☕ Artemis",
                expand=False,
            )
        )
        raise typer.Exit()


@app.callback()
def main_callback(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-v",
            help="Show Artemis version and exit.",
            callback=version_callback,
            is_eager=True,
        ),
    ] = False,
):
    """ARTEMIS Autonomous Mobile Agent CLI."""
    pass


def cli():
    """Main CLI entrypoint."""
    app()


if __name__ == "__main__":
    cli()
