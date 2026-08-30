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

"""Task execution command (artemis run)."""

import asyncio
import os
from pathlib import Path
from shutil import which
from typing import Annotated

from adbutils import AdbClient
from langchain_core.callbacks.base import Callbacks
from artemis.config import initialize_llm_config, settings
from artemis.utils.startup_progress import publish_startup_progress
from artemis import Agent, Builders
from artemis.sdk.types.task import AgentProfile
from artemis.utils.cli_helpers import display_device_status
from artemis.utils.logger import get_logger
from artemis.utils.video import check_ffmpeg_available
import signal
from rich.console import Console
from rich.panel import Panel
import typer

logger = get_logger(__name__)


async def execute_task(
    goal: str,
    device_serial: str | None = None,
    session_id: str | None = None,
    locked_app_package: str | None = None,
    test_name: str | None = None,
    traces_output_path_str: str | None = None,
    output_description: str | None = None,
    graph_config_callbacks: Callbacks = [],
    video_recording_tools_enabled: bool | None = None,
    profile: str | None = None,
    app_path: str | None = None,
    enable_planner_validation: bool | None = None,
    enable_committee: bool | None = None,
    enable_checker: bool | None = None,
    enable_step_summarizer: bool | None = None,
    enable_outputter: bool | None = None,
    force_output_synthesis: bool | None = None,
    explorer_version: str | None = None,
    explorer_flash_mode: str | None = None,
    explorer_pro_mode: str | None = None,
) -> None:
    """Executes a single mobile automation task end-to-end.

    Args:
        goal: Target objective to achieve on the mobile device.
        locked_app_package: Optional package name to constrain actions to.
        test_name: Optional test identifier for trace directory naming.
        traces_output_path_str: Destination path for recording traces.
        output_description: Structured natural language output description.
        graph_config_callbacks: LangGraph callback handlers.
        video_recording_tools_enabled: Whether screen video analyzer is enabled.
        profile: Execution profile ('flash' for fast reactive loop, 'pro' for full graph).
        app_path: Optional local APK path to install prior to execution.
        enable_planner_validation: Explicit override to enable/disable async planner validation.
        enable_committee: Explicit override to enable/disable Multi-Agent Committee tool.
        explorer_version: Override default Explorer version mode ('flash', 'pro', or 'ultra').
        explorer_flash_mode: Override Explorer version mode for Flash execution profile.
        explorer_pro_mode: Override Explorer version mode for Pro execution profile.
    """
    effective_sid = (
        session_id
        or os.getenv("ARTEMIS_SESSION_ID")
        or os.getenv("ARTEMIS_CLOUD_SESSION_ID")
    )
    if effective_sid:
        os.environ["ARTEMIS_SESSION_ID"] = str(effective_sid)
    if not os.environ.get("ARTEMIS_TASK_INGRESS"):
        os.environ["ARTEMIS_TASK_INGRESS"] = "cli"
    publish_startup_progress(
        "configuration", "Loading the run configuration", session_id=str(effective_sid) if effective_sid else None
    )

    llm_config = initialize_llm_config()
    agent_profile = AgentProfile(name="default", llm_config=llm_config)
    config = Builders.AgentConfig.with_default_profile(profile=agent_profile)

    if video_recording_tools_enabled is not None:
        config.with_video_recording_tools(enabled=video_recording_tools_enabled)

    if enable_planner_validation is not None:
        config.with_planner_validation(enabled=enable_planner_validation)

    if enable_committee is not None:
        config.with_committee(enabled=enable_committee)

    if enable_checker is not None:
        config.with_checker(enabled=enable_checker)

    if enable_step_summarizer is not None:
        config.with_flash_step_summarizer(enabled=enable_step_summarizer)

    if enable_outputter is not None or force_output_synthesis is not None:
        config.with_outputter(
            enabled=enable_outputter if enable_outputter is not None else True,
            force_synthesis=bool(force_output_synthesis),
        )

    if (
        explorer_version is not None
        or explorer_flash_mode is not None
        or explorer_pro_mode is not None
    ):
        config.with_explorer(
            version=explorer_version,
            flash_mode=explorer_flash_mode,
            pro_mode=explorer_pro_mode,
        )

    if settings.ADB_HOST:
        config.with_adb_server(host=settings.ADB_HOST, port=settings.ADB_PORT)

    target_serial = device_serial or settings.ADB_DEVICE_SERIAL or os.environ.get("ADB_DEVICE_SERIAL")
    if not target_serial:
        try:
            from artemis.runtime import device_pool
            target_serial = device_pool.select_device()
        except Exception:
            target_serial = None

    if target_serial:
        from artemis.context import DevicePlatform
        config.for_device(DevicePlatform.ANDROID, target_serial)

    if graph_config_callbacks:
        config.with_graph_config_callbacks(graph_config_callbacks)

    agent: Agent | None = None
    try:
        agent = Agent(config=config.build(), session_id=effective_sid)
        await agent.init(
            retry_count=int(os.getenv("ARTEMIS_HEALTH_RETRIES", 5)),
            retry_wait_seconds=int(os.getenv("ARTEMIS_HEALTH_DELAY", 2)),
        )

        task = agent.new_task(goal)
        if locked_app_package:
            task.with_locked_app_package(locked_app_package)
        if test_name:
            trace_path = traces_output_path_str or str(settings.TRACES_PATH)
            task.with_name(test_name).with_trace_recording(path=trace_path)
        if output_description:
            task.with_output_description(output_description)
        if profile:
            task.using_profile(profile)
        if app_path:
            task.with_app_path(Path(app_path))

        llm_result_path = os.getenv("RESULTS_OUTPUT_PATH", None)
        if llm_result_path:
            task.with_llm_output_saving(path=llm_result_path)

        await agent.run_task(request=task.build())
    finally:
        if agent is not None:
            await agent.clean()


def run_command(
    goal: Annotated[str, typer.Argument(help="The main goal for the agent to achieve.")],
    profile: Annotated[
        str | None,
        typer.Option(
            "--profile",
            "-p",
            help="Execution profile ('flash' for fast reactive, 'pro' for full graph).",
        ),
    ] = "pro",
    locked_app_package: Annotated[
        str | None,
        typer.Option(
            "--locked-app",
            "-a",
            help="Optional Android package name to restrict the agent to.",
        ),
    ] = None,
    test_name: Annotated[
        str | None,
        typer.Option(
            "--test-name",
            "-n",
            help="Name of the test run for trace recording.",
        ),
    ] = None,
    traces_path: Annotated[
        str | None,
        typer.Option(
            "--traces-path",
            "-t",
            help="Directory where execution traces are persisted.",
        ),
    ] = None,
    output_description: Annotated[
        str | None,
        typer.Option(
            "--output-description",
            "-o",
            help="Natural language or schema description of the expected output.",
        ),
    ] = None,
    with_video_recording_tools: Annotated[
        bool | None,
        typer.Option(
            "--with-video-recording-tools/--without-video-recording-tools",
            help=(
                "Enable or disable dynamic video recording and screen analysis "
                "tools (auto-detected if omitted)."
            ),
        ),
    ] = None,
    app_path: Annotated[
        str | None,
        typer.Option(
            "--app-path",
            help="Local APK path to install before starting the task.",
        ),
    ] = None,
    enable_planner_validation: Annotated[
        bool | None,
        typer.Option(
            "--enable-planner-validation/--disable-planner-validation",
            help="Enable or disable async Planner validation when task plan milestones change.",
        ),
    ] = None,
    enable_committee: Annotated[
        bool | None,
        typer.Option(
            "--enable-committee/--disable-committee",
            help="Enable or disable Multi-Agent Committee council debate tool for Operator.",
        ),
    ] = None,
    enable_checker: Annotated[
        bool | None,
        typer.Option(
            "--enable-checker/--disable-checker",
            help="Enable or disable visual subgoal verification and rollback by the Checker agent.",
        ),
    ] = None,
    enable_step_summarizer: Annotated[
        bool | None,
        typer.Option(
            "--enable-step-summarizer/--disable-step-summarizer",
            help="Enable or disable Flash asynchronous objective visual step state summarizer.",
        ),
    ] = None,
    enable_outputter: Annotated[
        bool | None,
        typer.Option(
            "--enable-outputter/--disable-outputter",
            help="Enable or disable Outputter post-execution report and structured output synthesis.",
        ),
    ] = None,
    force_output_synthesis: Annotated[
        bool | None,
        typer.Option(
            "--force-output-synthesis/--no-force-output-synthesis",
            help="Force Outputter report synthesis even if no structured output schema is specified.",
        ),
    ] = None,
    explorer_version: Annotated[
        str | None,
        typer.Option(
            "--explorer-version",
            help="Default Explorer version mode ('flash' for 1-shot detection, 'pro' for 3-turn ReAct, 'ultra' for deep pixel reasoning).",
        ),
    ] = None,
    explorer_flash_mode: Annotated[
        str | None,
        typer.Option(
            "--explorer-flash-mode",
            help="Explorer version mode when running under Flash profile (FlashRunner).",
        ),
    ] = None,
    explorer_pro_mode: Annotated[
        str | None,
        typer.Option(
            "--explorer-pro-mode",
            help="Explorer version mode when running under Pro profile (Operator/Validator).",
        ),
    ] = None,
    device_serial: Annotated[
        str | None,
        typer.Option(
            "--device-serial",
            "-s",
            help="Target specific Android device by serial number (e.g. emulator-5554).",
        ),
    ] = None,
    session_id: Annotated[
        str | None,
        typer.Option(
            "--session-id",
            help="Canonical session UUID for trace and stream telemetry.",
        ),
    ] = None,
    standalone: Annotated[
        bool,
        typer.Option(
            "--standalone",
            help="Run in standalone embedded mode without auto-spawning the Artemis Daemon.",
        ),
    ] = False,
) -> None:
    """Run an autonomous UI automation task on the connected Android device."""
    if with_video_recording_tools:
        check_ffmpeg_available()

    console = Console()

    is_worker = (
        os.environ.get("ARTEMIS_TASK_WORKER") == "1"
        or os.environ.get("ARTEMIS_DEVICE_QUEUE_TICKET") is not None
    )
    is_standalone = standalone or os.environ.get("ARTEMIS_STANDALONE") == "1"

    # All platforms route through unified Artemis Daemon unless specifically configured as standalone
    if not is_worker and not is_standalone:
        try:
            import uuid
            from artemis.runtime import (
                ensure_daemon_running,
                submit_task_to_daemon,
                wait_for_daemon_task,
                stop_task_on_daemon,
            )

            console.print("[dim]Connecting to Artemis Daemon scheduler...[/dim]")
            is_running, base_url = ensure_daemon_running(timeout=8.0, wait_ready=True)
            if is_running and base_url:
                target_sid = session_id or str(uuid.uuid4())
                console.print(f"[bold green]✓[/bold green] Artemis Daemon active at [cyan]{base_url}[/cyan]")
                resp = submit_task_to_daemon(
                    goal=goal,
                    profile=profile or "pro",
                    device_serial=device_serial,
                    expected_output=output_description,
                    enable_outputter=enable_outputter,
                    locked_app_package=locked_app_package,
                    app_path=app_path,
                    session_id=target_sid,
                    ingress="cli",
                    base_url=base_url,
                )
                if resp and resp.get("tasks"):
                    console.print(f"[bold green]✓[/bold green] Task scheduled in unified queue (Session: [cyan]{target_sid}[/cyan])")
                    console.print(f"[dim]Live dashboard & replay: {base_url}[/dim]\n")

                    def on_status(sess_info):
                        st = sess_info.get("status")
                        if st == "queued":
                            console.print("[yellow]⏳ Task queued in scheduler, waiting for device...[/yellow]")
                        elif st == "running":
                            console.print("[green]▶ Task executing on mobile device...[/green]")

                    try:
                        final_res = wait_for_daemon_task(
                            target_sid,
                            base_url=base_url,
                            timeout=1800.0,
                            on_status_update=on_status,
                        )
                        final_st = final_res.get("status")
                        if final_st in ("completed", "success"):
                            console.print("\n[bold green]✅ Task completed successfully![/bold green]")
                            return
                        else:
                            err = final_res.get("error") or final_res.get("explanation") or ""
                            console.print(f"\n[bold red]✖ Task {final_st}[/bold red]: {err}")
                            raise SystemExit(1)
                    except KeyboardInterrupt:
                        console.print("\n[yellow]Stopping task on Daemon...[/yellow]")
                        stop_task_on_daemon(target_sid, base_url=base_url)
                        console.print("[yellow]Task cancelled.[/yellow]")
                        raise SystemExit(130)
                else:
                    console.print("[yellow]Warning: Could not enqueue task to Daemon. Falling back to local execution...[/yellow]")
            else:
                console.print("[yellow]Warning: Artemis Daemon could not be started. Falling back to local execution...[/yellow]")
        except SystemExit:
            raise
        except Exception as exc:
            logger.debug(f"Daemon dispatch error: {exc}")
            console.print(f"[yellow]Daemon routing notice: {exc}. Falling back to local execution...[/yellow]")

    adb_client = None
    try:
        if which("adb"):
            adb_client = AdbClient(
                host=settings.ADB_HOST or "localhost",
                port=settings.ADB_PORT or 5037,
            )
    except Exception:
        pass

    display_device_status(console, adb_client=adb_client)

    cancelled = False
    original_sigterm = None
    try:
        if hasattr(signal, "SIGTERM"):
            original_sigterm = signal.signal(
                signal.SIGTERM, lambda s, f: (_ for _ in ()).throw(KeyboardInterrupt())
            )
    except Exception:
        pass

    try:
        asyncio.run(
            execute_task(
                goal=goal,
                device_serial=device_serial,
                session_id=session_id,
                locked_app_package=locked_app_package,
                test_name=test_name,
                traces_output_path_str=traces_path,
                output_description=output_description,
                video_recording_tools_enabled=with_video_recording_tools,
                profile=profile,
                app_path=app_path,
                enable_planner_validation=enable_planner_validation,
                enable_committee=enable_committee,
                enable_checker=enable_checker,
                enable_step_summarizer=enable_step_summarizer,
                enable_outputter=enable_outputter,
                force_output_synthesis=force_output_synthesis,
                explorer_version=explorer_version,
                explorer_flash_mode=explorer_flash_mode,
                explorer_pro_mode=explorer_pro_mode,
            )
        )
    except KeyboardInterrupt:
        cancelled = True
    except Exception as e:
        err_msg = str(e)
        if "API_KEY" in err_msg or "requires" in err_msg:
            console.print()
            console.print(
                Panel(
                    f"[bold red]✖ Authentication Error:[/bold red] {err_msg}\n\n"
                    "💡 [bold cyan]Quick Fix:[/bold cyan] Run [bold green]artemis init[/bold green] to configure your API key in 10 seconds,\n"
                    "or add [bold]GEMINI_API_KEY=your_key[/bold] to [dim].env[/dim].",
                    title="Missing API Key",
                    expand=False,
                )
            )
            console.print()
            raise SystemExit(1)
        else:
            console.print(f"[bold red]Task execution failed:[/bold red] {e}")
            raise
    finally:
        try:
            if original_sigterm is not None and hasattr(signal, "SIGTERM"):
                signal.signal(signal.SIGTERM, original_sigterm)
        except Exception:
            pass
        if cancelled:
            raise SystemExit(130)
