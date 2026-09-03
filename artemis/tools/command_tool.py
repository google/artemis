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

"""Universal ADB command and background task management tools for ARTEMIS.

State model
-----------
Every automation task owns one :class:`AdbTaskRegistry` (stored on its
:class:`~artemis.context.ArtemisContext`).  The registry holds the running
background processes, the recently finished task logs, and the persistent
terminal environments.  Keeping this per context matters because the MCP
daemon runs concurrent tasks for different devices inside one process: a
module-level registry would leak device A's tasks and completion notices into
device B's Operator prompt.  A module-level default registry still exists for
callers that have no context (tests, ad-hoc scripts).

Hang protection
---------------
Commands are passed to ``adb shell`` as an argument, never through stdin, so
the remote shell exits as soon as the script does.  ``stdin`` is closed
(``DEVNULL``) unless the caller asks for ``Interactive=True``; a command that
reads stdin therefore sees EOF instead of blocking forever.  Anything that
outlives ``WaitMsBeforeAsync`` is moved to a background task with a bounded
log buffer, and :func:`shutdown_adb_background_tasks` kills whatever is still
running when the automation task ends.
"""

import asyncio
from asyncio.subprocess import Process as AsyncProcess
from collections import deque
import os
import shlex
from typing import Any, Literal
import uuid

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from artemis.agents.log_analyzer.output_analyzer import TaskOutputAnalyzerNode
from artemis.context import ArtemisContext
from artemis.controllers.platform_specific_commands_controller import (
    get_adb_device,
)
from artemis.data_engine.trace import trace_langchain_tool
from artemis.drivers.base import BaseDeviceDriver
from artemis.tools.base import ArtemisTool
from artemis.tools.tool_wrapper import ToolWrapper
from artemis.utils.logger import get_logger

logger = get_logger(__name__)

#: Maximum number of finished task records kept per registry.
_FINISHED_TASKS_CAP = 50
#: Maximum number of output lines retained for a background task.
_BACKGROUND_LOG_MAX_LINES = 20000
#: Seconds to wait for a terminated process before killing it.
_TERMINATE_GRACE_SECONDS = 3.0

_EXIT_CODE_MARKER = "===EXIT_CODE==="
_ENV_START_MARKER = "===ENV_START==="

#: Variables that every Android shell already defines. They are never
#: re-exported into a persistent terminal: re-exporting PATH/HOME etc. is
#: pointless and some of them (BOOTCLASSPATH, LD_*) are long and fragile.
_PERSISTENT_ENV_EXCLUDE = frozenset(
    {
        "PATH",
        "HOME",
        "HOSTNAME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
        "PWD",
        "OLDPWD",
        "SHLVL",
        "_",
        "TMPDIR",
        "HISTFILE",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "EXTERNAL_STORAGE",
        "ASEC_MOUNTPOINT",
        "DOWNLOAD_CACHE",
    }
)
_PERSISTENT_ENV_EXCLUDE_PREFIXES = (
    "ANDROID_",
    "BOOTCLASSPATH",
    "DEX2OAT",
    "SYSTEMSERVER",
    "STANDALONE_",
)

#: Guidance shared by the tool descriptions: the commands that hang an
#: ``adb shell`` and how to bound them.
HANG_GUIDANCE = (
    "Streaming or open-ended commands (logcat, top, getevent, screenrecord,"
    " tail -f, an interactive shell) never return on their own: bound them"
    " (logcat -d, top -n 1, --time-limit) or expect them to be moved to a"
    " background task. Detach daemons explicitly with"
    " `nohup CMD >/dev/null 2>&1 &`, otherwise the shell waits for them."
)


def _adb_binary() -> str:
    """Resolves the adb executable through the shared toolchain resolver."""
    try:
        from artemis.toolchain import toolchain

        return toolchain.find_adb()
    except Exception:  # pylint: disable=broad-exception-caught
        return "adb"


def _is_output_long(output: str) -> bool:
    """Checks if command output exceeds the max threshold for inline display."""
    return len(output.splitlines()) > 500 or len(output) > 50000


def _format_long_output_response(_task_id: str, output: str, intro: str) -> str:
    """Formats truncated output with guidance to use the analyzer tool."""
    lines = output.splitlines()
    last_200 = "\n".join(lines[-200:])
    return (
        f"{intro}\nWarning: Output is too long ({len(lines)} lines,"
        f" {len(output)} chars) and has been truncated.\n--- Last 200 lines of"
        f" output ---\n{last_200}\n------------------------\nYou can use the"
        " 'analyze_task_output' tool to get more information."
    )


def _filter_persistent_env(env: dict[str, str]) -> dict[str, str]:
    """Drops the platform-provided variables from a captured shell environment."""
    kept: dict[str, str] = {}
    for key, value in env.items():
        if not key or key in _PERSISTENT_ENV_EXCLUDE:
            continue
        if key.startswith(_PERSISTENT_ENV_EXCLUDE_PREFIXES):
            continue
        if not key.replace("_", "a").isalnum():
            continue
        kept[key] = value
    return kept


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class AdbTaskRegistry:
    """Per-task bookkeeping for background processes, finished logs and envs."""

    def __init__(self) -> None:
        self.background: dict[str, BackgroundTask] = {}
        self.finished: dict[str, dict[str, Any]] = {}
        self.persistent_envs: dict[str, dict[str, str]] = {}

    # -- finished task cache -------------------------------------------------

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    def register_finished(
        self,
        task_id: str,
        command: str,
        cwd: str | None,
        terminal_id: str | None,
        status: str,
        exit_code: int | None,
        output: str,
    ) -> None:
        """Caches execution details for a finished task."""
        self.finished[task_id] = {
            "task_id": task_id,
            "command": command,
            "cwd": cwd,
            "terminal_id": terminal_id,
            "status": status,
            "exit_code": exit_code,
            "output": output,
            "notified": False,
        }
        while len(self.finished) > _FINISHED_TASKS_CAP:
            oldest_key = next(iter(self.finished))
            del self.finished[oldest_key]

    def get_task_info(self, task_id: str) -> dict[str, Any] | None:
        """Returns metadata and logs for a running or finished task, if known."""
        if task_id in self.background:
            t = self.background[task_id]
            return {
                "command": t.command,
                "cwd": t.cwd,
                "terminal_id": t.terminal_id,
                "status": t.status,
                "output": "".join(t.stdout_log),
            }
        return self.finished.get(task_id)

    # -- prompt helpers ------------------------------------------------------

    def active_task_summaries(self) -> list[dict[str, Any]]:
        """Compact view of running tasks for the Operator prompt."""
        return [
            {
                "task_id": tid,
                "command": t.command,
                "cwd": t.cwd,
                "terminal_id": t.terminal_id,
                "output_line_count": len(t.stdout_log),
            }
            for tid, t in self.background.items()
        ]

    def pop_unnotified_finished(self) -> list[dict[str, Any]]:
        """Returns finished tasks not yet shown to the model and marks them shown."""
        newly_finished: list[dict[str, Any]] = []
        for tid, tinfo in self.finished.items():
            if tinfo.get("notified", False):
                continue
            newly_finished.append(
                {
                    "task_id": tid,
                    "command": tinfo.get("command", ""),
                    "status": tinfo.get("status", "completed"),
                    "output_text": tinfo.get("output", ""),
                }
            )
            tinfo["notified"] = True
        return newly_finished

    # -- lifecycle -----------------------------------------------------------

    async def shutdown(self, ctx: ArtemisContext | None = None) -> int:
        """Terminates every running background task. Returns the number killed."""
        tasks = list(self.background.values())
        for task in tasks:
            try:
                await task.stop(ctx, status="killed")
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.warning(f"Failed to stop background ADB task {task.task_id}: {e}")
        return len(tasks)


#: Registry used when no context is available.
_DEFAULT_REGISTRY = AdbTaskRegistry()

# Backward-compatibility aliases onto the default registry's dictionaries.
_BACKGROUND_TASKS = _DEFAULT_REGISTRY.background
_FINISHED_TASKS_LOGS = _DEFAULT_REGISTRY.finished
_PERSISTENT_ENVIRONMENTS = _DEFAULT_REGISTRY.persistent_envs


def get_adb_task_registry(ctx: ArtemisContext | None) -> AdbTaskRegistry:
    """Returns the registry bound to ``ctx``, creating it on first use."""
    if ctx is None:
        return _DEFAULT_REGISTRY
    existing = getattr(ctx, "adb_task_registry", None)
    if isinstance(existing, AdbTaskRegistry):
        return existing
    registry = AdbTaskRegistry()
    try:
        setattr(ctx, "adb_task_registry", registry)
    except (AttributeError, TypeError):
        return _DEFAULT_REGISTRY
    return registry


async def shutdown_adb_background_tasks(ctx: ArtemisContext | None) -> int:
    """Kills the background ADB tasks of ``ctx``. Safe to call when none exist."""
    existing = getattr(ctx, "adb_task_registry", None) if ctx is not None else None
    if not isinstance(existing, AdbTaskRegistry):
        return 0
    killed = await existing.shutdown(ctx)
    if killed:
        logger.info(f"Terminated {killed} background ADB task(s) at task end.")
    return killed


# pylint: disable=too-many-arguments,too-many-positional-arguments
def _register_finished_task(
    task_id: str,
    command: str,
    cwd: str | None,
    terminal_id: str | None,
    status: str,
    exit_code: int | None,
    output: str,
    ctx: ArtemisContext | None = None,
):
    """Caches execution details for finished tasks (registry of ``ctx``)."""
    get_adb_task_registry(ctx).register_finished(
        task_id, command, cwd, terminal_id, status, exit_code, output
    )


def _get_task_info(task_id: str, ctx: ArtemisContext | None) -> dict[str, Any] | None:
    """Retrieves metadata and logs for a running or finished task.

    Looks in the context registry, then the default registry, then the
    persisted background task table.
    """
    registry = get_adb_task_registry(ctx)
    info = registry.get_task_info(task_id)
    if info is None and registry is not _DEFAULT_REGISTRY:
        info = _DEFAULT_REGISTRY.get_task_info(task_id)
    if info is not None:
        return info
    if ctx and ctx.data_engine:
        db_tasks = ctx.data_engine.get_all_background_tasks()
        for db_t in db_tasks:
            if db_t.get("task_id") == task_id:
                return {
                    "command": db_t.get("summary") or "",
                    "cwd": None,
                    "terminal_id": None,
                    "status": db_t.get("status") or "completed",
                    "output": db_t.get("logs") or "",
                }
    return None


# ---------------------------------------------------------------------------
# Background task
# ---------------------------------------------------------------------------


# pylint: disable=too-many-instance-attributes
class BackgroundTask:
    """Represents an active or completing background ADB shell task."""

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    def __init__(
        self,
        task_id: str,
        command: str,
        process: AsyncProcess,
        terminal_id: str | None = None,
        cwd: str | None = None,
        interactive: bool = False,
        registry: AdbTaskRegistry | None = None,
    ):
        self.task_id = task_id
        self.command = command
        self.process = process
        self.terminal_id = terminal_id
        self.cwd = cwd
        self.interactive = interactive
        self.registry = registry
        self.stdout_log: deque[str] = deque(maxlen=_BACKGROUND_LOG_MAX_LINES)
        self.status = "running"
        self.exit_code: int | None = None
        self.trace_id: uuid.UUID | None = None
        self.listener_task: asyncio.Task | None = None
        self._stop_requested = False
        self._finalized = False

    async def start(self, ctx: ArtemisContext):
        """Starts the background output listener task."""
        self.listener_task = asyncio.create_task(self._listen(ctx))

    async def stop(self, ctx: ArtemisContext | None, status: str = "killed") -> None:
        """Terminates the process (kill after a grace period) and records the result."""
        self._stop_requested = True
        self.status = status
        proc = self.process
        if getattr(proc, "returncode", None) is None:
            try:
                proc.terminate()
            except (ProcessLookupError, OSError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=_TERMINATE_GRACE_SECONDS)
            except TimeoutError:
                try:
                    proc.kill()
                except (ProcessLookupError, OSError):
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=_TERMINATE_GRACE_SECONDS)
                except TimeoutError:
                    pass
            except Exception as exc:  # pylint: disable=broad-exception-caught
                # Shutdown must never raise; the exit code is read back below.
                logger.debug(
                    f"Waiting for background ADB task {self.task_id} to exit failed: {exc}",
                    exc_info=True,
                )
        if self.listener_task is not None and not self.listener_task.done():
            self.listener_task.cancel()
            try:
                await self.listener_task
            except asyncio.CancelledError:
                pass
        self.exit_code = getattr(proc, "returncode", None)
        self._finalize(ctx)

    def _finalize(self, ctx: ArtemisContext | None) -> None:
        """Moves the task from the active map to the finished cache exactly once."""
        if self._finalized:
            return
        self._finalized = True
        registry = self.registry or get_adb_task_registry(ctx)

        output_text = "".join(self.stdout_log)
        _, parsed_env, _ = self.parse_output(output_text)
        if self.terminal_id and parsed_env is not None:
            registry.persistent_envs[self.terminal_id] = _filter_persistent_env(parsed_env)
            logger.info(
                "Updated persistent env for Android terminal"
                f" '{self.terminal_id}' from background task."
            )

        if ctx and getattr(ctx, "data_engine", None):
            try:
                ctx.data_engine.unregister_background_task(self.task_id, self.status)
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.debug(f"Failed to unregister background task {self.task_id}: {e}")

        registry.register_finished(
            task_id=self.task_id,
            command=self.command,
            cwd=self.cwd,
            terminal_id=self.terminal_id,
            status=self.status,
            exit_code=self.exit_code,
            output=output_text,
        )
        registry.background.pop(self.task_id, None)

    async def _listen(self, ctx: ArtemisContext):
        """Reads process output streams and updates lifecycle status."""
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    break
                decoded = line.decode(errors="replace")
                self.stdout_log.append(decoded)
                if ctx and getattr(ctx, "data_engine", None) and self.trace_id:
                    ctx.data_engine.stream_output(self.trace_id, decoded)

            self.exit_code = await self.process.wait()
            if self._stop_requested:
                return
            _, _, script_exit = self.parse_output("".join(self.stdout_log))
            if self.terminal_id and script_exit is not None:
                self.exit_code = script_exit
            self.status = "completed" if self.exit_code == 0 else "failed"
            logger.info(
                f"Background ADB task {self.task_id} completed with exit code {self.exit_code}"
            )
        except asyncio.CancelledError:
            return
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error(f"Error in background task {self.task_id} listener: {e}")
            self.status = "error"
        finally:
            if not self._stop_requested:
                self._finalize(ctx)

    @staticmethod
    def parse_output(output: str) -> tuple[str, dict[str, str] | None, int | None]:
        """Separates stdout lines from the exit-code marker and exported env.

        Returns ``(clean_output, env_or_None, script_exit_code_or_None)``.
        """
        clean_lines: list[str] = []
        env_lines: list[str] = []
        exit_code: int | None = None
        in_env = False

        for line in output.splitlines():
            if _EXIT_CODE_MARKER in line:
                raw = line.split(_EXIT_CODE_MARKER, 1)[1].strip()
                try:
                    exit_code = int(raw)
                except ValueError:
                    exit_code = None
                continue
            if _ENV_START_MARKER in line:
                in_env = True
                continue
            if in_env:
                env_lines.append(line)
            else:
                clean_lines.append(line)

        clean_output = "\n".join(clean_lines)

        parsed_env: dict[str, str] | None = None
        if in_env:
            parsed_env = {}
            for el in env_lines:
                if "=" in el:
                    k, v = el.split("=", 1)
                    parsed_env[k] = v

        return clean_output, parsed_env, exit_code

    # Backwards compatibility alias
    _parse_output = parse_output


# ---------------------------------------------------------------------------
# run_adb_command
# ---------------------------------------------------------------------------


class RunAdbCommandArgs(BaseModel):
    """Arguments schema for executing ADB shell commands."""

    CommandLine: str = Field(
        ...,
        description=(
            "The exact shell command line string to execute inside the Android device shell."
        ),
    )
    Cwd: str = Field(
        default="/data/local/tmp",
        description="The directory on the Android device to run the command in.",
    )
    RunPersistent: bool = Field(
        default=False,
        description=(
            "Set to true to run this command in a persistent terminal session"
            " that preserves environment variables between invocations on the"
            " phone."
        ),
    )
    RequestedTerminalID: str | None = Field(
        default=None,
        description=(
            "Optional ID of a persistent terminal to reuse. Reuses environment"
            " variables if provided."
        ),
    )
    WaitMsBeforeAsync: int = Field(
        default=5000,
        description=(
            "Number of milliseconds to wait for the command to finish"
            " synchronously before sending it to the background."
        ),
    )
    Interactive: bool = Field(
        default=False,
        description=(
            "Keep stdin open so 'manage_task send_input' can feed the command"
            " later. Default false: stdin is closed and commands that read it"
            " see EOF instead of hanging."
        ),
    )


def _build_phone_script(
    cmd_line: str,
    cwd: str,
    env_vars: dict[str, str],
    run_persistent: bool,
) -> str:
    """Builds the script passed as the single ``adb shell`` argument."""
    script = f"cd {shlex.quote(cwd)}\n"
    for k, v in env_vars.items():
        script += f"export {k}={shlex.quote(v)}\n"
    script += f"{cmd_line}\n"
    if run_persistent:
        script += (
            "_artemis_ec=$?\n"
            f'echo "{_EXIT_CODE_MARKER}$_artemis_ec"\n'
            f'echo "{_ENV_START_MARKER}"\n'
            "env\n"
        )
    return script


class RunAdbCommandTool(ArtemisTool):
    """Universal tool for executing ADB shell commands on an Android device."""

    def __init__(self):
        super().__init__(
            name="run_adb_command",
            description=(
                "[SHELL] Executes a shell command directly on the Android mobile "
                "device via ADB shell. Runs synchronously and transitions to a "
                "background task if execution takes longer than WaitMsBeforeAsync. "
                "Supports persistent environments (environment variables) on the "
                f"phone across invocations. {HANG_GUIDANCE}"
            ),
            args_schema=RunAdbCommandArgs,
            category="system",
        )

    # pylint: disable=too-many-arguments,too-many-locals,too-many-branches,too-many-statements,too-many-return-statements,too-many-positional-arguments,too-many-boolean-expressions
    async def execute(
        self,
        driver: BaseDeviceDriver | None = None,
        ctx: ArtemisContext | None = None,
        CommandLine: str | None = None,
        Cwd: str = "/data/local/tmp",
        RunPersistent: bool = False,
        RequestedTerminalID: str | None = None,
        WaitMsBeforeAsync: int = 5000,
        Interactive: bool = False,
        **kwargs: Any,
    ) -> str:
        # Extract command line supporting alternative key formats
        cmd_line = (
            CommandLine
            if CommandLine is not None
            else (kwargs.get("command_line") or kwargs.get("command") or kwargs.get("cmd") or "")
        )
        cwd = Cwd or kwargs.get("cwd") or "/data/local/tmp"
        run_persistent = (
            RunPersistent if RunPersistent is not False else kwargs.get("run_persistent", False)
        )
        requested_terminal_id = (
            RequestedTerminalID
            if RequestedTerminalID is not None
            else kwargs.get("requested_terminal_id")
        )
        wait_ms = (
            WaitMsBeforeAsync
            if WaitMsBeforeAsync != 5000
            else kwargs.get("wait_ms_before_async", WaitMsBeforeAsync)
        )
        interactive = bool(Interactive or kwargs.get("interactive", False))
        wait_seconds = max(float(wait_ms) / 1000.0, 0.05)

        # Fallback to driver's execute_shell if no full ArtemisContext is available
        if (
            (ctx is None or not hasattr(ctx, "device") or not ctx.device)
            and driver is not None
            and hasattr(driver, "execute_shell")
            and not run_persistent
        ):
            return await driver.execute_shell(cmd_line, timeout_seconds=wait_seconds)

        registry = get_adb_task_registry(ctx)

        device_id = None
        if ctx and hasattr(ctx, "device") and ctx.device:
            device_id = getattr(ctx.device, "device_id", None)
        elif driver and hasattr(driver, "device_id"):
            device_id = getattr(driver, "device_id", None)
        if not device_id:
            device_id = "default_device"

        # Prepare environment
        terminal_id = None
        run_env = os.environ.copy()  # Local host env to run adb command
        android_env_vars: dict[str, str] = {}

        if run_persistent:
            terminal_id = requested_terminal_id or f"term_{uuid.uuid4().hex[:8]}"
            android_env_vars = registry.persistent_envs.setdefault(terminal_id, {})

        phone_script = _build_phone_script(cmd_line, cwd, android_env_vars, run_persistent)

        # In Cloud Mode or when adb client is virtualized: no host subprocess,
        # therefore no background handoff. The wait window is a hard timeout.
        adb_client = getattr(ctx, "adb_client", None) if ctx else None
        if os.environ.get("ARTEMIS_CLOUD_MODE") == "1" or (
            adb_client is not None and hasattr(adb_client, "_bridge")
        ):
            try:
                device = get_adb_device(ctx)
                output = await asyncio.wait_for(
                    asyncio.to_thread(device.shell, phone_script),
                    timeout=wait_seconds,
                )
            except TimeoutError:
                return (
                    f"Error: Command did not finish within {wait_seconds:.1f}s."
                    " Background tasks are not available on this device"
                    " connection; bound the command (logcat -d, top -n 1,"
                    " --time-limit) and retry."
                )
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.error(f"Failed to execute virtual adb shell for '{cmd_line}': {e}")
                return f"Failed to execute adb command: {e}"
            clean_output, new_env, script_exit = BackgroundTask.parse_output(str(output))
            if run_persistent and new_env is not None:
                registry.persistent_envs[terminal_id] = _filter_persistent_env(new_env)
            intro = "ADB command completed"
            if script_exit is not None:
                intro += f" with exit code {script_exit}"
            intro += "."
            if run_persistent:
                intro += f" TerminalID: {terminal_id}."
            body = clean_output if clean_output.strip() else "(empty output)"
            return f"{intro}\nOutput:\n{body}"

        # We spawn the host subprocess: adb -s <serial> shell <script>.
        # The script travels as an argument so the remote shell exits with it;
        # stdin is closed unless the caller wants to feed input later.
        try:
            process = await asyncio.create_subprocess_exec(
                _adb_binary(),
                "-s",
                device_id,
                "shell",
                phone_script,
                cwd=os.getcwd(),
                env=run_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.PIPE if interactive else asyncio.subprocess.DEVNULL,
            )
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error(f"Failed to spawn adb shell process for command '{cmd_line}': {e}")
            return f"Failed to spawn adb process: {e}"

        stdout_data: list[str] = []
        try:

            async def read_limit():
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        break
                    stdout_data.append(line.decode(errors="replace"))
                await process.wait()

            await asyncio.wait_for(read_limit(), timeout=wait_seconds)
        except TimeoutError:
            return await self._hand_off_to_background(
                ctx,
                registry,
                process,
                cmd_line,
                cwd,
                terminal_id,
                interactive,
                stdout_data,
            )

        # The process completed within the wait window.
        exit_code = process.returncode
        output_text = "".join(stdout_data)

        clean_output = output_text
        if run_persistent:
            clean_output, parsed_env, script_exit = BackgroundTask.parse_output(output_text)
            if parsed_env is not None:
                registry.persistent_envs[terminal_id] = _filter_persistent_env(parsed_env)
            if script_exit is not None:
                exit_code = script_exit

        intro = f"ADB command completed with exit code {exit_code}."
        if run_persistent:
            intro += f" TerminalID: {terminal_id}."

        if _is_output_long(clean_output):
            task_id = f"task_sync_{uuid.uuid4().hex[:8]}"
            intro += f" TaskId: {task_id}."
            registry.register_finished(
                task_id=task_id,
                command=cmd_line,
                cwd=cwd,
                terminal_id=terminal_id,
                status="completed" if exit_code == 0 else "failed",
                exit_code=exit_code,
                output=clean_output,
            )
            # Already shown inline; do not re-announce it as newly finished.
            registry.finished[task_id]["notified"] = True
            return _format_long_output_response(task_id, clean_output, intro)

        return f"{intro}\nOutput:\n{clean_output}"

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    async def _hand_off_to_background(
        self,
        ctx: ArtemisContext | None,
        registry: AdbTaskRegistry,
        process: AsyncProcess,
        cmd_line: str,
        cwd: str,
        terminal_id: str | None,
        interactive: bool,
        stdout_so_far: list[str],
    ) -> str:
        """Registers a still-running process as a background task."""
        task_id = f"task_{process.pid}_{uuid.uuid4().hex[:6]}"
        trace_id = uuid.uuid4()

        bg_task = BackgroundTask(
            task_id=task_id,
            command=cmd_line,
            process=process,
            terminal_id=terminal_id,
            cwd=cwd,
            interactive=interactive,
            registry=registry,
        )
        bg_task.stdout_log.extend(stdout_so_far)
        bg_task.trace_id = trace_id
        registry.background[task_id] = bg_task

        if ctx and getattr(ctx, "data_engine", None):
            ctx.data_engine.register_background_task(
                task_id=task_id,
                summary=f"ADB Command: {cmd_line[:50]}...",
                trace_id=trace_id,
            )
            if stdout_so_far:
                ctx.data_engine.stream_output(trace_id, "".join(stdout_so_far))

        await bg_task.start(ctx)

        response_msg = f"ADB command was sent to the background as a task.\nTaskId: {task_id}\n"
        if terminal_id:
            response_msg += f"TerminalID: {terminal_id}\n"
        response_msg += "Use the 'manage_task' tool to check its status or terminate it."
        if not interactive:
            response_msg += " (stdin is closed; relaunch with Interactive=true to send input.)"
        return response_msg


# Universal tool instance & aliases
run_adb_command = RunAdbCommandTool()
RunAdbCommand = RunAdbCommandTool


def get_run_adb_command_tool(ctx: ArtemisContext) -> BaseTool:
    """Exports run_adb_command as a LangChain BaseTool."""
    return trace_langchain_tool(run_adb_command.to_langchain_tool(ctx), ctx)


# ---------------------------------------------------------------------------
# manage_task
# ---------------------------------------------------------------------------


class ManageTaskArgs(BaseModel):
    """Arguments schema for managing background ADB tasks."""

    Action: Literal["list", "list_finished", "kill", "status", "send_input"] = Field(
        ...,
        description=(
            "The action to perform: 'list' (list running tasks),"
            " 'list_finished' (list completed tasks), 'kill', 'status',"
            " 'send_input'."
        ),
    )
    TaskId: str | None = Field(
        default=None,
        description=("The task ID to manage (required for kill, status, and send_input)."),
    )
    Input: str | None = Field(
        default=None,
        description=("The input to send to the task (required when Action is 'send_input')."),
    )


class ManageTaskTool(ArtemisTool):
    """Universal tool for managing background ADB shell tasks."""

    def __init__(self):
        super().__init__(
            name="manage_task",
            description=("[SHELL] Manage background ADB shell tasks launched via run_adb_command."),
            args_schema=ManageTaskArgs,
            category="system",
        )

    # pylint: disable=too-many-arguments,too-many-locals,too-many-branches,too-many-statements,too-many-return-statements,too-many-positional-arguments
    async def execute(
        self,
        driver: BaseDeviceDriver | None = None,  # pylint: disable=unused-argument
        ctx: ArtemisContext | None = None,
        Action: Literal["list", "list_finished", "kill", "status", "send_input"] = "list",
        TaskId: str | None = None,
        Input: str | None = None,
        **kwargs: Any,
    ) -> str:
        action = Action or kwargs.get("action", "list")
        task_id = TaskId if TaskId is not None else kwargs.get("task_id")
        input_str = Input if Input is not None else kwargs.get("input")
        registry = get_adb_task_registry(ctx)

        if action == "list":
            active_tasks = []
            for tid, t in registry.background.items():
                active_tasks.append(
                    f"- {tid}: Command='{t.command[:40]}...', Phone"
                    f" Cwd='{t.cwd}', TerminalID='{t.terminal_id}',"
                    f" Interactive={t.interactive}"
                )
            if not active_tasks:
                return "No active background tasks running."
            return "Active background tasks:\n" + "\n".join(active_tasks)

        if action == "list_finished":
            finished_tasks = []
            for tid, t in registry.finished.items():
                finished_tasks.append(
                    f"- {tid}: Command='{t['command'][:40]}...', Status='{t['status']}'"
                )
            if not finished_tasks:
                return "No recently finished tasks."
            return f"Recently finished tasks (up to {_FINISHED_TASKS_CAP}):\n" + "\n".join(
                finished_tasks
            )

        if not task_id:
            return "Error: TaskId is required for status, kill, or send_input actions."

        task = registry.background.get(task_id)

        if action == "status":
            task_info = _get_task_info(task_id, ctx)
            if not task_info:
                return f"Task {task_id} not found."

            output_text = task_info.get("output", "")
            if _ENV_START_MARKER in output_text:
                output_text = output_text.split(_ENV_START_MARKER)[0]

            intro = (
                f"Task: {task_id}\n"
                f"Status: {task_info.get('status')}\n"
                f"Command: {task_info.get('command')}\n"
                f"Cwd: {task_info.get('cwd')}\n"
                f"TerminalID: {task_info.get('terminal_id')}"
            )
            exit_code = task_info.get("exit_code")
            if exit_code is not None:
                intro += f"\nExitCode: {exit_code}"

            if _is_output_long(output_text):
                return _format_long_output_response(task_id, output_text, intro)

            return f"{intro}\nAccumulated output:\n{output_text}"

        if action == "kill":
            if not task:
                return f"Task {task_id} is not active or already finished."
            try:
                await task.stop(ctx, status="killed")
                return f"Task {task_id} successfully terminated."
            except Exception as e:  # pylint: disable=broad-exception-caught
                return f"Failed to terminate task {task_id}: {e}"

        if action == "send_input":
            if not task:
                return f"Task {task_id} is not active."
            if not input_str:
                return "Error: Input is required for send_input action."
            if not task.interactive or task.process.stdin is None:
                return (
                    f"Error: Task {task_id} was launched without Interactive=true, so its"
                    " stdin is closed. Relaunch the command with Interactive=true."
                )
            try:
                task.process.stdin.write(input_str.encode())
                await task.process.stdin.drain()
                return f"Input successfully sent to task {task_id}."
            except Exception as e:  # pylint: disable=broad-exception-caught
                return f"Failed to send input: {e}"

        return "Unsupported action."


# Universal tool instance & aliases
manage_task = ManageTaskTool()
ManageTask = ManageTaskTool


def get_manage_task_tool(ctx: ArtemisContext) -> BaseTool:
    """Exports manage_task as a LangChain BaseTool."""
    return trace_langchain_tool(manage_task.to_langchain_tool(ctx), ctx)


# ---------------------------------------------------------------------------
# run_short_adb_command (Diagnoser)
# ---------------------------------------------------------------------------

_SHORT_COMMAND_TIMEOUT_SECONDS = 3.0


class RunShortAdbCommandArgs(BaseModel):
    """Arguments schema for short ADB shell commands."""

    CommandLine: str = Field(
        ...,
        description=(
            "The exact shell command line string to execute inside the Android device shell."
        ),
    )


class RunShortAdbCommandTool(ArtemisTool):
    """Universal tool for executing short, synchronous ADB shell commands."""

    def __init__(self, name: str = "run_short_adb_command"):
        super().__init__(
            name=name,
            description=(
                "[SHELL] Executes a short, synchronous shell command directly on the Android "
                "mobile device via ADB shell. Returns the command output. Times out after 3 "
                "seconds, so bound streaming commands (logcat -d, top -n 1). Do not use this "
                "tool to execute actions that change the phone state such as clicking, "
                "swiping, or navigating back."
            ),
            args_schema=RunShortAdbCommandArgs,
            category="system",
        )

    # pylint: disable=too-many-locals,too-many-return-statements
    async def execute(
        self,
        driver: BaseDeviceDriver | None = None,
        ctx: ArtemisContext | None = None,
        CommandLine: str | None = None,
        **kwargs: Any,
    ) -> str:
        cmd_line = (
            CommandLine
            if CommandLine is not None
            else (kwargs.get("command_line") or kwargs.get("command") or kwargs.get("cmd") or "")
        )
        timeout = _SHORT_COMMAND_TIMEOUT_SECONDS

        # Fallback to driver's execute_shell if no full ArtemisContext is available
        if (
            (ctx is None or not hasattr(ctx, "device") or not ctx.device)
            and driver is not None
            and hasattr(driver, "execute_shell")
        ):
            return await driver.execute_shell(cmd_line, timeout_seconds=timeout)

        device_id = None
        if ctx and hasattr(ctx, "device") and ctx.device:
            device_id = getattr(ctx.device, "device_id", None)
        elif driver and hasattr(driver, "device_id"):
            device_id = getattr(driver, "device_id", None)
        if not device_id:
            device_id = "default_device"

        run_env = os.environ.copy()
        phone_script = f"cd /data/local/tmp\n{cmd_line}\n"

        adb_client = getattr(ctx, "adb_client", None) if ctx else None
        if os.environ.get("ARTEMIS_CLOUD_MODE") == "1" or (
            adb_client is not None and hasattr(adb_client, "_bridge")
        ):
            try:
                device = get_adb_device(ctx)
                output = await asyncio.wait_for(
                    asyncio.to_thread(device.shell, phone_script),
                    timeout=timeout,
                )
                return f"ADB command completed.\nOutput:\n{output}"
            except TimeoutError:
                return f"Error: Command timed out after {timeout:.0f} seconds."
            except Exception as e:  # pylint: disable=broad-exception-caught
                return f"Error running command: {e}"

        try:
            process = await asyncio.create_subprocess_exec(
                _adb_binary(),
                "-s",
                device_id,
                "shell",
                phone_script,
                cwd=os.getcwd(),
                env=run_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
            )
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error(f"Failed to spawn adb shell process for command '{cmd_line}': {e}")
            return f"Failed to spawn adb process: {e}"

        try:
            stdout_data, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
            exit_code = process.returncode
            output_text = stdout_data.decode(errors="replace")

            return f"ADB command completed with exit code {exit_code}.\nOutput:\n{output_text}"
        except TimeoutError:
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=_TERMINATE_GRACE_SECONDS)
            except Exception:  # pylint: disable=broad-exception-caught
                try:
                    process.kill()
                except (ProcessLookupError, OSError):
                    pass
            return f"Error: Command timed out after {timeout:.0f} seconds."
        except Exception as e:  # pylint: disable=broad-exception-caught
            return f"Error running command: {e}"


# Universal tool instance & aliases
run_short_adb_command = RunShortAdbCommandTool()
RunShortAdbCommand = RunShortAdbCommandTool


def get_run_short_adb_command_tool(ctx: ArtemisContext) -> BaseTool:
    """Exports run_short_adb_command as a LangChain BaseTool."""
    return trace_langchain_tool(
        run_short_adb_command.to_langchain_tool(ctx, name="run_adb_command"),
        ctx,
    )


# Expose Wrappers
run_adb_command_wrapper = ToolWrapper(
    tool_fn_getter=get_run_adb_command_tool,
    on_success_fn=lambda output: output,
    on_failure_fn=lambda err: f"ADB command failed: {err}",
)

manage_task_wrapper = ToolWrapper(
    tool_fn_getter=get_manage_task_tool,
    on_success_fn=lambda output: output,
    on_failure_fn=lambda err: f"Task management failed: {err}",
)


# ---------------------------------------------------------------------------
# analyze_task_output
# ---------------------------------------------------------------------------


class AnalyzeTaskOutputArgs(BaseModel):
    """Arguments schema for output analysis requests."""

    TaskId: str = Field(
        ...,
        description="The task ID whose output you want to analyze.",
    )
    Query: str = Field(
        ...,
        description=(
            "The question or query to ask the output analyzer about the task's full output log."
        ),
    )


class AnalyzeTaskOutputTool(ArtemisTool):
    """Universal tool for analyzing the full, long output of an ADB task."""

    def __init__(self):
        super().__init__(
            name="analyze_task_output",
            description=(
                "[ANALYSIS] Analyzes the full, long output of a completed or running ADB task. "
                "Use this tool when you receive a message indicating that a task's output "
                "is too long and was truncated."
            ),
            args_schema=AnalyzeTaskOutputArgs,
            category="system",
        )

    async def execute(
        self,
        driver: BaseDeviceDriver | None = None,  # pylint: disable=unused-argument
        ctx: ArtemisContext | None = None,
        TaskId: str | None = None,
        Query: str | None = None,
        **kwargs: Any,
    ) -> str:
        task_id = TaskId or kwargs.get("task_id") or ""
        query = Query or kwargs.get("query") or ""

        task_info = _get_task_info(task_id, ctx)
        if not task_info:
            return f"Error: Task {task_id} not found."

        analyzer = TaskOutputAnalyzerNode(ctx)
        result = await analyzer.run(
            command=task_info.get("command", ""),
            output_text=task_info.get("output", ""),
            query=query,
        )
        return f"Analysis result for Task {task_id}:\n{result}"


# Universal tool instance & aliases
analyze_task_output = AnalyzeTaskOutputTool()
AnalyzeTaskOutput = AnalyzeTaskOutputTool


def get_analyze_task_output_tool(ctx: ArtemisContext) -> BaseTool:
    """Exports analyze_task_output as a LangChain BaseTool."""
    return trace_langchain_tool(analyze_task_output.to_langchain_tool(ctx), ctx)


analyze_task_output_wrapper = ToolWrapper(
    tool_fn_getter=get_analyze_task_output_tool,
    on_success_fn=lambda output: output,
    on_failure_fn=lambda err: f"Task output analysis failed: {err}",
)
