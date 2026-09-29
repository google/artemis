"""Optional Codex-powered automation using a managed ChatGPT login."""

import asyncio
import json
from pathlib import Path
import tempfile
from typing import Annotated
import webbrowser

import typer

from artemis.integrations.codex.client import CodexClient, CodexError

codex_app = typer.Typer(no_args_is_help=True)
Executable = Annotated[str, typer.Option("--codex-bin", help="Codex executable name or path.")]


def _execute(coroutine):
    try:
        return asyncio.run(coroutine)
    except (CodexError, OSError, TimeoutError, ValueError) as exc:
        typer.echo(f"Codex: {exc or 'operation timed out'}", err=True)
        raise typer.Exit(1) from exc
    except KeyboardInterrupt:
        typer.echo("Codex operation cancelled.", err=True)
        raise typer.Exit(130) from None


@codex_app.command("status")
def status(executable: Executable = "codex"):
    """Check Codex's managed ChatGPT login without making a model request."""

    async def check():
        with tempfile.TemporaryDirectory(prefix="artemis-codex-") as directory:
            async with CodexClient(Path(directory), executable) as client:
                result = await client.account()
                typer.echo(json.dumps(result))
                if not result["authenticated"]:
                    raise CodexError("Run `artemis codex login` to sign in with ChatGPT.")

    _execute(check())


@codex_app.command("login")
def login(
    executable: Executable = "codex",
    device_code: Annotated[bool, typer.Option("--device-code")] = False,
):
    """Sign in through Codex; reuse an existing ChatGPT session when available."""

    async def authenticate():
        with tempfile.TemporaryDirectory(prefix="artemis-codex-") as directory:
            async with CodexClient(Path(directory), executable) as client:
                if (await client.account())["authenticated"]:
                    typer.echo("Already signed in with ChatGPT through Codex.")
                    return
                result = await client.request(
                    "account/login/start",
                    {"type": "chatgptDeviceCode" if device_code else "chatgpt"},
                )
                login_id = result["loginId"]
                completed = False
                try:
                    if device_code:
                        typer.echo(
                            f"Open {result['verificationUrl']} and enter {result['userCode']}"
                        )
                    else:
                        typer.echo(f"Open this URL to sign in: {result['authUrl']}")
                        webbrowser.open(result["authUrl"])
                    async with asyncio.timeout(300):
                        while True:
                            event = await client.event()
                            if "id" in event and "method" in event:
                                await client.reject_request(event)
                            params = event.get("params", {})
                            if (
                                event.get("method") == "account/login/completed"
                                and params.get("loginId") == login_id
                            ):
                                if not params.get("success"):
                                    raise CodexError(params.get("error") or "Login failed.")
                                await client.require_chatgpt()
                                completed = True
                                typer.echo("Signed in with ChatGPT through Codex.")
                                return
                finally:
                    if not completed:
                        await client.request("account/login/cancel", {"loginId": login_id})

    _execute(authenticate())


@codex_app.command("run")
def run(
    task: Annotated[str, typer.Argument(help="Natural-language Android task.")],
    serial: Annotated[str, typer.Option("--serial", help="Explicit authorized Android serial.")],
    executable: Executable = "codex",
    model: Annotated[
        str | None, typer.Option(help="Optional Codex model; defaults to Codex config.")
    ] = None,
    timeout: Annotated[
        int,
        typer.Option(min=1, max=3600, help="Codex turn timeout in seconds, after device setup."),
    ] = 300,
    max_actions: Annotated[int, typer.Option(min=1, max=200)] = 30,
):
    """Run using Codex and the Accessibility Helper; no model API key required."""
    from artemis.integrations.codex.runner import run_task

    result = _execute(run_task(task, serial, executable, model, timeout, max_actions))
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["succeeded"]:
        raise typer.Exit(1)
