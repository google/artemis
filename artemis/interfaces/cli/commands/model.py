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

"""Local on-device model management (artemis model).

ARTEMIS can pull and serve a local multimodal model itself — no external
serving stack required. ``pull`` downloads weights through huggingface_hub;
``serve`` runs ``mlx_vlm.server`` (installed via the ``local`` extra) as a
managed background process exposing an OpenAI-compatible endpoint that the
``custom`` LLM provider and the local-model readiness probe consume.
"""

import json
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Annotated

from artemis.config.local_models import DEFAULT_LOCAL_MODEL, LOCAL_MODELS, resolve_model_ref
from third_party.mobile_use.utils.logger import get_logger
from rich.console import Console
from rich.table import Table
import httpx
import typer

logger = get_logger(__name__)
model_app = typer.Typer(help="Pull and serve local on-device models.")

_STATE_DIR = Path.home() / ".artemis" / "local_models"
_SERVE_TIMEOUT_S = 300.0


def _state_file(port: int) -> Path:
    return _STATE_DIR / f"server-{port}.json"


def _read_state(port: int) -> dict | None:
    path = _state_file(port)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import os

        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _require_local_extra() -> None:
    try:
        import mlx_vlm  # noqa: F401
    except ImportError as e:
        typer.secho(
            "mlx-vlm is not installed. Install the local-model extra first:\n"
            "  pip install 'artemis[local]'   # Apple Silicon only\n"
            "On other platforms, run an OpenAI-compatible server (e.g. Ollama) "
            "and point api_base at it instead.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1) from e


@model_app.command("list")
def list_models() -> None:
    """List local model aliases ARTEMIS can pull and serve."""
    console = Console()
    table = Table(title="Local Models")
    table.add_column("Alias", style="cyan")
    table.add_column("Repository", style="green")
    table.add_column("Notes", style="white")
    for alias, spec in LOCAL_MODELS.items():
        table.add_row(alias, spec.repo, spec.description)
    console.print(table)
    console.print(
        "\nAliases resolve through [bold]artemis model pull/serve <alias>[/bold]; "
        "an org/repo id may be used instead of an alias."
    )


@model_app.command("pull")
def pull_model(
    model: Annotated[
        str,
        typer.Argument(help="Catalog alias or org/repo id to download."),
    ] = DEFAULT_LOCAL_MODEL,
) -> None:
    """Download model weights so they are ready to serve."""
    ref = resolve_model_ref(model)
    if ref is None:
        typer.secho(
            f"Unknown model '{model}'. Run `artemis model list` or pass an org/repo id.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)
    _require_local_extra()
    alias, repo = ref
    typer.secho(f"Pulling {alias} ({repo})...", fg=typer.colors.CYAN)
    try:
        from huggingface_hub import snapshot_download

        path = snapshot_download(repo)
    except ImportError as e:
        typer.secho(
            "huggingface_hub is not installed. Install 'artemis[local]' first.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1) from e
    except Exception as e:
        typer.secho(f"Pull failed for {repo}: {e}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from e
    typer.secho(f"Ready: {alias} cached at {path}", fg=typer.colors.GREEN)


_VISION_SOFT_TOKENS_SUPPORTED = (70, 140, 280, 560, 1120)


def _snapshot_path(repo: str) -> Path | None:
    """Locate the local HF snapshot for ``repo`` without hitting the network."""
    try:
        from huggingface_hub import snapshot_download

        return Path(snapshot_download(repo, local_files_only=True))
    except Exception:
        return None


def _apply_vision_soft_tokens(repo: str, tokens: int) -> bool:
    """Write ``image_processor.max_soft_tokens`` into the pulled snapshot.

    ``mlx_vlm.server`` exposes no flag for the vision-token budget; the
    image processor reads it from ``processor_config.json`` at load time,
    so we adjust the pulled snapshot before spawning. Idempotent.
    """
    snapshot = _snapshot_path(repo)
    if snapshot is None:
        return False
    cfg_path = snapshot / "processor_config.json"
    try:
        cfg = json.loads(cfg_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    image_proc = cfg.get("image_processor")
    if not isinstance(image_proc, dict) or "max_soft_tokens" not in image_proc:
        return False
    image_proc["max_soft_tokens"] = tokens
    cfg_path.write_text(json.dumps(cfg, indent=2))
    return True


@model_app.command("serve")
def serve_model(
    model: Annotated[
        str,
        typer.Argument(help="Catalog alias or org/repo id to serve."),
    ] = DEFAULT_LOCAL_MODEL,
    host: Annotated[str, typer.Option("--host", help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", "-p", help="Bind port.")] = 8080,
    vision_tokens: Annotated[
        int | None,
        typer.Option(
            "--vision-tokens",
            help="Image soft-token budget (Gemma 4 supports "
            f"{_VISION_SOFT_TOKENS_SUPPORTED}; catalog default used when omitted).",
        ),
    ] = None,
) -> None:
    """Start a managed OpenAI-compatible model server in the background."""
    ref = resolve_model_ref(model)
    if ref is None:
        typer.secho(
            f"Unknown model '{model}'. Run `artemis model list` or pass an org/repo id.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)
    if vision_tokens is not None and vision_tokens not in _VISION_SOFT_TOKENS_SUPPORTED:
        typer.secho(
            f"--vision-tokens must be one of {_VISION_SOFT_TOKENS_SUPPORTED}.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)
    _require_local_extra()
    alias, repo = ref
    tokens = vision_tokens
    if tokens is None:
        tokens = LOCAL_MODELS[alias].vision_soft_tokens if alias in LOCAL_MODELS else None
    if tokens is not None:
        if _apply_vision_soft_tokens(repo, tokens):
            typer.secho(f"Vision soft tokens per image: {tokens}", fg=typer.colors.CYAN)
        else:
            typer.secho(
                "Could not set the vision-token budget (model not pulled, or its "
                "processor has no max_soft_tokens); serving the model default. "
                "Run `artemis model pull` first.",
                fg=typer.colors.YELLOW,
            )
    existing = _read_state(port)
    if existing and _pid_alive(int(existing.get("pid", -1))):
        typer.secho(
            f"A server (pid {existing['pid']}) is already running on {host}:{port}.",
            fg=typer.colors.YELLOW,
        )
        return
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    log_path = _STATE_DIR / f"server-{port}.log"
    argv = [
        sys.executable,
        "-m",
        "mlx_vlm.server",
        "--model",
        repo,
        "--host",
        host,
        "--port",
        str(port),
    ]
    typer.secho(
        f"Starting {alias} ({repo}) on http://{host}:{port}/v1 — log: {log_path}",
        fg=typer.colors.CYAN,
    )
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(
            argv,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    _state_file(port).write_text(
        json.dumps({"pid": proc.pid, "alias": alias, "repo": repo, "host": host, "port": port})
    )
    base = f"http://{host}:{port}/v1"
    deadline = time.monotonic() + _SERVE_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        try:
            resp = httpx.get(f"{base}/models", timeout=2.0)
            if resp.status_code == 200:
                typer.secho(
                    f"Serving {alias} at {base} (pid {proc.pid}). Stop with "
                    f"`artemis model stop --port {port}`.",
                    fg=typer.colors.GREEN,
                )
                return
        except httpx.HTTPError:
            pass
        time.sleep(2.0)
    typer.secho(
        f"Server did not become ready within {_SERVE_TIMEOUT_S:.0f}s; "
        f"check the log at {log_path}.",
        fg=typer.colors.RED,
    )
    raise typer.Exit(code=1)


@model_app.command("stop")
def stop_model(
    port: Annotated[int, typer.Option("--port", "-p", help="Port the server was started on.")] = 8080,
) -> None:
    """Stop a managed model server started by `artemis model serve`."""
    state = _read_state(port)
    if not state:
        typer.secho(f"No managed server recorded on port {port}.", fg=typer.colors.YELLOW)
        return
    pid = int(state.get("pid", -1))
    if not _pid_alive(pid):
        _state_file(port).unlink(missing_ok=True)
        typer.secho("Server was not running; cleaned up state.", fg=typer.colors.YELLOW)
        return
    import os

    os.kill(pid, signal.SIGTERM)
    _state_file(port).unlink(missing_ok=True)
    typer.secho(f"Stopped {state.get('alias', 'server')} (pid {pid}).", fg=typer.colors.GREEN)


@model_app.command("status")
def status_model(
    port: Annotated[int, typer.Option("--port", "-p", help="Port to check.")] = 8080,
    host: Annotated[str, typer.Option("--host", help="Server host.")] = "127.0.0.1",
) -> None:
    """Report whether a managed local model server is up."""
    state = _read_state(port)
    if state and _pid_alive(int(state.get("pid", -1))):
        typer.secho(
            f"Running: {state.get('alias')} (pid {state['pid']}) on {state.get('host')}:{port}",
            fg=typer.colors.GREEN,
        )
        return
    try:
        resp = httpx.get(f"http://{host}:{port}/v1/models", timeout=3.0)
        if resp.status_code == 200:
            ids = [m.get("id") for m in resp.json().get("data", [])]
            typer.secho(
                f"An unmanaged server is serving {ids} on {host}:{port}.",
                fg=typer.colors.YELLOW,
            )
            return
    except httpx.HTTPError:
        pass
    typer.secho(f"No model server on {host}:{port}.", fg=typer.colors.RED)
