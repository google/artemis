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


def test_cli_batch_help():
    """Verify 'artemis batch --help' displays batch options."""
    result = runner.invoke(app, ["batch", "--help"])
    assert result.exit_code == 0
    assert "--file" in result.output
    assert "--delay" in result.output


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

    legacy_config = _get_config_snippet("jetski", "python", "/project")["mcpServers"][
        "artemis"
    ]
    assert set(legacy_config["tools"]) == {
        "mobile_run_task",
        "mobile_manage_task",
        "mobile_get_device_state",
        "mobile_inspect_trace",
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

    windsurf_data = json.loads(
        (tmp_path / ".codeium" / "windsurf" / "mcp_config.json").read_text()
    )
    assert windsurf_data["mcpServers"]["artemis"]["command"]

    claude_data = json.loads((tmp_path / ".claude.json").read_text())
    assert claude_data["mcpServers"]["artemis"]["type"] == "stdio"

    vscode_data = json.loads(
        (tmp_path / "AppData" / "Roaming" / "Code" / "User" / "mcp.json").read_text()
    )
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
    assert (tmp_path / ".claude" / "CLAUDE.md").exists()
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


