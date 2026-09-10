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

"""Unit tests for ARTEMIS Unified CLI application."""

from typer.testing import CliRunner
from artemis.interfaces.cli.main import app

runner = CliRunner()


def test_cli_help():
    """Verify top-level CLI help returns status 0 and lists core subcommands."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Artemis: Autonomous Multimodal Android Agent" in result.output
    assert "run" in result.output
    assert "batch" in result.output
    assert "server" in result.output
    assert "trace" in result.output
    assert "mcp" in result.output


def test_cli_version():
    """Verify --version returns version banner."""
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "Artemis Agent Platform" in result.output


def test_cli_run_help():
    """Verify 'artemis run --help' displays execution options."""
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    assert "--profile" in result.output
    assert "--locked-app" in result.output
    assert "--traces-path" in result.output
    assert "--verification-level" in result.output
    assert "--explorer-pro-mode" in result.output


def test_cli_batch_help():
    """Verify 'artemis batch --help' displays batch options."""
    result = runner.invoke(app, ["batch", "--help"])
    assert result.exit_code == 0
    assert "--file" in result.output
    assert "--delay" in result.output
    assert "--verification-level" in result.output
    assert "--explorer-pro-mode" in result.output


def test_cli_batch_forwards_pro_tuning_in_standalone_mode(monkeypatch):
    """`artemis batch --standalone` threads both knobs into run_batch_tasks."""
    import artemis.interfaces.cli.commands.batch as batch_module

    captured: dict = {}

    async def fake_run_batch_tasks(tasks, **kwargs):
        captured["tasks"] = tasks
        captured.update(kwargs)

    monkeypatch.setattr(batch_module, "run_batch_tasks", fake_run_batch_tasks)
    result = runner.invoke(
        app,
        [
            "batch",
            "--standalone",
            "--profile",
            "pro",
            "--verification-level",
            "strict",
            "--explorer-pro-mode",
            "ultra",
            "Open Settings",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["tasks"] == ["Open Settings"]
    assert captured["profile_name"] == "pro"
    assert captured["verification_level"] == "strict"
    assert captured["explorer_pro_mode"] == "ultra"


def test_cli_batch_forwards_pro_tuning_to_daemon(monkeypatch):
    """Daemon-routed batches carry the knobs as /api/run JSON fields."""
    import artemis.runtime as runtime

    monkeypatch.delenv("ARTEMIS_STANDALONE", raising=False)
    captured: dict = {}

    def fake_submit_batch(goals, **kwargs):
        captured["goals"] = goals
        captured.update(kwargs)
        return {"tasks": [{"session_id": "sid-1", "goal": goals[0]}]}

    monkeypatch.setattr(runtime, "ensure_daemon_running", lambda **_: (True, "http://x:1"))
    monkeypatch.setattr(runtime, "submit_batch_to_daemon", fake_submit_batch)
    monkeypatch.setattr(runtime, "wait_for_daemon_task", lambda *_, **__: {"status": "completed"})

    result = runner.invoke(
        app,
        ["batch", "--verification-level", "checkpoints", "--explorer-pro-mode", "pro", "Goal A"],
    )
    assert result.exit_code == 0, result.output
    assert captured["goals"] == ["Goal A"]
    assert captured["verification_level"] == "checkpoints"
    assert captured["explorer_mode"] == "pro"


def test_run_batch_tasks_applies_pro_tuning_to_agent_config(monkeypatch):
    """The standalone batch runner applies the knobs on the AgentConfig builder."""
    from unittest.mock import AsyncMock, MagicMock

    import artemis.interfaces.cli.commands.batch as batch_module

    fake_builder = MagicMock()
    fake_builders = MagicMock()
    fake_builders.AgentConfig.with_default_profile.return_value = fake_builder
    fake_agent = MagicMock()
    fake_agent.init = AsyncMock()
    fake_agent.run_task = AsyncMock(return_value="ok")
    fake_agent.clean = AsyncMock()

    monkeypatch.setattr(batch_module, "initialize_llm_config", lambda: MagicMock())
    monkeypatch.setattr(batch_module, "AgentProfile", MagicMock())
    monkeypatch.setattr(batch_module, "Builders", fake_builders)
    monkeypatch.setattr(batch_module, "Agent", MagicMock(return_value=fake_agent))

    import asyncio

    asyncio.run(
        batch_module.run_batch_tasks(
            ["Goal A"],
            profile_name="pro",
            delay_seconds=0,
            verification_level="strict",
            explorer_pro_mode="ultra",
        )
    )
    fake_builder.with_verification_level.assert_called_once_with("strict")
    fake_builder.with_explorer.assert_called_once_with(pro_mode="ultra")
    fake_agent.run_task.assert_awaited_once_with(goal="Goal A", profile="pro")


def test_cli_trace_help():
    """Verify 'artemis trace --help' lists trace subcommands."""
    result = runner.invoke(app, ["trace", "--help"])
    assert result.exit_code == 0
    assert "list" in result.output
    assert "view" in result.output


def test_cli_mcp_help():
    """Verify 'artemis mcp --help' lists server options."""
    result = runner.invoke(app, ["mcp", "--help"])
    assert result.exit_code == 0
    assert "--type" in result.output
    assert "--generate-config" in result.output


def test_cli_mcp_generate_config():
    """Verify 'artemis mcp --generate-config cursor' produces valid configuration."""
    result = runner.invoke(app, ["mcp", "--generate-config", "cursor"])
    assert result.exit_code == 0
    assert "mcpServers" in result.output
    assert "mcp_server" in result.output


def test_cli_mcp_generate_config_antigravity():
    """Verify current Antigravity uses only documented, load-safe fields."""
    from artemis.interfaces.cli.commands.mcp import _get_config_snippet

    result = runner.invoke(app, ["mcp", "--generate-config", "antigravity"])
    assert result.exit_code == 0
    assert "mcpServers" in result.output
    server_config = _get_config_snippet("antigravity", "python", "/project")["mcpServers"][
        "artemis"
    ]
    assert server_config["disabledTools"] == []
    assert "tools" not in server_config

    legacy_config = _get_config_snippet("jetski", "python", "/project")["mcpServers"]["artemis"]
    assert set(legacy_config["tools"]) == {
        "mobile_run_task",
        "mobile_manage_task",
        "mobile_get_device_state",
        "mobile_inspect_trace",
        "mobile_diagnose",
    }
    assert all(tool["eager"] is True for tool in legacy_config["tools"].values())


def test_cli_mcp_generate_config_all():
    """Verify 'artemis mcp --generate-config all' includes every supported client."""
    result = runner.invoke(app, ["mcp", "--generate-config", "all"])
    assert result.exit_code == 0
    assert "antigravity" in result.output
    assert "cursor" in result.output
    assert "windsurf" in result.output
    assert "claude" in result.output
    assert "vscode" in result.output
    assert "cline" in result.output
    assert "roo" in result.output
    assert "codex" in result.output


def test_cli_mcp_generate_config_codex():
    """Verify Codex initializes the server and enables every Artemis tool."""
    from artemis.interfaces.cli.commands.mcp import _get_config_snippet

    result = runner.invoke(app, ["mcp", "--generate-config", "codex"])
    assert result.exit_code == 0
    assert "[mcp_servers.artemis]" in result.output
    assert "[mcp_servers.artemis.env]" in result.output
    assert "enabled = true" in result.output
    assert "required = true" in result.output
    assert "startup_timeout_sec = 120" in result.output
    assert "enabled_tools" in result.output
    assert "mobile_run_task" in result.output
    assert "mobile_manage_task" in result.output
    server_config = _get_config_snippet("codex", "python", "/project")["mcp_servers"]["artemis"]
    assert server_config["enabled_tools"] == [
        "mobile_run_task",
        "mobile_manage_task",
        "mobile_get_device_state",
        "mobile_inspect_trace",
        "mobile_diagnose",
    ]


def test_cli_mcp_install_antigravity(tmp_path, monkeypatch):
    """Verify 'artemis mcp --install antigravity' writes configuration and global rules into target files."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr("mcp_server.utils.env_utils.get_project_root", lambda: str(tmp_path))
    result = runner.invoke(app, ["mcp", "--install", "antigravity"])
    assert result.exit_code == 0
    assert "Successfully installed ARTEMIS MCP server configuration & rules" in result.output
    jetski_file = tmp_path / ".gemini" / "jetski" / "mcp_config.json"
    assert jetski_file.exists()
    import json

    data = json.loads(jetski_file.read_text())
    assert "artemis" in data["mcpServers"]
    assert "mobile_run_task" in data["mcpServers"]["artemis"]["tools"]
    assert "PYTHONPATH" in data["mcpServers"]["artemis"]["env"]

    current_file = tmp_path / ".gemini" / "config" / "mcp_config.json"
    current_data = json.loads(current_file.read_text())
    assert current_data["mcpServers"]["artemis"]["disabledTools"] == []
    assert "tools" not in current_data["mcpServers"]["artemis"]

    # Verify global rule file installed
    gemini_md = tmp_path / ".gemini" / "GEMINI.md"
    assert gemini_md.exists()
    assert "Mobile Testing Mindset" in gemini_md.read_text(encoding="utf-8")
    rule_file = tmp_path / ".gemini" / "rules" / "artemis.md"
    assert rule_file.exists()
    assert "Mobile Testing Mindset" in rule_file.read_text(encoding="utf-8")


def test_cli_mcp_install_all(tmp_path, monkeypatch):
    """Verify 'artemis mcp --install all' installs configs and global rules to all supported IDE locations."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    monkeypatch.setattr("mcp_server.utils.env_utils.get_project_root", lambda: str(tmp_path))
    result = runner.invoke(app, ["mcp", "--install", "all"])
    assert result.exit_code == 0
    assert "Successfully installed ARTEMIS MCP server configuration & rules" in result.output
    assert (tmp_path / ".cursor" / "mcp.json").exists()
    assert (tmp_path / ".codeium" / "windsurf" / "mcp_config.json").exists()
    assert (tmp_path / ".openclaw" / "openclaw.json").exists()
    assert (tmp_path / ".claude.json").exists()
    assert (tmp_path / ".codex" / "config.toml").exists()

    import json

    cursor_data = json.loads((tmp_path / ".cursor" / "mcp.json").read_text())
    assert cursor_data["mcpServers"]["artemis"]["command"]

    windsurf_data = json.loads((tmp_path / ".codeium" / "windsurf" / "mcp_config.json").read_text())
    assert windsurf_data["mcpServers"]["artemis"]["command"]

    claude_data = json.loads((tmp_path / ".claude.json").read_text())
    assert claude_data["mcpServers"]["artemis"]["type"] == "stdio"

    from artemis.interfaces.cli.commands.mcp import _get_vscode_user_dir

    vscode_data = json.loads((_get_vscode_user_dir() / "mcp.json").read_text())
    assert vscode_data["servers"]["artemis"]["type"] == "stdio"
    assert "mcpServers" not in vscode_data

    copilot_data = json.loads((tmp_path / ".copilot" / "mcp-config.json").read_text())
    assert copilot_data["servers"]["artemis"]["type"] == "stdio"

    openclaw_data = json.loads((tmp_path / ".openclaw" / "openclaw.json").read_text())
    assert openclaw_data["mcp"]["servers"]["artemis"]["enabled"] is True

    cline_data = json.loads(
        (tmp_path / ".cline" / "data" / "settings" / "cline_mcp_settings.json").read_text()
    )
    assert cline_data["mcpServers"]["artemis"]["disabled"] is False

    roo_data = json.loads((tmp_path / ".roo" / "mcp.json").read_text())
    assert roo_data["mcpServers"]["artemis"]["disabled"] is False

    # Verify global rule files installed across IDEs
    assert (tmp_path / ".gemini" / "GEMINI.md").exists()
    assert (tmp_path / ".gemini" / "rules" / "artemis.md").exists()
    assert (tmp_path / ".cursorrules").exists()
    assert (tmp_path / ".cursor" / "rules" / "artemis.mdc").exists()
    # Claude Code loads both CLAUDE.md and rules/*.md, so rules are installed
    # only as the standalone rule file to avoid duplicated context.
    assert not (tmp_path / ".claude" / "CLAUDE.md").exists()
    assert (tmp_path / ".claude" / "rules" / "artemis.md").exists()
    assert (tmp_path / ".codeium" / "windsurf" / "memories" / "global_rules.md").exists()
    assert (tmp_path / ".codeium" / "windsurf" / "rules" / "artemis.md").exists()
    assert (tmp_path / ".vscode" / "rules" / "artemis.md").exists()
    assert (tmp_path / ".clinerules").exists()
    assert (tmp_path / ".cline" / "rules" / "artemis.md").exists()
    assert (tmp_path / ".roorules").exists()
    assert (tmp_path / ".roo" / "rules" / "artemis.md").exists()
    assert (tmp_path / ".openclaw" / "OPENCLAW.md").exists()
    assert (tmp_path / ".openclaw" / "rules" / "artemis.md").exists()
    assert (tmp_path / ".codex" / "AGENTS.md").exists()


def test_cli_mcp_install_claude_migrates_legacy_claude_md(tmp_path, monkeypatch):
    """Verify the claude target removes a previously injected CLAUDE.md block while preserving user content."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    monkeypatch.setattr("mcp_server.utils.env_utils.get_project_root", lambda: str(tmp_path))

    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True)
    claude_md = claude_dir / "CLAUDE.md"
    claude_md.write_text(
        "# My own instructions\n\n"
        "<!-- BEGIN ARTEMIS MOBILE TESTING RULES -->\nold injected rules\n"
        "<!-- END ARTEMIS MOBILE TESTING RULES -->\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["mcp", "--install", "claude"])
    assert result.exit_code == 0

    remaining = claude_md.read_text(encoding="utf-8")
    assert "My own instructions" in remaining
    assert "ARTEMIS MOBILE TESTING RULES" not in remaining
    rule_file = claude_dir / "rules" / "artemis.md"
    assert rule_file.exists()
    assert "Mobile Testing Mindset" in rule_file.read_text(encoding="utf-8")


def test_cli_mcp_install_claude_deletes_block_only_claude_md(tmp_path, monkeypatch):
    """Verify a CLAUDE.md consisting solely of the injected block is deleted outright."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    monkeypatch.setattr("mcp_server.utils.env_utils.get_project_root", lambda: str(tmp_path))

    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True)
    claude_md = claude_dir / "CLAUDE.md"
    claude_md.write_text(
        "<!-- BEGIN ARTEMIS MOBILE TESTING RULES -->\nold injected rules\n"
        "<!-- END ARTEMIS MOBILE TESTING RULES -->\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["mcp", "--install", "claude"])
    assert result.exit_code == 0
    assert not claude_md.exists()
    assert (claude_dir / "rules" / "artemis.md").exists()


def test_cli_mcp_install_claude_refuses_unparseable_claude_json(tmp_path, monkeypatch):
    """Verify an unparseable ~/.claude.json is left untouched instead of being rewritten."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    monkeypatch.setattr("mcp_server.utils.env_utils.get_project_root", lambda: str(tmp_path))

    corrupt = "{ this is not json at all"
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(corrupt, encoding="utf-8")

    result = runner.invoke(app, ["mcp", "--install", "claude"])
    assert result.exit_code == 0
    assert claude_json.read_text(encoding="utf-8") == corrupt


def test_cli_mcp_install_codex_preserves_config_and_is_idempotent(tmp_path, monkeypatch):
    """Verify Codex TOML merging preserves other servers and updates only Artemis."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr("mcp_server.utils.env_utils.get_project_root", lambda: str(tmp_path))
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir(parents=True)
    config_file = codex_dir / "config.toml"
    config_file.write_text(
        'model = "test-model"\n\n'
        '[mcp_servers.existing]\ncommand = "echo"\n\n'
        '[mcp_servers.artemis]\ncommand = "old-command"\n',
        encoding="utf-8",
    )

    result = runner.invoke(app, ["mcp", "--install", "codex"])
    assert result.exit_code == 0
    result2 = runner.invoke(app, ["mcp", "--install", "codex"])
    assert result2.exit_code == 0

    import tomllib

    config_text = config_file.read_text(encoding="utf-8")
    data = tomllib.loads(config_text)
    assert data["model"] == "test-model"
    assert data["mcp_servers"]["existing"]["command"] == "echo"
    assert data["mcp_servers"]["artemis"]["args"] == ["-m", "mcp_server"]
    assert data["mcp_servers"]["artemis"]["enabled"] is True
    assert data["mcp_servers"]["artemis"]["required"] is True
    assert data["mcp_servers"]["artemis"]["startup_timeout_sec"] == 120
    assert data["mcp_servers"]["artemis"]["enabled_tools"] == [
        "mobile_run_task",
        "mobile_manage_task",
        "mobile_get_device_state",
        "mobile_inspect_trace",
        "mobile_diagnose",
    ]
    assert data["mcp_servers"]["artemis"]["env"]["PYTHONPATH"] == str(tmp_path)
    assert config_text.count("# BEGIN ARTEMIS MCP CONFIG") == 1

    agents_file = codex_dir / "AGENTS.md"
    agents_text = agents_file.read_text(encoding="utf-8")
    assert "Mobile Testing Mindset" in agents_text
    assert agents_text.count("<!-- BEGIN ARTEMIS MOBILE TESTING RULES -->") == 1


def test_cli_mcp_install_jsonc_and_backup(tmp_path, monkeypatch):
    """Verify merging configuration preserves valid JSONC and creates .bak for unparseable JSON."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr("mcp_server.utils.env_utils.get_project_root", lambda: str(tmp_path))

    cursor_dir = tmp_path / ".cursor"
    cursor_dir.mkdir(parents=True, exist_ok=True)
    mcp_json = cursor_dir / "mcp.json"

    # 1. Valid JSONC with comments and trailing comma
    mcp_json.write_text(
        '{\n  // My existing server\n  "mcpServers": {\n    "test": {"command": "echo"},\n  }\n}',
        encoding="utf-8",
    )
    result = runner.invoke(app, ["mcp", "--install", "cursor"])
    assert result.exit_code == 0
    import json

    data = json.loads(mcp_json.read_text(encoding="utf-8"))
    assert "test" in data["mcpServers"]
    assert "artemis" in data["mcpServers"]

    # 2. Corrupt / unparseable file triggers backup
    mcp_json.write_text("INVALID JSON DATA {{{{", encoding="utf-8")
    result2 = runner.invoke(app, ["mcp", "--install", "cursor"])
    assert result2.exit_code == 0
    backup_file = cursor_dir / "mcp.json.bak"
    assert backup_file.exists()
    assert "INVALID JSON DATA" in backup_file.read_text(encoding="utf-8")


def test_cli_mcp_install_refuses_rewrite_when_backup_fails(tmp_path, monkeypatch):
    """Backup failure for an unparseable config must abort the rewrite instead of destroying it."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr("mcp_server.utils.env_utils.get_project_root", lambda: str(tmp_path))

    cursor_dir = tmp_path / ".cursor"
    cursor_dir.mkdir(parents=True, exist_ok=True)
    mcp_json = cursor_dir / "mcp.json"
    original = "INVALID JSON DATA {{{{"
    mcp_json.write_text(original, encoding="utf-8")

    import pathlib

    real_write_text = pathlib.Path.write_text

    def failing_bak_write(self, *args, **kwargs):
        if str(self).endswith(".bak"):
            raise OSError("disk full")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr("pathlib.Path.write_text", failing_bak_write)

    runner.invoke(app, ["mcp", "--install", "cursor"])

    assert mcp_json.read_text(encoding="utf-8") == original
    assert not (cursor_dir / "mcp.json.bak").exists()


def test_cli_mcp_install_openclaw_migrates_legacy_plugin_shape(tmp_path, monkeypatch):
    """Verify reinstalling OpenClaw replaces the obsolete plugin wrapper with mcp.servers."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr("mcp_server.utils.env_utils.get_project_root", lambda: str(tmp_path))
    config_path = tmp_path / ".openclaw" / "openclaw.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        '{"plugins":{"artemis_mcp":{"enabled":true},"keep":{"enabled":true}}}',
        encoding="utf-8",
    )

    result = runner.invoke(app, ["mcp", "--install", "openclaw"])
    assert result.exit_code == 0

    import json

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert "artemis_mcp" not in data["plugins"]
    assert data["plugins"]["keep"]["enabled"] is True
    assert data["mcp"]["servers"]["artemis"]["enabled"] is True
    assert data["mcp"]["servers"]["artemis"]["command"]


def test_cli_restart_help():
    """Verify 'artemis restart --help' displays lifecycle options."""
    result = runner.invoke(app, ["restart", "--help"])
    assert result.exit_code == 0
    assert "--port" in result.output
    assert "--host" in result.output
    assert "--force" in result.output
    assert "--daemon" in result.output
    assert "--open" in result.output


def test_cli_stop_help():
    """Verify 'artemis stop --help' displays stop options."""
    result = runner.invoke(app, ["stop", "--help"])
    assert result.exit_code == 0
    assert "--port" in result.output
    assert "--force" in result.output


def test_cli_status_help():
    """Verify 'artemis status --help' displays status options."""
    result = runner.invoke(app, ["status", "--help"])
    assert result.exit_code == 0
    assert "--port" in result.output


def test_cli_status_offline(monkeypatch):
    """Verify 'artemis status' reports offline when port is unused."""
    from artemis.runtime import server_lifecycle

    monkeypatch.setattr(server_lifecycle, "is_port_in_use", lambda port, **kwargs: False)
    monkeypatch.setattr(server_lifecycle, "find_server_pids", lambda port: [])
    monkeypatch.setattr(server_lifecycle, "read_server_info", lambda: None)

    result = runner.invoke(app, ["status", "--port", "59998"])
    assert result.exit_code == 0
    assert "OFFLINE" in result.output or "STOPPED" in result.output


def test_cli_status_online(monkeypatch):
    """Verify 'artemis status' reports online details when server is active."""
    from artemis.runtime import server_lifecycle

    monkeypatch.setattr(server_lifecycle, "is_port_in_use", lambda port, **kwargs: True)
    monkeypatch.setattr(server_lifecycle, "find_server_pids", lambda port: [12345])
    monkeypatch.setattr(
        server_lifecycle,
        "read_server_info",
        lambda: {"pid": 12345, "port": 8000, "started_at": 1000.0, "cwd": "/tmp"},
    )

    result = runner.invoke(app, ["status", "--port", "8000"])
    assert result.exit_code == 0
    assert "ONLINE" in result.output or "RUNNING" in result.output
    assert "12345" in result.output


def test_cli_stop_command(monkeypatch):
    """Verify 'artemis stop' invokes stop_server with given parameters."""
    from artemis.interfaces.cli.commands import server_lifecycle as sl_cmd

    mock_called = {}

    def mock_stop(port, timeout=4.0, force=False):
        mock_called["port"] = port
        mock_called["force"] = force
        return True, "Artemis server stopped (PID: 12345).", [12345]

    monkeypatch.setattr(sl_cmd, "find_server_pids", lambda port: [12345])
    monkeypatch.setattr(sl_cmd, "stop_server", mock_stop)

    result = runner.invoke(app, ["stop", "--port", "8000", "--force"])
    assert result.exit_code == 0
    assert mock_called["port"] == 8000
    assert mock_called["force"] is True
    assert "stopped successfully" in result.output.lower() or "PID: 12345" in result.output


def test_cli_restart_command(monkeypatch):
    """Verify 'artemis restart' stops previous server and spawns a detached daemon by default."""
    from unittest.mock import MagicMock

    from artemis.interfaces.cli.commands import server_lifecycle as sl_cmd
    from artemis.runtime import server_lifecycle

    stopped = {}
    spawned = {}

    def mock_stop(port, timeout=4.0, force=False):
        stopped["port"] = port
        return True, "Stopped server", [12345]

    def mock_spawn(host, port):
        spawned["host"] = host
        spawned["port"] = port
        proc = MagicMock()
        proc.pid = 54321
        proc.poll.return_value = None
        return proc

    monkeypatch.setattr(server_lifecycle, "find_server_pids", lambda port: [12345])
    monkeypatch.setattr(sl_cmd, "stop_server", mock_stop)
    monkeypatch.setattr(sl_cmd, "spawn_daemon", mock_spawn)
    monkeypatch.setattr(sl_cmd, "is_daemon_running", lambda **kwargs: True)
    monkeypatch.setattr(sl_cmd, "ensure_showcase_built", lambda console: None)

    result = runner.invoke(app, ["restart", "--port", "8888", "--no-open"])
    assert result.exit_code == 0
    assert stopped["port"] == 8888
    assert spawned["port"] == 8888
    assert "PID: 54321" in result.output


def test_cli_restart_foreground_command(monkeypatch):
    """Verify 'artemis restart --foreground' runs the server attached via ui_command."""
    from artemis.interfaces.cli.commands import server_lifecycle as sl_cmd
    from artemis.runtime import server_lifecycle

    stopped = {}
    ui_called = {}

    def mock_stop(port, timeout=4.0, force=False):
        stopped["port"] = port
        return True, "Stopped server", [12345]

    def mock_ui(host, port, open_browser, reload):
        ui_called["host"] = host
        ui_called["port"] = port
        ui_called["open_browser"] = open_browser

    monkeypatch.setattr(server_lifecycle, "find_server_pids", lambda port: [12345])
    monkeypatch.setattr(sl_cmd, "stop_server", mock_stop)
    monkeypatch.setattr(sl_cmd, "ui_command", mock_ui)

    result = runner.invoke(app, ["restart", "--port", "8888", "--no-open", "--foreground"])
    assert result.exit_code == 0
    assert stopped["port"] == 8888
    assert ui_called["port"] == 8888
    assert ui_called["open_browser"] is False


def test_cli_server_lifecycle_aliases(monkeypatch):
    """Verify 'artemis server status/stop/restart' subcommands are accessible."""
    from artemis.runtime import server_lifecycle

    monkeypatch.setattr(server_lifecycle, "is_port_in_use", lambda port, **kwargs: False)
    monkeypatch.setattr(server_lifecycle, "find_server_pids", lambda port: [])
    monkeypatch.setattr(server_lifecycle, "read_server_info", lambda: None)

    result = runner.invoke(app, ["server", "status", "--port", "59998"])
    assert result.exit_code == 0
    assert "OFFLINE" in result.output or "STOPPED" in result.output

    help_result = runner.invoke(app, ["server", "--help"])
    assert help_result.exit_code == 0
    assert "restart" in help_result.output
    assert "stop" in help_result.output
    assert "status" in help_result.output


# ---------------------------------------------------------------------------
# artemis doctor (readiness-engine backed)
# ---------------------------------------------------------------------------

_RAW_SECRET = "sk-RAWSECRET0123456789abcdef"


def _probe(
    probe_id: str,
    *,
    status="pass",
    blocker: bool = True,
    summary: str = "OK",
    description: str = "fine",
    actions=(),
    metadata=None,
):
    from artemis.core.diagnostics.schema import (
        ProbeAction,
        ProbeCategory,
        ProbeResult,
        ProbeStatus,
    )

    return ProbeResult(
        id=probe_id,
        category=ProbeCategory.RUNTIME,
        title=f"Title {probe_id}",
        status=ProbeStatus(status),
        is_blocker=blocker,
        summary=summary,
        description=description,
        metadata=metadata or {},
        actions=[ProbeAction(action_type=t, label=lbl, payload=p) for t, lbl, p in actions],
    )


def _all_pass_probes():
    return [
        _probe("vision_ocr_key", blocker=False, summary="Not Configured (Optional)"),
        _probe("toolchain", blocker=False),
        _probe("android_adb", metadata={"installed": True, "adb_keys": {"is_corrupted": False}}),
        _probe(
            "gemini_api_key",
            summary="Active (Gemini)",
            description="Gemini credential is active (sk-RAW...cdef).",
            metadata={
                "current_key": _RAW_SECRET,
                "api_keys": {"google": _RAW_SECRET},
                "providers": [{"raw_key": _RAW_SECRET, "key": _RAW_SECRET}],
            },
        ),
        _probe("system_config"),
        _probe("python_runtime"),
    ]


def _install_doctor_fakes(monkeypatch, probes, host=None, *, heal_result=None):
    """Patch the engine, the host probe, and the CLI-only extra rows."""
    from unittest.mock import AsyncMock
    import time

    from artemis.core.diagnostics.engine import readiness_engine
    from artemis.core.diagnostics.probes.host_probe import IntegrationHostProbe
    from artemis.core.diagnostics.schema import ProbeStatus, SystemReadinessReport
    import artemis.interfaces.cli.commands.doctor as doctor_module

    monkeypatch.setenv("COLUMNS", "200")
    blockers = [p for p in probes if p.is_blocker]
    report = SystemReadinessReport(
        overall_ready=all(p.status is ProbeStatus.PASS for p in blockers),
        blocker_count=len(blockers),
        passed_blocker_count=sum(p.status is ProbeStatus.PASS for p in blockers),
        probes=list(probes),
        os_type="windows",
        timestamp=time.time(),
    )
    run_all = AsyncMock(return_value=report)
    monkeypatch.setattr(readiness_engine, "run_all", run_all)
    heal = AsyncMock(return_value=heal_result or {"success": True, "message": "Keys regenerated."})
    monkeypatch.setattr(readiness_engine, "heal_adb_keys", heal)
    host_probe = AsyncMock(
        return_value=host or _probe("integration_host", summary="Host Ready", description="host ok")
    )
    monkeypatch.setattr(IntegrationHostProbe, "probe", host_probe)
    monkeypatch.setattr(
        doctor_module,
        "_npm_row",
        lambda: doctor_module.ExtraRow(
            key="nodejs_npm",
            title="Node.js / npm",
            status="pass",
            status_markup="[bold green]✔ Installed[/bold green]",
            summary="Installed",
            detail="/usr/bin/npm",
        ),
    )
    monkeypatch.setattr(
        doctor_module,
        "_showcase_row",
        lambda: doctor_module.ExtraRow(
            key="showcase_ui",
            title="Showcase UI",
            status="missing",
            status_markup="[bold yellow]○ Not Compiled[/bold yellow]",
            summary="Not Compiled",
            detail="Run ./start.sh or artemis ui to auto-compile.",
        ),
    )
    return {"run_all": run_all, "heal": heal, "host": host_probe}


def test_cli_doctor_all_pass_renders_table_in_fix_order(monkeypatch):
    """All-green run: engine rows in fix order, extras last, exit 0, ready footer."""
    _install_doctor_fakes(monkeypatch, _all_pass_probes())

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Artemis System & Environment Doctor" in out
    assert "Details & Recommendations" in out
    assert out.count("✔ OK") == 7
    assert "Node.js / npm" in out and "Showcase UI" in out
    assert "All system checks passed" in out
    assert 'artemis run "Open Settings and check Battery level"' in out

    order = [
        "Title python_runtime",
        "Title system_config",
        "Title integration_host",
        "Title gemini_api_key",
        "Title android_adb",
        "Title toolchain",
        "Title vision_ocr_key",
        "Node.js / npm",
        "Showcase UI",
    ]
    positions = [out.index(name) for name in order]
    assert positions == sorted(positions)


def test_cli_doctor_blocker_failure_shows_run_lines_and_splits_chains(monkeypatch):
    """A failing blocker prints each action on its own line; && chains are split."""
    probes = [p for p in _all_pass_probes() if p.id != "android_adb"]
    probes.append(
        _probe(
            "android_adb",
            status="fail",
            summary="ADB Missing",
            description="adb was not found.",
            actions=[
                ("command", "Install", "winget install Google.PlatformTools && adb start-server"),
                ("hint", "Enable USB", "Enable USB debugging on the phone."),
                ("link", "Docs", "https://developer.android.com/tools/adb"),
            ],
            metadata={"installed": False},
        )
    )
    _install_doctor_fakes(monkeypatch, probes)

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1, result.output
    out = result.output
    assert "✖ ADB Missing" in out
    assert "Run: winget install Google.PlatformTools" in out
    assert "Run: adb start-server" in out
    assert "&&" not in out
    assert "Enable USB debugging on the phone." in out
    assert "https://developer.android.com/tools/adb" in out
    assert "Action Required" in out
    assert "artemis init" in out
    assert "mobile_diagnose" in out


def test_cli_doctor_optional_failure_is_degraded_not_blocked(monkeypatch):
    """Non-blocker FAIL renders as Optional, keeps exit 0, and yields 'degraded'."""
    import json

    probes = [p for p in _all_pass_probes() if p.id != "toolchain"]
    probes.append(
        _probe(
            "toolchain",
            status="fail",
            blocker=False,
            summary="Missing FFmpeg",
            actions=[("command", "Install", "winget install Gyan.FFmpeg")],
        )
    )
    _install_doctor_fakes(monkeypatch, probes)

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "⚪ Optional" in result.output
    assert "Run: winget install Gyan.FFmpeg" in result.output
    assert "optional gaps" in result.output

    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["verdict"] == "degraded"


def test_cli_doctor_never_prints_raw_keys_from_metadata(monkeypatch):
    """Probe metadata (which carries raw credentials) must not reach the terminal or JSON."""
    _install_doctor_fakes(monkeypatch, _all_pass_probes())

    table = runner.invoke(app, ["doctor"])
    assert table.exit_code == 0, table.output
    assert "RAWSECRET" not in table.output
    assert "sk-RAW...cdef" in table.output  # the masked description is still shown

    as_json = runner.invoke(app, ["doctor", "--json"])
    assert as_json.exit_code == 0, as_json.output
    assert "RAWSECRET" not in as_json.output
    assert "metadata" not in as_json.output


def test_cli_doctor_json_shape_and_ready_verdict(monkeypatch):
    """--json emits the documented document shape and nothing else."""
    import json

    _install_doctor_fakes(monkeypatch, _all_pass_probes())

    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert set(doc) == {"verdict", "checks", "extras", "fixes"}
    assert doc["verdict"] == "ready"
    assert doc["fixes"] == []
    assert [c["id"] for c in doc["checks"]] == [
        "python_runtime",
        "system_config",
        "integration_host",
        "gemini_api_key",
        "android_adb",
        "toolchain",
        "vision_ocr_key",
    ]
    for check in doc["checks"]:
        assert set(check) == {"id", "title", "status", "required", "summary", "detail", "fix"}
        assert check["status"] == "pass"
        assert check["fix"] == []
    assert set(doc["extras"]) == {"nodejs_npm", "showcase_ui"}
    assert doc["extras"]["nodejs_npm"]["status"] == "pass"
    assert doc["extras"]["showcase_ui"]["status"] == "missing"


def test_cli_doctor_json_blocked_verdict_and_exit_code(monkeypatch):
    """A blocker that is not PASS makes the verdict 'blocked' and the exit code 1."""
    import json

    probes = [p for p in _all_pass_probes() if p.id != "gemini_api_key"]
    probes.append(
        _probe(
            "gemini_api_key",
            status="fail",
            summary="Key Missing",
            description="No LLM credential found.",
            actions=[
                ("command", "Run Artemis Init", "artemis init"),
                ("link", "Get Key", "https://aistudio.google.com/app/apikey"),
            ],
        )
    )
    _install_doctor_fakes(monkeypatch, probes)

    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 1, result.output
    doc = json.loads(result.output)
    assert doc["verdict"] == "blocked"
    key_check = next(c for c in doc["checks"] if c["id"] == "gemini_api_key")
    assert key_check["status"] == "fail"
    assert key_check["required"] is True
    assert key_check["fix"] == [
        {"type": "command", "label": "Run Artemis Init", "payload": "artemis init"},
        {"type": "link", "label": "Get Key", "payload": "https://aistudio.google.com/app/apikey"},
    ]


def test_cli_doctor_host_warn_degrades_and_keeps_the_fix(monkeypatch):
    """A host WARN is not a blocker (the runner works around it): the doctor
    exits 0 with a degraded verdict and still prints the fix."""
    host = _probe(
        "integration_host",
        status="warn",
        blocker=False,
        summary="Host Warning",
        description="MCP server runs on the wrong interpreter.",
        actions=[("command", "Regenerate", "uv run artemis mcp --install cursor")],
    )
    _install_doctor_fakes(monkeypatch, _all_pass_probes(), host=host)

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "⚠ Host Warning" in result.output
    assert "Run: uv run artemis mcp --install cursor" in result.output


def test_cli_doctor_host_fail_blocks_and_uses_summary(monkeypatch):
    host = _probe(
        "integration_host",
        status="fail",
        summary="Host Misconfigured",
        description="traces directory is not writable.",
    )
    _install_doctor_fakes(monkeypatch, _all_pass_probes(), host=host)

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1, result.output
    assert "✖ Host Misconfigured" in result.output


def test_cli_doctor_fix_heals_corrupted_keys_and_sweeps_locks(monkeypatch):
    """--fix heals ADB keys when the probe reports corruption and sweeps stale locks."""
    from artemis.runtime.device_lock import DeviceExecutionLock

    probes = [p for p in _all_pass_probes() if p.id != "android_adb"]
    probes.append(
        _probe(
            "android_adb",
            status="fail",
            summary="Keys Corrupted",
            metadata={"installed": True, "adb_keys": {"is_corrupted": True}},
        )
    )
    fakes = _install_doctor_fakes(monkeypatch, probes)
    cleanup_calls: list = []
    monkeypatch.setattr(
        DeviceExecutionLock,
        "cleanup_stale_locks",
        classmethod(lambda cls, device_id=None: cleanup_calls.append(device_id) or 3),
    )

    result = runner.invoke(app, ["doctor", "--fix"])
    fakes["heal"].assert_awaited_once()
    assert cleanup_calls == [None]
    assert fakes["run_all"].await_count == 2  # report is re-collected after the repairs
    assert "Repairs (--fix)" in result.output
    assert "Keys regenerated." in result.output
    assert "Removed 3 stale device lock(s)" in result.output


def test_cli_doctor_fix_skips_heal_when_keys_are_healthy(monkeypatch):
    """--fix does not touch healthy ADB keys but still sweeps stale locks."""
    from artemis.runtime.device_lock import DeviceExecutionLock

    fakes = _install_doctor_fakes(monkeypatch, _all_pass_probes())
    monkeypatch.setattr(
        DeviceExecutionLock, "cleanup_stale_locks", classmethod(lambda cls, device_id=None: 0)
    )

    result = runner.invoke(app, ["doctor", "--fix"])
    assert result.exit_code == 0, result.output
    fakes["heal"].assert_not_awaited()
    assert "nothing to repair" in result.output
    assert "No stale device locks" in result.output
