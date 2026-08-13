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

"""Universal ADB command and background task management tools for ARTEMIS."""

import asyncio
from asyncio.subprocess import Process as AsyncProcess
import os
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
from artemis.tools.base import ArtemisTool, ToolRegistry
from artemis.tools.tool_wrapper import ToolWrapper
from artemis.utils.logger import get_logger

logger = get_logger(__name__)

# Global persistent Android shell environments
_PERSISTENT_ENVIRONMENTS: dict[str, dict[str, str]] = {}
# Global background tasks
_BACKGROUND_TASKS: dict[str, "BackgroundTask"] = {}
# Finished tasks history logs cache (to support Action='analyze' even after tasks are unregistered)
_FINISHED_TASKS_LOGS: dict[str, dict[str, Any]] = {}


# pylint: disable=too-many-arguments,too-many-positional-arguments
def _register_finished_task(
    task_id: str,
    command: str,
    cwd: str | None,
    terminal_id: str | None,
    status: str,
    exit_code: int | None,
    output: str,
):
    """Caches execution details for finished tasks."""
    _FINISHED_TASKS_LOGS[task_id] = {
        "task_id": task_id,
        "command": command,
        "cwd": cwd,
        "terminal_id": terminal_id,
        "status": status,
        "exit_code": exit_code,
        "output": output,
        "notified": False,
    }
    # Keep memory bounded
    if len(_FINISHED_TASKS_LOGS) > 10:
        oldest_key = next(iter(_FINISHED_TASKS_LOGS))
        del _FINISHED_TASKS_LOGS[oldest_key]


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


def _get_task_info(task_id: str, ctx: ArtemisContext) -> dict[str, Any] | None:
    """Retrieves metadata and logs for a running or finished task."""
    if task_id in _BACKGROUND_TASKS:
        t = _BACKGROUND_TASKS[task_id]
        return {
            "command": t.command,
            "cwd": t.cwd,
            "terminal_id": t.terminal_id,
            "status": t.status,
            "output": "".join(t.stdout_log),
        }
    if task_id in _FINISHED_TASKS_LOGS:
        return _FINISHED_TASKS_LOGS[task_id]
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
    ):
        self.task_id = task_id
        self.command = command
        self.process = process
        self.terminal_id = terminal_id
        self.cwd = cwd
        self.stdout_log: list[str] = []
        self.status = "running"
        self.exit_code: int | None = None
        self.trace_id: uuid.UUID | None = None
        self.listener_task: asyncio.Task | None = None

    async def start(self, ctx: ArtemisContext):
        """Starts the background output listener task."""
        self.listener_task = asyncio.create_task(self._listen(ctx))

    async def _listen(self, ctx: ArtemisContext):
        """Reads process output streams and updates lifecycle status."""
        try:
            # Read stdout/stderr merged
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    break
                decoded = line.decode()
                self.stdout_log.append(decoded)
                if ctx and ctx.data_engine and getattr(self, "trace_id", None):
                    ctx.data_engine.stream_output(self.trace_id, decoded)

            self.exit_code = await self.process.wait()
            self.status = "completed" if self.exit_code == 0 else "failed"
            logger.info(
                f"Background ADB task {self.task_id} completed with exit code {self.exit_code}"
            )

            # Post-process output to extract env if persistent
            output_text = "".join(self.stdout_log)
            _, parsed_env = self.parse_output(output_text)

            if self.terminal_id and parsed_env:
                _PERSISTENT_ENVIRONMENTS[self.terminal_id] = parsed_env
                logger.info(
                    "Updated persistent env for Android terminal"
                    f" '{self.terminal_id}' from background task."
                )

            # Update status in DataEngine
            if ctx and ctx.data_engine:
                ctx.data_engine.unregister_background_task(
                    task_id=self.task_id,
                    status=self.status,
                )
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error(f"Error in background task {self.task_id} listener: {e}")
            self.status = "error"
            if ctx and ctx.data_engine:
                ctx.data_engine.unregister_background_task(self.task_id, "failed")
        finally:
            # Cache logs in memory before removing task from active tasks
            full_output = "".join(self.stdout_log)
            _register_finished_task(
                task_id=self.task_id,
                command=self.command,
                cwd=self.cwd,
                terminal_id=self.terminal_id,
                status=self.status,
                exit_code=self.exit_code,
                output=full_output,
            )
            if self.task_id in _BACKGROUND_TASKS:
                del _BACKGROUND_TASKS[self.task_id]

    @staticmethod
    def parse_output(output: str) -> tuple[str, dict[str, str] | None]:
        """Separates stdout lines from exported environment variables."""
        clean_lines = []
        env_lines = []
        in_env = False

        for line in output.splitlines():
            if "===EXIT_CODE===" in line:
                continue
            if "===ENV_START===" in line:
                in_env = True
                continue
            if in_env:
                env_lines.append(line)
            else:
                clean_lines.append(line)

        clean_output = "\n".join(clean_lines)

        parsed_env = None
        if env_lines:
            parsed_env = {}
            for el in env_lines:
                if "=" in el:
                    k, v = el.split("=", 1)
                    parsed_env[k] = v

        return clean_output, parsed_env

    # Backwards compatibility alias
    _parse_output = parse_output


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


class RunAdbCommandTool(ArtemisTool):
    """Universal tool for executing ADB shell commands on an Android device."""

    def __init__(self):
        super().__init__(
            name="run_adb_command",
            description=(
                "[SHELL] Executes a shell command directly on the Android mobile "
                "device via ADB shell. Can run synchronously or transition to a "
                "background task if execution takes longer than WaitMsBeforeAsync. "
                "Supports persistent environments (environment variables) on the "
                "phone across invocations."
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

        # Fallback to driver's execute_shell if no full ArtemisContext is available
        if (
            (ctx is None or not hasattr(ctx, "device") or not ctx.device)
            and driver is not None
            and hasattr(driver, "execute_shell")
            and not run_persistent
        ):
            return await driver.execute_shell(cmd_line)

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
        android_env_vars = {}

        if run_persistent:
            terminal_id = requested_terminal_id or f"term_{uuid.uuid4().hex[:8]}"
            if terminal_id in _PERSISTENT_ENVIRONMENTS:
                android_env_vars = _PERSISTENT_ENVIRONMENTS[terminal_id]
            else:
                _PERSISTENT_ENVIRONMENTS[terminal_id] = android_env_vars

        # Construct command to run inside adb shell
        # We inject the cached environment variables by prepending 'export K=V' declarations
        env_prepends = ""
        for k, v in android_env_vars.items():
            env_prepends += f"export {k}='{v}'\n"

        # Construct final command script
        phone_script = f"cd '{cwd}'\n" + env_prepends + f"{cmd_line}\n"
        if run_persistent:
            phone_script += (
                '_exit_code=$?\necho "===EXIT_CODE===$_exit_code"\necho "===ENV_START==="\nenv'
            )

        # In Cloud Mode or when adb client is virtualized
        adb_client = getattr(ctx, "adb_client", None) if ctx else None
        if os.environ.get("ARTEMIS_CLOUD_MODE") == "1" or (
            adb_client is not None and hasattr(adb_client, "_bridge")
        ):
            try:
                device = get_adb_device(ctx)
                output = device.shell(phone_script)
                clean_output, new_env = BackgroundTask.parse_output(output)
                if run_persistent and new_env:
                    android_env_vars.update(new_env)
                return (
                    clean_output
                    if clean_output.strip()
                    else "Command executed successfully with empty output."
                )
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.error(f"Failed to execute virtual adb shell for '{cmd_line}': {e}")
                return f"Failed to execute adb command: {e}"

        # We spawn the host subprocess: adb -s <serial> shell
        # Pass phone_script as an argument so it doesn't hold the shell open if we keep stdin open.
        try:
            process = await asyncio.create_subprocess_exec(
                "adb",
                "-s",
                device_id,
                "shell",
                phone_script,
                cwd=os.getcwd(),
                env=run_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.PIPE,
            )

        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error(f"Failed to spawn adb shell process for command '{cmd_line}': {e}")
            return f"Failed to spawn adb process: {e}"

        # Wait for wait_ms milliseconds
        wait_seconds = wait_ms / 1000.0
        try:
            stdout_data = []

            async def read_limit():
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        break
                    stdout_data.append(line.decode())
                await process.wait()

            await asyncio.wait_for(read_limit(), timeout=wait_seconds)

            # If we reached here, the process completed within the wait window
            exit_code = process.returncode
            output_text = "".join(stdout_data)

            # Clean output and extract environment
            clean_output = output_text
            parsed_env = None
            if run_persistent:
                clean_output, parsed_env = BackgroundTask.parse_output(output_text)
                if parsed_env:
                    _PERSISTENT_ENVIRONMENTS[terminal_id] = parsed_env

            intro = f"ADB command completed with exit code {exit_code}."
            if run_persistent:
                intro += f" TerminalID: {terminal_id}."

            if _is_output_long(clean_output):
                task_id = f"task_sync_{uuid.uuid4().hex[:8]}"
                intro += f" TaskId: {task_id}."
                _register_finished_task(
                    task_id=task_id,
                    command=cmd_line,
                    cwd=cwd,
                    terminal_id=terminal_id,
                    status="completed" if exit_code == 0 else "failed",
                    exit_code=exit_code,
                    output=clean_output,
                )
                return _format_long_output_response(task_id, clean_output, intro)

            return f"{intro}\nOutput:\n{clean_output}"

        except TimeoutError:
            # Process did not finish in time, move to background
            task_id = f"task_{process.pid}"
            trace_id = uuid.uuid4()

            bg_task = BackgroundTask(
                task_id=task_id,
                command=cmd_line,
                process=process,
                terminal_id=terminal_id,
                cwd=cwd,
            )
            bg_task.stdout_log = list(stdout_data)
            bg_task.trace_id = trace_id
            _BACKGROUND_TASKS[task_id] = bg_task

            # Register in DataEngine
            if ctx and ctx.data_engine:
                ctx.data_engine.register_background_task(
                    task_id=task_id,
                    summary=f"ADB Command: {cmd_line[:50]}...",
                    trace_id=trace_id,
                )
                if stdout_data:
                    ctx.data_engine.stream_output(trace_id, "".join(stdout_data))

            if ctx:
                await bg_task.start(ctx)

            response_msg = f"ADB command was sent to the background as a task.\nTaskId: {task_id}\n"
            if run_persistent:
                response_msg += f"TerminalID: {terminal_id}\n"
            response_msg += "Use the 'manage_task' tool to check its status or terminate it."
            return response_msg


# Universal tool instance & aliases
run_adb_command = RunAdbCommandTool()
RunAdbCommand = RunAdbCommandTool
ToolRegistry.register(run_adb_command)


def get_run_adb_command_tool(ctx: ArtemisContext) -> BaseTool:
    """Exports run_adb_command as a LangChain BaseTool."""
    return trace_langchain_tool(run_adb_command.to_langchain_tool(ctx), ctx)


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

        if action == "list":
            active_tasks = []
            for tid, t in _BACKGROUND_TASKS.items():
                active_tasks.append(
                    f"- {tid}: Command='{t.command[:40]}...', Phone"
                    f" Cwd='{t.cwd}', TerminalID='{t.terminal_id}'"
                )
            if not active_tasks:
                return "No active background tasks running."
            return "Active background tasks:\n" + "\n".join(active_tasks)

        if action == "list_finished":
            finished_tasks = []
            for tid, t in _FINISHED_TASKS_LOGS.items():
                finished_tasks.append(
                    f"- {tid}: Command='{t['command'][:40]}...', Status='{t['status']}'"
                )
            if not finished_tasks:
                return "No recently finished tasks."
            return "Recently finished tasks (up to 10):\n" + "\n".join(finished_tasks)

        if not task_id:
            return "Error: TaskId is required for status, kill, or send_input actions."

        task = _BACKGROUND_TASKS.get(task_id)

        if action == "status":
            task_info = _get_task_info(task_id, ctx)
            if not task_info:
                return f"Task {task_id} not found."

            output_text = task_info.get("output", "")
            if "===ENV_START===" in output_text:
                output_text = output_text.split("===ENV_START===")[0]

            intro = (
                f"Task: {task_id}\n"
                f"Status: {task_info.get('status')}\n"
                f"Command: {task_info.get('command')}\n"
                f"Cwd: {task_info.get('cwd')}\n"
                f"TerminalID: {task_info.get('terminal_id')}"
            )

            if _is_output_long(output_text):
                return _format_long_output_response(task_id, output_text, intro)

            return f"{intro}\nAccumulated output:\n{output_text}"

        if action == "kill":
            if not task:
                return f"Task {task_id} is not active or already finished."
            try:
                task.process.terminate()
                await task.process.wait()
                task.status = "killed"
                if ctx and ctx.data_engine:
                    ctx.data_engine.unregister_background_task(task_id, "killed")
                if task_id in _BACKGROUND_TASKS:
                    del _BACKGROUND_TASKS[task_id]
                return f"Task {task_id} successfully terminated."
            except Exception as e:  # pylint: disable=broad-exception-caught
                return f"Failed to terminate task {task_id}: {e}"

        if action == "send_input":
            if not task:
                return f"Task {task_id} is not active."
            if not input_str:
                return "Error: Input is required for send_input action."
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
ToolRegistry.register(manage_task)


def get_manage_task_tool(ctx: ArtemisContext) -> BaseTool:
    """Exports manage_task as a LangChain BaseTool."""
    return trace_langchain_tool(manage_task.to_langchain_tool(ctx), ctx)


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
                "seconds. Do not use this tool to execute actions that change the phone state "
                "such as clicking, swiping, or navigating back."
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

        # Fallback to driver's execute_shell if no full ArtemisContext is available
        if (
            (ctx is None or not hasattr(ctx, "device") or not ctx.device)
            and driver is not None
            and hasattr(driver, "execute_shell")
        ):
            return await driver.execute_shell(cmd_line, timeout_seconds=3.0)

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
                output = device.shell(phone_script)
                return f"ADB command completed.\nOutput:\n{output}"
            except Exception as e:  # pylint: disable=broad-exception-caught
                return f"Error running command: {e}"

        try:
            process = await asyncio.create_subprocess_exec(
                "adb",
                "-s",
                device_id,
                "shell",
                phone_script,
                cwd=os.getcwd(),
                env=run_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.PIPE,
            )
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error(f"Failed to spawn adb shell process for command '{cmd_line}': {e}")
            return f"Failed to spawn adb process: {e}"

        try:
            stdout_data, _ = await asyncio.wait_for(process.communicate(), timeout=3.0)
            exit_code = process.returncode
            output_text = stdout_data.decode()

            return f"ADB command completed with exit code {exit_code}.\nOutput:\n{output_text}"
        except TimeoutError:
            try:
                process.terminate()
                await process.wait()
            except Exception:  # pylint: disable=broad-exception-caught
                pass
            return "Error: Command timed out after 3 seconds."
        except Exception as e:  # pylint: disable=broad-exception-caught
            return f"Error running command: {e}"


# Universal tool instance & aliases
run_short_adb_command = RunShortAdbCommandTool()
RunShortAdbCommand = RunShortAdbCommandTool
ToolRegistry.register(run_short_adb_command)


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
ToolRegistry.register(analyze_task_output)


def get_analyze_task_output_tool(ctx: ArtemisContext) -> BaseTool:
    """Exports analyze_task_output as a LangChain BaseTool."""
    return trace_langchain_tool(analyze_task_output.to_langchain_tool(ctx), ctx)


analyze_task_output_wrapper = ToolWrapper(
    tool_fn_getter=get_analyze_task_output_tool,
    on_success_fn=lambda output: output,
    on_failure_fn=lambda err: f"Task output analysis failed: {err}",
)
