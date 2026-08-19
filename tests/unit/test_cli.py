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
    """Verify 'artemis mcp --generate-config antigravity' produces valid configuration with tools."""
    result = runner.invoke(app, ["mcp", "--generate-config", "antigravity"])
    assert result.exit_code == 0
    assert "mcpServers" in result.output
    assert "mobile_run_task" in result.output
    assert "eager" in result.output


def test_cli_mcp_generate_config_all():
    """Verify 'artemis mcp --generate-config all' includes antigravity, cursor, windsurf, claude, vscode, cline, and roo."""
    result = runner.invoke(app, ["mcp", "--generate-config", "all"])
    assert result.exit_code == 0
    assert "antigravity" in result.output
    assert "cursor" in result.output
    assert "windsurf" in result.output
    assert "claude" in result.output
    assert "vscode" in result.output
    assert "cline" in result.output
    assert "roo" in result.output


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
    monkeypatch.setattr("mcp_server.utils.env_utils.get_project_root", lambda: str(tmp_path))
    result = runner.invoke(app, ["mcp", "--install", "all"])
    assert result.exit_code == 0
    assert "Successfully installed ARTEMIS MCP server configuration & rules" in result.output
    assert (tmp_path / ".cursor" / "mcp.json").exists()
    assert (tmp_path / ".codeium" / "windsurf" / "mcp_config.json").exists()
    assert (tmp_path / ".openclaw" / "openclaw.json").exists()
    assert (tmp_path / ".claude.json").exists()

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


