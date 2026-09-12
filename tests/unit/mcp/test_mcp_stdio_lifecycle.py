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

"""Unit and integration tests verifying MCP stdio handshake reliability,

subprocess stdin isolation against pipe hijacking, and stdout purity.
"""

import io
import json
import os
import queue
import subprocess
import sys
import threading
from unittest.mock import MagicMock, patch

from artemis.clients import ui_automator_client
from artemis.runtime.awake_lease import ScreenAwakeLease
from artemis.runtime.awake_service import _run_awake_adb_command
from artemis.utils.logger import get_logger
from mcp_server.notifiers.agentapi import AgentApiNotifier
from mcp_server.notifiers.desktop import (
    DesktopNotifier,
    escape_applescript_string,
    escape_powershell_string,
)
from mcp_server.notifiers.script import ScriptNotifier
from mcp_server.utils import device_utils, env_utils


def _readline_with_timeout(pipe, timeout: float) -> str | None:
    """Read one line from a subprocess pipe with a timeout, portably.

    select.select only supports sockets on Windows, so poll via a reader thread
    instead of selecting on the pipe handle.
    """
    result: queue.Queue = queue.Queue()

    def _reader():
        try:
            result.put(pipe.readline())
        except Exception:
            result.put(b"")

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()
    try:
        line = result.get(timeout=timeout)
    except queue.Empty:
        return None
    if not line:
        return None
    return line.decode("utf-8").strip()


def test_mcp_stdio_handshake_immediate_input():
    """Verify that MCP server over stdio responds to an immediate initialize request

    without hanging, even when stdin data is pushed concurrently with process startup.
    """
    project_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    python_exe = sys.executable

    p = subprocess.Popen(
        [python_exe, "-m", "mcp_server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            "PYTHONUNBUFFERED": "1",
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": project_root,
            # Windows: the interpreter's socket stack (Winsock/_overlapped),
            # Path.home(), and tempfile handling need these system variables.
            **{
                key: os.environ[key]
                for key in (
                    "SYSTEMROOT",
                    "SYSTEMDRIVE",
                    "WINDIR",
                    "TEMP",
                    "TMP",
                    "USERPROFILE",
                    "HOMEDRIVE",
                    "HOMEPATH",
                    "APPDATA",
                    "LOCALAPPDATA",
                )
                if key in os.environ
            },
        },
        cwd=project_root,
    )

    try:
        # Immediately write the JSON-RPC initialize request into stdin
        init_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0.0"},
            },
        }
        p.stdin.write(json.dumps(init_req).encode("utf-8") + b"\n")
        p.stdin.flush()

        # Read initialize response with a strict timeout
        init_resp_line = _readline_with_timeout(p.stdout, 6.0)

        assert init_resp_line is not None, (
            "MCP server failed to respond to initialize request within 6 seconds (deadlock detected)!"
        )
        init_data = json.loads(init_resp_line)
        assert init_data.get("id") == 1
        assert "result" in init_data

        # Follow up with initialized notification and tools/list request
        notif = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        tools_req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        p.stdin.write(json.dumps(notif).encode("utf-8") + b"\n")
        p.stdin.write(json.dumps(tools_req).encode("utf-8") + b"\n")
        p.stdin.flush()

        tools_resp_line = _readline_with_timeout(p.stdout, 4.0)

        assert tools_resp_line is not None, "MCP server failed to respond to tools/list request!"
        tools_data = json.loads(tools_resp_line)
        assert tools_data.get("id") == 2
        tools = tools_data.get("result", {}).get("tools", [])
        tool_names = {t["name"] for t in tools}
        assert {
            "mobile_run_task",
            "mobile_manage_task",
            "mobile_get_device_state",
            "mobile_inspect_trace",
            "mobile_diagnose",
        }.issubset(tool_names)

    finally:
        p.terminate()
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()


def test_awake_service_adb_command_isolates_stdin():
    """Verify _run_awake_adb_command always sets stdin=subprocess.DEVNULL."""
    with (
        patch("artemis.runtime.adb_endpoint.toolchain.resolve", return_value="/mock/adb"),
        patch("artemis.runtime.awake_service.subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        _run_awake_adb_command("test-dev-1", ["shell", "date"], "test command")

        assert mock_run.called
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("stdin") == subprocess.DEVNULL, (
            "Expected stdin=subprocess.DEVNULL to prevent stdin hijacking!"
        )


def test_awake_lease_run_isolates_stdin():
    """Verify ScreenAwakeLease._run always sets stdin=subprocess.DEVNULL."""
    lease = ScreenAwakeLease("test-dev-1")
    with (
        patch("artemis.runtime.adb_endpoint.toolchain.resolve", return_value="/mock/adb"),
        patch("artemis.runtime.awake_lease.subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        lease._run(["shell", "date"], "test lease command")

        assert mock_run.called
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("stdin") == subprocess.DEVNULL, (
            "Expected stdin=subprocess.DEVNULL to prevent stdin hijacking!"
        )


def test_detached_process_kwargs_isolates_stdin():
    """Verify get_detached_process_kwargs always sets stdin=subprocess.DEVNULL."""
    kwargs = env_utils.get_detached_process_kwargs()
    assert kwargs.get("stdin") == subprocess.DEVNULL, (
        "Expected stdin=subprocess.DEVNULL for detached background tasks!"
    )


def test_device_utils_isolates_stdin():
    """Verify device_utils subprocess calls always set stdin=subprocess.DEVNULL."""
    with patch("mcp_server.utils.device_utils.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0, stdout="List of devices attached\n", stderr=""
        )
        device_utils.get_connected_devices()
        assert mock_run.called
        assert mock_run.call_args.kwargs.get("stdin") == subprocess.DEVNULL


def test_ui_automator_client_isolates_stdin():
    """Verify ui_automator_client screencap commands isolate stdin."""
    client = ui_automator_client.UIAutomatorClient("dev-1")
    with (
        patch(
            "artemis.clients.ui_automator_client.adb_command", return_value=["adb", "-s", "dev-1"]
        ),
        patch("artemis.clients.ui_automator_client.subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"", stderr="")
        with patch("artemis.clients.ui_automator_client.Image.open"):
            client.get_screenshot()
        assert mock_run.called
        assert mock_run.call_args.kwargs.get("stdin") == subprocess.DEVNULL


def test_agentapi_notifier_isolates_stdin():
    """Verify AgentApiNotifier subprocess calls always set stdin=subprocess.DEVNULL."""
    notifier = AgentApiNotifier()
    with (
        patch.object(notifier, "_find_agentapi_path", return_value="/usr/local/bin/agentapi"),
        patch.object(
            notifier, "_get_candidate_envs", return_value=[("127.0.0.1:1234", "dummy_token")]
        ),
        patch.object(notifier, "_save_shared_env"),
        patch("mcp_server.notifiers.agentapi.subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout="{}", stderr="")
        res = notifier.notify("test-conv-id", "Hello from Artemis", title="Test Header")
        assert res is True
        assert mock_run.called
        assert mock_run.call_args.kwargs.get("stdin") == subprocess.DEVNULL


def test_desktop_notifier_isolates_stdin_linux():
    """Verify DesktopNotifier on Linux sets stdin=subprocess.DEVNULL."""
    notifier = DesktopNotifier()
    with (
        patch("mcp_server.notifiers.desktop.os.getenv", return_value="1"),
        patch("mcp_server.notifiers.desktop.sys.platform", "linux"),
        patch("mcp_server.notifiers.desktop.shutil.which", return_value="/usr/bin/notify-send"),
        patch("mcp_server.notifiers.desktop.subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0)
        res = notifier.notify("cid", "Hello Linux", title="Test Linux")
        assert res is True
        assert mock_run.called
        assert mock_run.call_args.kwargs.get("stdin") == subprocess.DEVNULL


def test_desktop_notifier_isolates_stdin_darwin():
    """Verify DesktopNotifier on macOS sets stdin=subprocess.DEVNULL and invokes osascript."""
    notifier = DesktopNotifier()
    with (
        patch("mcp_server.notifiers.desktop.os.getenv", return_value="1"),
        patch("mcp_server.notifiers.desktop.sys.platform", "darwin"),
        patch("mcp_server.notifiers.desktop.shutil.which", return_value="/usr/bin/osascript"),
        patch("mcp_server.notifiers.desktop.subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0)
        res = notifier.notify(
            "cid", 'Notification with "quotes" and \\backslashes\\', title="macOS Title"
        )
        assert res is True
        assert mock_run.called
        assert mock_run.call_args.kwargs.get("stdin") == subprocess.DEVNULL
        # Verify script escaping in command arguments
        cmd = mock_run.call_args.args[0]
        assert cmd[0] == "osascript"
        assert cmd[1] == "-e"
        assert '\\"quotes\\"' in cmd[2]
        assert "\\\\backslashes\\\\" in cmd[2]


def test_desktop_notifier_isolates_stdin_windows():
    """Verify DesktopNotifier on Windows sets stdin=subprocess.DEVNULL."""
    notifier = DesktopNotifier()
    with (
        patch("mcp_server.notifiers.desktop.os.getenv", return_value="1"),
        patch("mcp_server.notifiers.desktop.sys.platform", "win32"),
        patch("mcp_server.notifiers.desktop.subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0)
        res = notifier.notify("cid", "Hello Windows", title="Test Windows")
        assert res is True
        assert mock_run.called
        assert mock_run.call_args.kwargs.get("stdin") == subprocess.DEVNULL


def test_script_notifier_isolates_stdin():
    """Verify ScriptNotifier executes commands with stdin=subprocess.DEVNULL."""
    notifier = ScriptNotifier()
    with (
        patch.object(notifier, "_get_command_template", return_value="echo {message}"),
        patch("mcp_server.notifiers.script.subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0)
        res = notifier.notify("cid", "Hello Script")
        assert res is True
        assert mock_run.called
        assert mock_run.call_args.kwargs.get("stdin") == subprocess.DEVNULL


def test_escape_applescript_string():
    """Verify AppleScript string sanitization preserves apostrophes, escapes quotes and backslashes, and normalizes newlines."""
    # Quotes and backslashes
    assert escape_applescript_string('Hello "World" \\ Path') == 'Hello \\"World\\" \\\\ Path'
    # Apostrophes should NOT be touched (valid in AppleScript double-quoted string literals)
    assert escape_applescript_string("It's a test") == "It's a test"
    # Newlines normalized to spaces to prevent syntax breakage
    assert (
        escape_applescript_string("Line 1\r\nLine 2\nLine 3\rLine 4")
        == "Line 1 Line 2 Line 3 Line 4"
    )
    # Unicode preserved
    assert escape_applescript_string("☕ Artemis 🚀") == "☕ Artemis 🚀"


def test_escape_powershell_string():
    """Verify PowerShell string sanitization escapes quotes and backticks, and normalizes newlines."""
    assert escape_powershell_string('Hello "World" ` Path') == 'Hello `"World`" `` Path'
    assert escape_powershell_string("Line 1\r\nLine 2\nLine 3") == "Line 1 Line 2 Line 3"


def test_logger_header_does_not_pollute_stdout():
    """Verify logger.header directs output to stderr and never pollutes sys.stdout."""
    logger = get_logger("test_purity_logger")
    captured_stdout = io.StringIO()
    captured_stderr = io.StringIO()

    orig_stdout = sys.stdout
    orig_stderr = sys.stderr
    try:
        sys.stdout = captured_stdout
        sys.stderr = captured_stderr
        logger.header("IMPORTANT PROTOCOL BANNER")
    finally:
        sys.stdout = orig_stdout
        sys.stderr = orig_stderr

    assert captured_stdout.getvalue() == "", (
        f"Detected stdout pollution from logger.header: {captured_stdout.getvalue()!r}. "
        "In MCP stdio mode, stdout MUST be reserved exclusively for JSON-RPC messages!"
    )
    assert "IMPORTANT PROTOCOL BANNER" in captured_stderr.getvalue()
