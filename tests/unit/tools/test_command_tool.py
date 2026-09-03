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

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from artemis.context import ArtemisContext
from artemis.tools.command_tool import (
    get_adb_task_registry,
    get_manage_task_tool,
    get_run_adb_command_tool,
    get_run_short_adb_command_tool,
)
import pytest


class MockProcess:
    def __init__(
        self,
        output_bytes: bytes,
        exit_code: int = 0,
        pid: int = 12345,
        delay: float = 0,
    ):
        self.output_bytes = output_bytes
        self.exit_code = exit_code
        self.pid = pid
        self.returncode = None
        self._read_offset = 0
        self.delay = delay

        self.stdin = MagicMock()
        self.stdin.write = MagicMock()
        self.stdin.drain = AsyncMock()
        self.stdin.close = MagicMock()

        self.stdout = MagicMock()
        self.stdout.readline = AsyncMock(side_effect=self._readline)

    async def _readline(self) -> bytes:
        if self.delay > 0:
            await asyncio.sleep(self.delay)
            self.delay = 0  # Only delay the first read

        if self._read_offset >= len(self.output_bytes):
            self.returncode = self.exit_code
            return b""

        idx = self.output_bytes.find(b"\n", self._read_offset)
        if idx == -1:
            line = self.output_bytes[self._read_offset :]
            self._read_offset = len(self.output_bytes)
        else:
            line = self.output_bytes[self._read_offset : idx + 1]
            self._read_offset = idx + 1
        return line

    async def wait(self) -> int:
        self.returncode = self.exit_code
        return self.exit_code

    def terminate(self):
        self.returncode = -15
        self.exit_code = -15

    async def communicate(self) -> tuple[bytes, bytes]:
        lines = []
        while True:
            line = await self._readline()
            if not line:
                break
            lines.append(line)
        self.returncode = self.exit_code
        return b"".join(lines), b""


@pytest.fixture
def mock_ctx():
    ctx = MagicMock(spec=ArtemisContext)
    ctx.data_engine = None
    ctx.device = MagicMock()
    ctx.device.device_id = "test_device_1234"
    return ctx


@pytest.mark.asyncio
@patch("asyncio.create_subprocess_exec")
async def test_run_command_sync(mock_exec, mock_ctx):
    # Mock subprocess output
    mock_process = MockProcess(
        output_bytes=b"hello world\n===EXIT_CODE===0\n===ENV_START===\n",
        exit_code=0,
    )
    mock_exec.return_value = mock_process

    run_command = get_run_adb_command_tool(mock_ctx)

    result = await run_command.ainvoke(
        {
            "CommandLine": "echo 'hello world'",
            "Cwd": "/data/local/tmp",
            "RunPersistent": False,
            "WaitMsBeforeAsync": 500,
        }
    )

    assert "completed with exit code 0" in result
    assert "hello world" in result
    mock_exec.assert_called_once()
    # Check that adb was called with device ID and "shell"
    args, kwargs = mock_exec.call_args
    assert args[0].lower().replace(".exe", "").endswith("adb")
    assert args[1] == "-s"
    assert args[2] == "test_device_1234"
    assert args[3] == "shell"


@pytest.mark.asyncio
@patch("asyncio.create_subprocess_exec")
async def test_run_command_persistent_env(mock_exec, mock_ctx):
    run_command = get_run_adb_command_tool(mock_ctx)
    terminal_id = "test_term_android"

    # 1. Set environment variable in persistent terminal
    mock_process_1 = MockProcess(
        output_bytes=(b"===EXIT_CODE===0\n===ENV_START===\nARTEMIS_TEST_ENV=coffee\n"),
        exit_code=0,
    )
    mock_exec.return_value = mock_process_1

    res1 = await run_command.ainvoke(
        {
            "CommandLine": "export ARTEMIS_TEST_ENV=coffee",
            "Cwd": "/data/local/tmp",
            "RunPersistent": True,
            "RequestedTerminalID": terminal_id,
            "WaitMsBeforeAsync": 500,
        }
    )
    assert "completed with exit code 0" in res1
    envs = get_adb_task_registry(mock_ctx).persistent_envs
    assert terminal_id in envs
    assert envs[terminal_id].get("ARTEMIS_TEST_ENV") == "coffee"

    # 2. Query environment variable in subsequent command on same terminal
    mock_process_2 = MockProcess(
        output_bytes=b"coffee\n===EXIT_CODE===0\n===ENV_START===\nARTEMIS_TEST_ENV=coffee\n",
        exit_code=0,
    )
    mock_exec.return_value = mock_process_2

    res2 = await run_command.ainvoke(
        {
            "CommandLine": "echo $ARTEMIS_TEST_ENV",
            "Cwd": "/data/local/tmp",
            "RunPersistent": True,
            "RequestedTerminalID": terminal_id,
            "WaitMsBeforeAsync": 500,
        }
    )
    assert "completed with exit code 0" in res2
    assert "coffee" in res2


@pytest.mark.asyncio
@patch("asyncio.create_subprocess_exec")
async def test_run_command_async_and_status(mock_exec, mock_ctx):
    # Mock a long running command by delaying stdout response
    mock_process = MockProcess(
        output_bytes=(b"background task finished\n===EXIT_CODE===0\n===ENV_START===\n"),
        exit_code=0,
        delay=1.0,  # 1.0s delay will trigger timeout
    )
    mock_exec.return_value = mock_process

    run_command = get_run_adb_command_tool(mock_ctx)
    manage_task = get_manage_task_tool(mock_ctx)

    registry = get_adb_task_registry(mock_ctx)
    registry.background.clear()

    res = await run_command.ainvoke(
        {
            "CommandLine": "sleep 2 && echo 'background task finished'",
            "Cwd": "/data/local/tmp",
            "RunPersistent": False,
            "WaitMsBeforeAsync": 100,  # 100ms wait threshold
        }
    )

    assert "sent to the background as a task" in res
    assert "TaskId: task_" in res

    lines = res.splitlines()
    task_id_line = [line for line in lines if line.startswith("TaskId: ")][0]
    task_id = task_id_line.split(": ")[1]

    assert task_id in registry.background

    # Check status (should be running)
    status_res = await manage_task.ainvoke(
        {
            "Action": "status",
            "TaskId": task_id,
        }
    )
    assert "Status: running" in status_res

    # Wait for the background listener task to finish reading delayed output
    await asyncio.sleep(1.2)

    # Status should be completed (and task removed from active map)
    assert task_id not in registry.background


@pytest.mark.asyncio
@patch("asyncio.create_subprocess_exec")
async def test_run_command_kill(mock_exec, mock_ctx):
    mock_process = MockProcess(output_bytes=b"", exit_code=0, delay=5.0)
    mock_exec.return_value = mock_process

    run_command = get_run_adb_command_tool(mock_ctx)
    manage_task = get_manage_task_tool(mock_ctx)

    registry = get_adb_task_registry(mock_ctx)
    registry.background.clear()

    res = await run_command.ainvoke(
        {
            "CommandLine": "sleep 10",
            "Cwd": "/data/local/tmp",
            "RunPersistent": False,
            "WaitMsBeforeAsync": 100,
        }
    )

    lines = res.splitlines()
    task_id_line = [line for line in lines if line.startswith("TaskId: ")][0]
    task_id = task_id_line.split(": ")[1]

    # Kill the task
    kill_res = await manage_task.ainvoke(
        {
            "Action": "kill",
            "TaskId": task_id,
        }
    )
    assert f"Task {task_id} successfully terminated" in kill_res
    assert task_id not in registry.background


@pytest.mark.asyncio
@patch("asyncio.create_subprocess_exec")
async def test_run_short_command_success(mock_exec, mock_ctx):
    mock_process = MockProcess(output_bytes=b"short command success output", exit_code=0)
    mock_exec.return_value = mock_process

    run_short_command = get_run_short_adb_command_tool(mock_ctx)

    result = await run_short_command.ainvoke(
        {
            "CommandLine": "echo 'short command'",
        }
    )

    assert "completed with exit code 0" in result
    assert "short command success output" in result
    mock_exec.assert_called_once()
    args, kwargs = mock_exec.call_args
    assert args[0].lower().replace(".exe", "").endswith("adb")
    assert args[1] == "-s"
    assert args[2] == "test_device_1234"
    assert args[3] == "shell"
    # Verify it cd'd to /data/local/tmp by default
    assert "cd /data/local/tmp" in args[4]


@pytest.mark.asyncio
@patch("asyncio.create_subprocess_exec")
async def test_run_short_command_timeout(mock_exec, mock_ctx):
    mock_process = MockProcess(output_bytes=b"some output", exit_code=0, delay=5.0)
    mock_exec.return_value = mock_process

    run_short_command = get_run_short_adb_command_tool(mock_ctx)

    result = await run_short_command.ainvoke(
        {
            "CommandLine": "sleep 5",
        }
    )

    assert "Error: Command timed out after 3 seconds." in result


@pytest.mark.asyncio
@patch("asyncio.create_subprocess_exec")
async def test_run_command_sync_long_output(mock_exec, mock_ctx):
    # Generate long output (> 500 lines)
    long_output = "\n".join(f"Output line {i}" for i in range(1, 600))
    mock_process = MockProcess(
        output_bytes=long_output.encode() + b"\n===EXIT_CODE===0\n===ENV_START===\n",
        exit_code=0,
    )
    mock_exec.return_value = mock_process

    run_command = get_run_adb_command_tool(mock_ctx)
    result = await run_command.ainvoke(
        {
            "CommandLine": "generate_long_output",
            "Cwd": "/data/local/tmp",
            "RunPersistent": False,
            "WaitMsBeforeAsync": 500,
        }
    )

    assert "ADB command completed with exit code 0." in result
    assert "has been truncated" in result
    assert "Output line 599" in result
    assert "Output line 1" not in result  # since it was truncated to last 200 lines
    assert "analyze_task_output" in result
    assert "TaskId: task_sync_" in result


@pytest.mark.asyncio
@patch("artemis.agents.log_analyzer.output_analyzer.get_llm")
async def test_analyze_task_output_tool(mock_get_llm, mock_ctx):
    # Set up mock LLM
    mock_llm = MagicMock()
    mock_response = MagicMock()
    mock_response.content = "This is the analysis result: everything looks good."
    mock_response.tool_calls = []
    mock_llm.bind_tools.return_value = mock_llm
    mock_llm.ainvoke = AsyncMock(return_value=mock_response)
    mock_get_llm.return_value = mock_llm

    # Set up finished tasks logs cache manually
    from artemis.tools.command_tool import (
        _FINISHED_TASKS_LOGS,
        _register_finished_task,
        get_analyze_task_output_tool,
    )

    task_id = "test_task_long_output"
    _register_finished_task(
        task_id=task_id,
        command="cat long_log.txt",
        cwd="/data/local/tmp",
        terminal_id=None,
        status="completed",
        exit_code=0,
        output="line1\nline2\n" + "\n".join(f"line{i}" for i in range(3, 50)),
    )

    analyze_tool = get_analyze_task_output_tool(mock_ctx)
    result = await analyze_tool.ainvoke({"TaskId": task_id, "Query": "Summarize the log"})

    assert "Analysis result for Task test_task_long_output:" in result
    assert "This is the analysis result: everything looks good." in result
    mock_get_llm.assert_called_once_with(ctx=mock_ctx, name="output_analyzer")

    # Clean up registry
    if task_id in _FINISHED_TASKS_LOGS:
        del _FINISHED_TASKS_LOGS[task_id]


def test_run_adb_command_tool_subclass_and_registry():
    """Verify RunAdbCommandTool is a subclass of ArtemisTool and properly registered."""
    from artemis.tools.base import ArtemisTool, ToolRegistry
    from artemis.tools.command_tool import (
        RunAdbCommand,
        RunAdbCommandArgs,
        RunAdbCommandTool,
        run_adb_command,
    )

    assert issubclass(RunAdbCommandTool, ArtemisTool)
    assert issubclass(RunAdbCommand, ArtemisTool)
    assert isinstance(run_adb_command, ArtemisTool)
    assert isinstance(run_adb_command, RunAdbCommandTool)

    assert run_adb_command.name == "run_adb_command"
    assert run_adb_command.category == "system"
    assert run_adb_command.args_schema == RunAdbCommandArgs

    # Registry lookup
    reg_tool = ToolRegistry.get("run_adb_command")
    assert reg_tool is not None
    assert isinstance(reg_tool, RunAdbCommandTool)

    # GenAI FunctionDeclaration export
    declaration = run_adb_command.to_genai_declaration()
    assert declaration.name == "run_adb_command"
    assert "CommandLine" in declaration.parameters.properties


@pytest.mark.asyncio
async def test_run_adb_command_tool_mock_driver_execute():
    """Verify RunAdbCommandTool.execute dispatches to MockDeviceDriver when ctx is None."""
    from artemis.drivers.mock.mock_driver import MockDeviceDriver
    from artemis.tools.command_tool import RunAdbCommandTool

    driver = MockDeviceDriver(width=1080, height=2400)
    tool_inst = RunAdbCommandTool()

    result = await tool_inst.execute(
        driver=driver,
        ctx=None,
        CommandLine="pm list packages",
    )
    assert "mock_output: pm list packages" in result
    assert "pm list packages" in driver.action_history[-1]["command"]


def test_manage_task_tool_subclass_and_registry():
    """Verify ManageTaskTool is a subclass of ArtemisTool and properly registered."""
    from artemis.tools.base import ArtemisTool, ToolRegistry
    from artemis.tools.command_tool import (
        ManageTask,
        ManageTaskArgs,
        ManageTaskTool,
        manage_task,
    )

    assert issubclass(ManageTaskTool, ArtemisTool)
    assert issubclass(ManageTask, ArtemisTool)
    assert isinstance(manage_task, ArtemisTool)
    assert isinstance(manage_task, ManageTaskTool)

    assert manage_task.name == "manage_task"
    assert manage_task.category == "system"
    assert manage_task.args_schema == ManageTaskArgs

    # Registry lookup
    reg_tool = ToolRegistry.get("manage_task")
    assert reg_tool is not None
    assert isinstance(reg_tool, ManageTaskTool)

    # GenAI FunctionDeclaration export
    declaration = manage_task.to_genai_declaration()
    assert declaration.name == "manage_task"
    assert "Action" in declaration.parameters.properties


@pytest.mark.asyncio
async def test_manage_task_tool_direct_execute(mock_ctx):
    """Verify direct ManageTaskTool.execute execution."""
    from artemis.tools.command_tool import (
        BackgroundTask,
        manage_task,
    )

    registry = get_adb_task_registry(mock_ctx)
    registry.background.clear()
    list_res = await manage_task.execute(ctx=mock_ctx, Action="list")
    assert "No active background tasks" in list_res

    # Create dummy task
    dummy_proc = MagicMock()
    bg_task = BackgroundTask(
        task_id="dummy_task_99",
        command="sleep 100",
        process=dummy_proc,
        terminal_id="term_1",
        cwd="/data/local/tmp",
    )
    registry.background["dummy_task_99"] = bg_task

    list_res2 = await manage_task.execute(ctx=mock_ctx, Action="list")
    assert "dummy_task_99" in list_res2
    assert "sleep 100" in list_res2

    # Status check
    status_res = await manage_task.execute(ctx=mock_ctx, Action="status", TaskId="dummy_task_99")
    assert "Status: running" in status_res
    assert "dummy_task_99" in status_res

    registry = get_adb_task_registry(mock_ctx)
    registry.background.clear()


def test_analyze_task_output_tool_subclass_and_registry():
    """Verify AnalyzeTaskOutputTool is a subclass of ArtemisTool and properly registered."""
    from artemis.tools.base import ArtemisTool, ToolRegistry
    from artemis.tools.command_tool import (
        AnalyzeTaskOutput,
        AnalyzeTaskOutputArgs,
        AnalyzeTaskOutputTool,
        analyze_task_output,
    )

    assert issubclass(AnalyzeTaskOutputTool, ArtemisTool)
    assert issubclass(AnalyzeTaskOutput, ArtemisTool)
    assert isinstance(analyze_task_output, ArtemisTool)
    assert isinstance(analyze_task_output, AnalyzeTaskOutputTool)

    assert analyze_task_output.name == "analyze_task_output"
    assert analyze_task_output.category == "system"
    assert analyze_task_output.args_schema == AnalyzeTaskOutputArgs

    # Registry lookup
    reg_tool = ToolRegistry.get("analyze_task_output")
    assert reg_tool is not None
    assert isinstance(reg_tool, AnalyzeTaskOutputTool)

    # GenAI FunctionDeclaration export
    declaration = analyze_task_output.to_genai_declaration()
    assert declaration.name == "analyze_task_output"
    assert "TaskId" in declaration.parameters.properties
    assert "Query" in declaration.parameters.properties


@pytest.mark.asyncio
@patch("artemis.agents.log_analyzer.output_analyzer.get_llm")
async def test_analyze_task_output_tool_direct_execute(mock_get_llm, mock_ctx):
    """Verify direct AnalyzeTaskOutputTool.execute execution."""
    from artemis.tools.command_tool import (
        _FINISHED_TASKS_LOGS,
        _register_finished_task,
        analyze_task_output,
    )

    mock_llm = MagicMock()
    mock_response = MagicMock()
    mock_response.content = "Summary of task output."
    mock_response.tool_calls = []
    mock_llm.bind_tools.return_value = mock_llm
    mock_llm.ainvoke = AsyncMock(return_value=mock_response)
    mock_get_llm.return_value = mock_llm

    task_id = "test_direct_analyze"
    _register_finished_task(
        task_id=task_id,
        command="cat file.txt",
        cwd="/data/local/tmp",
        terminal_id=None,
        status="completed",
        exit_code=0,
        output="Sample log content",
    )

    result = await analyze_task_output.execute(
        ctx=mock_ctx, TaskId=task_id, Query="What is the content?"
    )
    assert "Analysis result for Task test_direct_analyze:" in result
    assert "Summary of task output." in result

    if task_id in _FINISHED_TASKS_LOGS:
        del _FINISHED_TASKS_LOGS[task_id]


def test_run_short_adb_command_tool_subclass_and_registry():
    """Verify RunShortAdbCommandTool is a subclass of ArtemisTool and properly registered."""
    from artemis.tools.base import ArtemisTool, ToolRegistry
    from artemis.tools.command_tool import (
        RunShortAdbCommand,
        RunShortAdbCommandArgs,
        RunShortAdbCommandTool,
        run_short_adb_command,
    )

    assert issubclass(RunShortAdbCommandTool, ArtemisTool)
    assert issubclass(RunShortAdbCommand, ArtemisTool)
    assert isinstance(run_short_adb_command, ArtemisTool)
    assert isinstance(run_short_adb_command, RunShortAdbCommandTool)

    assert run_short_adb_command.name == "run_short_adb_command"
    assert run_short_adb_command.category == "system"
    assert run_short_adb_command.args_schema == RunShortAdbCommandArgs

    # Registry lookup
    reg_tool = ToolRegistry.get("run_short_adb_command")
    assert reg_tool is not None
    assert isinstance(reg_tool, RunShortAdbCommandTool)

    # GenAI FunctionDeclaration export
    declaration = run_short_adb_command.to_genai_declaration()
    assert declaration.name == "run_short_adb_command"
    assert "CommandLine" in declaration.parameters.properties


@pytest.mark.asyncio
async def test_run_short_adb_command_tool_mock_driver_execute():
    """Verify RunShortAdbCommandTool.execute dispatches to MockDeviceDriver when ctx is None."""
    from artemis.drivers.mock.mock_driver import MockDeviceDriver
    from artemis.tools.command_tool import RunShortAdbCommandTool

    driver = MockDeviceDriver(width=1080, height=2400)
    tool_inst = RunShortAdbCommandTool()

    result = await tool_inst.execute(
        driver=driver,
        ctx=None,
        CommandLine="getprop ro.build.version.release",
    )
    assert "mock_output: getprop ro.build.version.release" in result
    assert "getprop ro.build.version.release" in driver.action_history[-1]["command"]


# ---------------------------------------------------------------------------
# Registry, exit codes, stdin control, cloud timeout, shutdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("asyncio.create_subprocess_exec")
async def test_persistent_reports_script_exit_code(mock_exec, mock_ctx):
    """A failing command in a persistent terminal must not report exit code 0."""
    mock_exec.return_value = MockProcess(
        output_bytes=b"boom\n===EXIT_CODE===3\n===ENV_START===\nPATH=/bin\nFOO=bar baz\n",
        exit_code=0,  # the trailing `env` succeeds, the command did not
    )
    run_command = get_run_adb_command_tool(mock_ctx)
    res = await run_command.ainvoke(
        {
            "CommandLine": "false",
            "RunPersistent": True,
            "RequestedTerminalID": "term_exit",
            "WaitMsBeforeAsync": 500,
        }
    )
    assert "completed with exit code 3" in res
    assert "===EXIT_CODE===" not in res
    envs = get_adb_task_registry(mock_ctx).persistent_envs["term_exit"]
    assert envs == {"FOO": "bar baz"}  # platform vars like PATH are dropped


def test_build_phone_script_quotes_env_and_cwd():
    import shlex

    from artemis.tools.command_tool import _build_phone_script

    script = _build_phone_script("echo hi", "/sdcard/my dir", {"FOO": "it's"}, True)
    assert script.startswith("cd '/sdcard/my dir'\n")
    expected_export = "export FOO=" + shlex.quote("it's") + "\n"
    assert expected_export in script
    assert "===EXIT_CODE===$_artemis_ec" in script
    assert script.rstrip().endswith("env")


@pytest.mark.asyncio
@patch("asyncio.create_subprocess_exec")
async def test_stdin_closed_unless_interactive(mock_exec, mock_ctx):
    mock_exec.return_value = MockProcess(output_bytes=b"ok\n", exit_code=0)
    run_command = get_run_adb_command_tool(mock_ctx)

    await run_command.ainvoke({"CommandLine": "cat", "WaitMsBeforeAsync": 500})
    assert mock_exec.call_args.kwargs["stdin"] == asyncio.subprocess.DEVNULL

    mock_exec.return_value = MockProcess(output_bytes=b"ok\n", exit_code=0)
    await run_command.ainvoke({"CommandLine": "cat", "Interactive": True, "WaitMsBeforeAsync": 500})
    assert mock_exec.call_args.kwargs["stdin"] == asyncio.subprocess.PIPE


@pytest.mark.asyncio
@patch("asyncio.create_subprocess_exec")
async def test_send_input_rejected_for_non_interactive_task(mock_exec, mock_ctx):
    mock_exec.return_value = MockProcess(output_bytes=b"", exit_code=0, delay=5.0)
    run_command = get_run_adb_command_tool(mock_ctx)
    manage_task = get_manage_task_tool(mock_ctx)
    registry = get_adb_task_registry(mock_ctx)

    res = await run_command.ainvoke({"CommandLine": "sleep 10", "WaitMsBeforeAsync": 100})
    task_id = [ln for ln in res.splitlines() if ln.startswith("TaskId: ")][0].split(": ")[1]
    assert "stdin is closed" in res

    reply = await manage_task.ainvoke({"Action": "send_input", "TaskId": task_id, "Input": "y\n"})
    assert "Interactive=true" in reply

    await manage_task.ainvoke({"Action": "kill", "TaskId": task_id})
    assert task_id not in registry.background


@pytest.mark.asyncio
@patch("asyncio.create_subprocess_exec")
async def test_shutdown_kills_background_tasks_and_records_them(mock_exec, mock_ctx):
    from artemis.tools.command_tool import shutdown_adb_background_tasks

    mock_exec.return_value = MockProcess(output_bytes=b"", exit_code=0, delay=5.0)
    run_command = get_run_adb_command_tool(mock_ctx)
    registry = get_adb_task_registry(mock_ctx)

    res = await run_command.ainvoke({"CommandLine": "logcat", "WaitMsBeforeAsync": 100})
    task_id = [ln for ln in res.splitlines() if ln.startswith("TaskId: ")][0].split(": ")[1]
    assert task_id in registry.background

    killed = await shutdown_adb_background_tasks(mock_ctx)
    assert killed == 1
    assert registry.background == {}
    assert registry.finished[task_id]["status"] == "killed"
    # A second shutdown is a no-op.
    assert await shutdown_adb_background_tasks(mock_ctx) == 0
    # A context that never used ADB tools has no registry and is a no-op too.
    fresh = MagicMock(spec=ArtemisContext)
    assert await shutdown_adb_background_tasks(fresh) == 0


def test_registry_is_per_context_and_notifications_do_not_leak():
    from artemis.tools.command_tool import _register_finished_task

    ctx_a = MagicMock(spec=ArtemisContext)
    ctx_b = MagicMock(spec=ArtemisContext)
    reg_a = get_adb_task_registry(ctx_a)
    reg_b = get_adb_task_registry(ctx_b)
    assert reg_a is not reg_b
    assert get_adb_task_registry(ctx_a) is reg_a

    _register_finished_task("t_a", "logcat -d", None, None, "completed", 0, "out", ctx=ctx_a)
    assert [t["task_id"] for t in reg_a.pop_unnotified_finished()] == ["t_a"]
    assert reg_b.pop_unnotified_finished() == []
    # Consumed once: the next Operator turn on ctx_a does not see it again.
    assert reg_a.pop_unnotified_finished() == []


@pytest.mark.asyncio
@patch("asyncio.create_subprocess_exec")
async def test_sync_long_output_is_not_reannounced(mock_exec, mock_ctx):
    long_output = "\n".join(f"line {i}" for i in range(600))
    mock_exec.return_value = MockProcess(output_bytes=long_output.encode(), exit_code=0)
    run_command = get_run_adb_command_tool(mock_ctx)
    registry = get_adb_task_registry(mock_ctx)
    registry.finished.clear()

    res = await run_command.ainvoke({"CommandLine": "dumpsys", "WaitMsBeforeAsync": 500})
    assert "has been truncated" in res
    assert registry.pop_unnotified_finished() == []
    # ... but analyze_task_output can still reach the full text.
    task_id = res.split("TaskId: ")[1].split(".")[0]
    assert "line 0" in registry.get_task_info(task_id)["output"]


@pytest.mark.asyncio
async def test_cloud_mode_command_has_hard_timeout(mock_ctx, monkeypatch):
    import time

    monkeypatch.setenv("ARTEMIS_CLOUD_MODE", "1")
    device = MagicMock()
    device.shell = MagicMock(side_effect=lambda _script: time.sleep(1.0) or "late")
    with patch("artemis.tools.command_tool.get_adb_device", return_value=device):
        run_command = get_run_adb_command_tool(mock_ctx)
        res = await run_command.ainvoke({"CommandLine": "logcat", "WaitMsBeforeAsync": 100})
    assert "did not finish within" in res

    device.shell = MagicMock(return_value="fast\n")
    with patch("artemis.tools.command_tool.get_adb_device", return_value=device):
        res = await run_command.ainvoke({"CommandLine": "echo fast", "WaitMsBeforeAsync": 500})
    assert "fast" in res
