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

"""Unit tests for the MCP / IDE integration-host readiness probe."""

import os
from pathlib import Path
import sys

import pytest

from artemis.core.diagnostics import IntegrationHostProbe, ReadinessEngine
from artemis.core.diagnostics.probes import host_probe
from artemis.core.diagnostics.schema import ProbeCategory, ProbeStatus


#: Environment keys / prefixes any IDE might export; scrubbed so the developer's
#: own host (this test suite is often run from inside Claude Code or Cursor) does
#: not leak into the detection tests.
_SIGNAL_PREFIXES = (
    "ANTIGRAVITY_",
    "CLAUDE_CODE_",
    "CURSOR_",
    "WINDSURF_",
    "CODEX_",
    "OPENCLAW_",
    "CLINE_",
    "ROO_",
    "VSCODE_",
)
_SIGNAL_KEYS = ("CLAUDECODE", "TERM_PROGRAM")


@pytest.fixture
def clean_ide_env(monkeypatch):
    for key in list(os.environ):
        if key in _SIGNAL_KEYS or key.startswith(_SIGNAL_PREFIXES):
            monkeypatch.delenv(key, raising=False)
    return monkeypatch


@pytest.fixture
def host_env(tmp_path, monkeypatch, clean_ide_env):
    """A consistent, healthy host layout the individual tests then break."""
    venv_python = host_probe.project_venv_python(tmp_path)
    venv_python.parent.mkdir(parents=True)
    venv_python.write_bytes(b"")
    traces = tmp_path / "traces"

    monkeypatch.setattr("artemis.config.paths.ROOT_DIR", tmp_path)
    monkeypatch.setattr("artemis.config.paths.get_env_file", lambda: tmp_path / ".env")
    monkeypatch.setattr("artemis.config.paths.get_default_traces_path", lambda: traces)
    monkeypatch.setattr("artemis.config.paths.is_source_checkout", lambda: True)
    monkeypatch.setattr(sys, "executable", str(venv_python))

    monkeypatch.setattr("artemis.runtime.daemon_client.is_standalone_forced", lambda: False)
    monkeypatch.setattr("artemis.runtime.daemon_client.is_artemis_daemon", lambda: True)
    monkeypatch.setattr(
        "artemis.runtime.daemon_client.daemon_log_path", lambda: tmp_path / "daemon.log"
    )
    monkeypatch.setattr("artemis.runtime.server_lifecycle.is_port_in_use", lambda *a, **k: True)
    return tmp_path


def test_probe_is_not_part_of_the_default_console_report():
    assert "integration_host" not in ReadinessEngine()._probes
    probe = IntegrationHostProbe()
    assert probe.probe_id == "integration_host"
    assert probe.category is ProbeCategory.RUNTIME
    assert probe.is_blocker is True


@pytest.mark.asyncio
async def test_consistent_host_passes(host_env):
    result = await IntegrationHostProbe().probe()

    assert result.status is ProbeStatus.PASS
    assert result.is_blocker is True
    assert result.summary == "Host Ready"
    meta = result.metadata
    assert meta["venv_exists"] is True
    assert meta["interpreter_matches_venv"] is True
    assert Path(meta["runner_python"]) == host_probe.project_venv_python(host_env)
    assert meta["env_file_exists"] is False
    assert meta["traces_dir_writable"] is True
    assert (host_env / "traces").is_dir()
    assert meta["daemon"] == {
        "host": "127.0.0.1",
        "port": 8000,
        "standalone_forced": False,
        "reachable": True,
        "port_in_use": True,
        "port_held_by_other_process": False,
        "log_path": str(host_env / "daemon.log"),
    }
    labels = [a.label for a in result.actions]
    assert labels[0] == "Host OK"
    assert "Env File Location" in labels  # informational even when passing


@pytest.mark.asyncio
async def test_daemon_port_held_by_other_process_warns(host_env, monkeypatch):
    monkeypatch.setattr("artemis.runtime.daemon_client.is_artemis_daemon", lambda: False)

    result = await IntegrationHostProbe().probe()

    assert result.status is ProbeStatus.WARN
    assert result.is_blocker is False  # tasks still run standalone
    assert result.metadata["daemon"]["port_held_by_other_process"] is True
    assert "port 8000 is held" in result.description.lower()
    port_fix = next(a for a in result.actions if a.label == "Free the Daemon Port")
    assert "ARTEMIS_DAEMON_PORT" in port_fix.payload


@pytest.mark.asyncio
async def test_daemon_not_running_and_port_free_is_informational(host_env, monkeypatch):
    monkeypatch.setattr("artemis.runtime.daemon_client.is_artemis_daemon", lambda: False)
    monkeypatch.setattr("artemis.runtime.server_lifecycle.is_port_in_use", lambda *a, **k: False)

    result = await IntegrationHostProbe().probe()

    assert result.status is ProbeStatus.PASS
    assert "auto-start" in result.description
    auto = next(a for a in result.actions if a.label == "Daemon Auto-Start")
    assert str(host_env / "daemon.log") in auto.payload


@pytest.mark.asyncio
async def test_standalone_mode_skips_daemon_probe(host_env, monkeypatch):
    monkeypatch.setattr("artemis.runtime.daemon_client.is_standalone_forced", lambda: True)
    monkeypatch.setattr("artemis.runtime.daemon_client.is_artemis_daemon", lambda: False)

    result = await IntegrationHostProbe().probe()

    assert result.status is ProbeStatus.PASS
    assert result.metadata["daemon"]["standalone_forced"] is True
    assert result.metadata["daemon"]["port_held_by_other_process"] is False
    auto = next(a for a in result.actions if a.label == "Daemon Auto-Start")
    assert "ARTEMIS_STANDALONE" in auto.payload


@pytest.mark.asyncio
async def test_interpreter_mismatch_warns_and_points_at_installer(host_env, monkeypatch):
    other = host_env / "other-python.exe"
    other.write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(other))

    result = await IntegrationHostProbe().probe()

    assert result.status is ProbeStatus.WARN
    assert result.metadata["interpreter_matches_venv"] is False
    assert Path(result.metadata["runner_python"]) == host_probe.project_venv_python(host_env)
    assert result.metadata["mcp_client"] is None
    assert not [a for a in result.actions if a.action_type == "command"]
    regen = next(a for a in result.actions if a.label == "Regenerate MCP Config")
    assert regen.action_type == "hint"
    assert "ask the user which ide" in regen.payload.lower()
    assert "uv run artemis mcp --install <client>" in regen.payload
    for name in host_probe.MCP_CLIENT_NAMES:
        assert name in regen.payload
    assert next(a for a in result.actions if a.label == "Interpreter Mismatch")


@pytest.mark.asyncio
async def test_interpreter_mismatch_emits_runnable_install_for_detected_client(
    host_env, monkeypatch
):
    monkeypatch.setenv("CLAUDECODE", "1")
    other = host_env / "other-python.exe"
    other.write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(other))

    result = await IntegrationHostProbe().probe()

    assert result.status is ProbeStatus.WARN
    assert result.metadata["mcp_client"] == "claude"
    regen = next(a for a in result.actions if a.action_type == "command")
    assert regen.label == "Regenerate MCP Config"
    assert regen.payload == "uv run artemis mcp --install claude"
    assert "<client>" not in regen.payload


@pytest.mark.asyncio
async def test_metadata_reports_client_even_when_host_is_healthy(host_env, monkeypatch):
    monkeypatch.setenv("CURSOR_TRACE_ID", "abc")

    result = await IntegrationHostProbe().probe()

    assert result.status is ProbeStatus.PASS
    assert result.metadata["mcp_client"] == "cursor"


def test_detect_mcp_client_unknown_in_clean_environment(clean_ide_env):
    assert host_probe.detect_mcp_client() is None


@pytest.mark.parametrize(
    ("variables", "expected"),
    [
        ({"ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:1"}, "antigravity"),
        ({"ANTIGRAVITY_CSRF_TOKEN": "t"}, "antigravity"),
        ({"CLAUDECODE": "1"}, "claude"),
        ({"CLAUDE_CODE_ENTRYPOINT": "cli"}, "claude"),
        ({"CURSOR_TRACE_ID": "abc"}, "cursor"),
        ({"CURSOR_AGENT": "1"}, "cursor"),
        ({"WINDSURF_SESSION": "x"}, "windsurf"),
        ({"CODEX_SANDBOX": "seatbelt"}, "codex"),
        ({"CODEX_HOME": "/tmp/codex", "CODEX_SANDBOX_NETWORK_DISABLED": "1"}, "codex"),
        ({"OPENCLAW_SESSION_ID": "s"}, "openclaw"),
        ({"CLINE_TASK_ID": "1"}, "cline"),
        ({"ROO_CODE_TASK": "1"}, "roo"),
        ({"VSCODE_PID": "42"}, "vscode"),
        ({"TERM_PROGRAM": "vscode"}, "vscode"),
    ],
)
def test_detect_mcp_client_signals(clean_ide_env, variables, expected):
    for key, value in variables.items():
        clean_ide_env.setenv(key, value)
    assert host_probe.detect_mcp_client() == expected


@pytest.mark.parametrize(
    "variables",
    [
        {"CODEX_HOME": "/home/me/.codex"},  # user relocated the Codex config dir
        {"OPENCLAW_WEBHOOK_URL": "https://hooks.example/x"},  # notifier setting, any host
        {"TERM_PROGRAM": "iTerm.app"},
    ],
)
def test_detect_mcp_client_ignores_weak_or_user_set_variables(clean_ide_env, variables):
    for key, value in variables.items():
        clean_ide_env.setenv(key, value)
    assert host_probe.detect_mcp_client() is None


@pytest.mark.parametrize(
    ("variables", "expected"),
    [
        # Claude Code or Codex running inside a VS Code / Cursor terminal: the
        # agent that spawned the server wins over the editor around it.
        ({"CLAUDECODE": "1", "VSCODE_PID": "1", "TERM_PROGRAM": "vscode"}, "claude"),
        ({"CLAUDECODE": "1", "CURSOR_TRACE_ID": "x"}, "claude"),
        ({"CODEX_SANDBOX": "seatbelt", "VSCODE_PID": "1"}, "codex"),
        # Cursor / Windsurf / Cline are VS Code forks or extensions and inherit
        # the generic VS Code markers.
        ({"CURSOR_TRACE_ID": "x", "VSCODE_PID": "1", "TERM_PROGRAM": "vscode"}, "cursor"),
        ({"WINDSURF_SESSION": "x", "TERM_PROGRAM": "vscode"}, "windsurf"),
        ({"CLINE_TASK_ID": "1", "VSCODE_PID": "1"}, "cline"),
    ],
)
def test_detect_mcp_client_specific_host_beats_generic_vscode(clean_ide_env, variables, expected):
    for key, value in variables.items():
        clean_ide_env.setenv(key, value)
    assert host_probe.detect_mcp_client() == expected


def test_mcp_install_actions_shapes():
    (cmd,) = host_probe.mcp_install_actions("antigravity")
    assert cmd.action_type == "command"
    assert cmd.payload == "uv run artemis mcp --install antigravity"

    (hint,) = host_probe.mcp_install_actions(None)
    assert hint.action_type == "hint"
    assert "antigravity, claude, cursor, windsurf, vscode, cline, roo, openclaw, codex" in (
        hint.payload
    )


@pytest.mark.asyncio
async def test_missing_venv_warns_with_uv_sync(host_env, monkeypatch):
    host_probe.project_venv_python(host_env).unlink()
    monkeypatch.setattr(sys, "executable", str(host_env / "system-python.exe"))

    result = await IntegrationHostProbe().probe()

    assert result.status is ProbeStatus.WARN
    assert result.metadata["venv_exists"] is False
    assert result.metadata["runner_python"] == str(host_env / "system-python.exe")
    assert any(a.payload == "uv sync" for a in result.actions)


@pytest.mark.asyncio
async def test_unwritable_traces_dir_fails_even_with_other_warnings(host_env, monkeypatch):
    monkeypatch.setattr(host_probe, "directory_is_writable", lambda _p: (False, "denied"))
    monkeypatch.setattr("artemis.runtime.daemon_client.is_artemis_daemon", lambda: False)

    result = await IntegrationHostProbe().probe()

    assert result.status is ProbeStatus.FAIL
    assert result.is_blocker is True
    assert result.summary == "Host Misconfigured"
    assert result.metadata["traces_dir_writable"] is False
    assert result.metadata["traces_dir_error"] == "denied"
    assert result.actions[0].label == "Fix Traces Directory"
    assert "ARTEMIS_TRACES_DIR" in result.actions[0].payload
    # The port problem is still reported alongside the blocking failure.
    assert "port 8000 is held" in result.description.lower()


def test_directory_is_writable_reports_real_errors(tmp_path):
    ok, error = host_probe.directory_is_writable(tmp_path / "new" / "nested")
    assert ok is True and error is None

    blocker = tmp_path / "file"
    blocker.write_text("x")
    ok, error = host_probe.directory_is_writable(blocker / "child")
    assert ok is False and error


@pytest.mark.asyncio
async def test_wheel_install_without_venv_is_not_a_venv_warning(host_env, monkeypatch):
    """A wheel install has no .venv under site-packages; that is normal, not a warning."""
    host_probe.project_venv_python(host_env).unlink()
    system_python = host_env / "system-python.exe"
    monkeypatch.setattr(sys, "executable", str(system_python))
    monkeypatch.setattr("artemis.config.paths.is_source_checkout", lambda: False)

    result = await IntegrationHostProbe().probe()

    assert result.status is ProbeStatus.PASS
    assert result.summary == "Host Ready"
    meta = result.metadata
    assert meta["is_source_checkout"] is False
    assert meta["venv_python"] is None
    assert meta["venv_exists"] is False
    assert meta["interpreter_matches_venv"] is False
    assert meta["runner_python"] == str(system_python)
    assert "venv" not in result.description.lower()
    assert not [a for a in result.actions if a.payload == "uv sync"]
    assert not [a for a in result.actions if a.label == "Interpreter Mismatch"]


@pytest.mark.asyncio
async def test_wheel_install_ignores_stray_venv_next_to_site_packages(host_env, monkeypatch):
    """Even if a .venv happens to exist under ROOT_DIR, a non-source install never compares against it."""
    other = host_env / "other-python.exe"
    other.write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(other))
    monkeypatch.setattr("artemis.config.paths.is_source_checkout", lambda: False)

    result = await IntegrationHostProbe().probe()

    assert result.status is ProbeStatus.PASS
    assert result.metadata["venv_python"] is None
    assert result.metadata["runner_python"] == str(other)
    assert not [a for a in result.actions if a.label == "Interpreter Mismatch"]


@pytest.mark.asyncio
async def test_source_checkout_without_venv_still_warns_with_uv_sync(host_env, monkeypatch):
    host_probe.project_venv_python(host_env).unlink()
    monkeypatch.setattr(sys, "executable", str(host_env / "system-python.exe"))
    monkeypatch.setattr("artemis.config.paths.is_source_checkout", lambda: True)

    result = await IntegrationHostProbe().probe()

    assert result.status is ProbeStatus.WARN
    assert result.metadata["is_source_checkout"] is True
    assert result.metadata["venv_python"] == str(host_probe.project_venv_python(host_env))
    assert result.metadata["venv_exists"] is False
    assert "no project virtualenv" in result.description.lower()
    assert any(a.payload == "uv sync" for a in result.actions)


@pytest.mark.asyncio
async def test_traces_path_creation_error_becomes_fail_not_crash(host_env, monkeypatch):
    """get_default_traces_path mkdirs as a side effect; a read-only parent must not crash the probe."""

    def denied():
        raise PermissionError(13, "Permission denied", str(host_env / "traces"))

    monkeypatch.setattr("artemis.config.paths.get_default_traces_path", denied)
    monkeypatch.setenv("ARTEMIS_TRACES_DIR", str(host_env / "ro" / "traces"))

    result = await IntegrationHostProbe().probe()

    assert result.status is ProbeStatus.FAIL
    assert result.is_blocker is True
    assert result.summary == "Host Misconfigured"
    meta = result.metadata
    assert meta["traces_dir_writable"] is False
    assert "Permission denied" in meta["traces_dir_error"]
    # The intended path is still named, resolved without touching the disk.
    assert Path(meta["traces_dir"]) == host_env / "ro" / "traces"
    assert not (host_env / "ro").exists()
    assert result.actions[0].label == "Fix Traces Directory"
    assert "ARTEMIS_TRACES_DIR" in result.actions[0].payload
    assert "not writable" in result.description


def test_intended_traces_path_mirrors_selection_without_mkdir(tmp_path, monkeypatch):
    monkeypatch.setattr("artemis.config.paths.ROOT_DIR", tmp_path)
    monkeypatch.delenv("ARTEMIS_TRACES_DIR", raising=False)

    monkeypatch.setattr("artemis.config.paths._use_user_app_dir", lambda: False)
    assert host_probe.intended_traces_path() == tmp_path / "traces"

    monkeypatch.setattr("artemis.config.paths._use_user_app_dir", lambda: True)
    monkeypatch.setattr("artemis.config.paths.get_app_dir", lambda: tmp_path / "app")
    assert host_probe.intended_traces_path() == tmp_path / "app" / "traces"

    monkeypatch.setenv("ARTEMIS_TRACES_DIR", str(tmp_path / "env-traces"))
    assert host_probe.intended_traces_path() == tmp_path / "env-traces"

    assert not (tmp_path / "traces").exists()
    assert not (tmp_path / "app").exists()
    assert not (tmp_path / "env-traces").exists()


# --------------------------------------------------------------------------- #
# Daemon identity (a dev server on port 8000 is not the daemon)
# --------------------------------------------------------------------------- #


class _FakeResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.parametrize(
    "status, body, expected",
    [
        (200, b'{"running": true, "port": 8000, "current_pid": 42}', True),
        (200, b"<!doctype html><title>Vite App</title>", False),  # dev server on /
        (200, b'{"hello": "world"}', False),
        (404, b"", False),
    ],
)
def test_is_artemis_daemon_requires_the_server_status_shape(monkeypatch, status, body, expected):
    from artemis.runtime import daemon_client

    seen: dict[str, str] = {}

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        return _FakeResponse(status, body)

    monkeypatch.setattr(daemon_client.urllib.request, "urlopen", fake_urlopen)
    assert daemon_client.is_artemis_daemon("127.0.0.1", 8000) is expected
    assert seen["url"] == "http://127.0.0.1:8000/api/system/server-status"


def test_is_artemis_daemon_is_false_when_nothing_listens(monkeypatch):
    from artemis.runtime import daemon_client

    def refuse(req, timeout):
        raise ConnectionRefusedError()

    monkeypatch.setattr(daemon_client.urllib.request, "urlopen", refuse)
    assert daemon_client.is_artemis_daemon("127.0.0.1", 8000) is False
