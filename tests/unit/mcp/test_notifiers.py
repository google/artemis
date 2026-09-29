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

"""Unit tests for MCP Multi-Environment Notifiers."""

import json
import os
import shutil
import tempfile
import pytest

from mcp_server.notifiers import (
    BaseNotifier,
    CompositeNotifier,
    DesktopNotifier,
    FileNotifier,
    WebhookNotifier,
    notify,
)
from artemis.runtime import trace_store


class DummyNotifier(BaseNotifier):
    def __init__(self, available: bool = True, return_val: bool = True):
        self._available = available
        self._return_val = return_val
        self.called_with = None

    @property
    def name(self) -> str:
        return "dummy"

    def is_available(self) -> bool:
        return self._available

    def notify(self, conversation_id, message, title=None, event_type="completed", payload=None):
        self.called_with = {
            "conversation_id": conversation_id,
            "message": message,
            "title": title,
            "event_type": event_type,
            "payload": payload,
        }
        return self._return_val


def test_file_notifier():
    temp_dir = tempfile.mkdtemp()
    try:
        trace_id = "test-file-trace-123"
        trace_dir = os.path.join(temp_dir, trace_id)
        os.makedirs(trace_dir, exist_ok=True)

        notifier = FileNotifier()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(trace_store, "TRACES_DIR", temp_dir)
            success = notifier.notify(
                conversation_id="conv-1",
                message="Task completed",
                title="Done",
                event_type="completed",
                payload={"trace_id": trace_id},
            )
            assert success is True

            log_file = os.path.join(trace_dir, "notifications.jsonl")
            assert os.path.exists(log_file)
            with open(log_file, encoding="utf-8") as f:
                lines = f.readlines()
                assert len(lines) == 1
                entry = json.loads(lines[0])
                assert entry["event_type"] == "completed"
                assert entry["message"] == "Task completed"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_webhook_notifier_not_configured(monkeypatch):
    for var in WebhookNotifier.ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    notifier = WebhookNotifier()
    assert notifier.is_available() is False
    assert notifier.notify("conv-1", "hello") is False


def test_desktop_notifier_disabled(monkeypatch):
    monkeypatch.setenv("ARTEMIS_DESKTOP_NOTIFY", "false")
    notifier = DesktopNotifier()
    assert notifier.is_available() is False


def test_desktop_notifier_enabled_by_default(monkeypatch):
    monkeypatch.delenv("ARTEMIS_DESKTOP_NOTIFY", raising=False)
    monkeypatch.delenv("CI", raising=False)
    import shutil
    import sys

    if sys.platform == "linux":
        monkeypatch.setattr(
            shutil, "which", lambda cmd: "/usr/bin/notify-send" if cmd == "notify-send" else None
        )
    elif sys.platform == "darwin":
        monkeypatch.setattr(
            shutil, "which", lambda cmd: "/usr/bin/osascript" if cmd == "osascript" else None
        )
    notifier = DesktopNotifier()
    assert notifier.is_available() is True


def test_script_notifier(monkeypatch):
    from mcp_server.notifiers.script import ScriptNotifier

    monkeypatch.delenv("ARTEMIS_NOTIFY_CMD", raising=False)
    monkeypatch.delenv("MCP_NOTIFY_COMMAND", raising=False)
    notifier = ScriptNotifier()
    assert notifier.is_available() is False

    monkeypatch.setenv("ARTEMIS_NOTIFY_CMD", "echo 'Notify: {title} - {message}'")
    assert notifier.is_available() is True
    res = notifier.notify("conv-123", "Task done", title="Success", payload={"trace_id": "t-1"})
    assert res is True


def test_script_notifier_rejects_shell_injection(monkeypatch, tmp_path):
    """Regression test for the ScriptNotifier command-injection issue.

    Values substituted into ARTEMIS_NOTIFY_CMD (title/message/etc.) can contain
    quotes, backticks, `$()`, `;`, `&&`, and other shell metacharacters -
    `message` in particular is built from free-text task descriptions and
    agent-produced results, neither of which is trusted or shell-safe. None of
    these should ever be able to run an additional command, regardless of how
    the operator quotes the placeholder in their template.
    """
    from mcp_server.notifiers.script import ScriptNotifier

    marker = tmp_path / "should_not_exist"
    payloads = [
        f"Goal: '; touch {marker}; echo '",
        f"hi `touch {marker}`",
        f"hi $(touch {marker})",
        f"hi; touch {marker}",
        f"hi && touch {marker}",
    ]

    templates = [
        # README's documented style: placeholder wrapped in single quotes.
        "echo --title '{title}' --message '{message}' --trace-id '{trace_id}'",
        # Equally plausible: placeholder wrapped in double quotes.
        'echo --message "{message}"',
    ]

    notifier = ScriptNotifier()
    for template in templates:
        monkeypatch.setenv("ARTEMIS_NOTIFY_CMD", template)
        for payload in payloads:
            res = notifier.notify("conv-inj", payload, title="T", payload={"trace_id": "t-1"})
            assert res is True
            assert not marker.exists(), (
                f"command injection executed via template={template!r} payload={payload!r}"
            )


def test_script_notifier_multiword_value_stays_one_argument(monkeypatch, tmp_path):
    """A multi-word message must reach the target program as a single argument,
    not be split on whitespace - this is what the shell=False + pre-tokenized
    argv approach buys us over naive string substitution."""
    from mcp_server.notifiers.script import ScriptNotifier

    out_file = tmp_path / "argv.txt"
    # %1 is what our test script echoes back; write argv[1] to a file so we can
    # assert it arrived as exactly one argument.
    script = tmp_path / "capture.sh"
    script.write_text(f'#!/bin/sh\nprintf "%s" "$1" > "{out_file}"\n')
    script.chmod(0o755)

    monkeypatch.setenv("ARTEMIS_NOTIFY_CMD", f"{script} '{{message}}'")
    notifier = ScriptNotifier()
    res = notifier.notify("conv-mw", "hello world with spaces", title="T", payload={"trace_id": "t-1"})
    assert res is True
    assert out_file.read_text() == "hello world with spaces"


def test_tokenize_command_template_windows_paths():
    """Regression test for a Windows-only bug: neither of stdlib shlex's posix
    modes tokenizes both quoted and unquoted Windows paths correctly (posix=True
    silently strips backslashes from an unquoted path; posix=False leaves literal
    quote characters in a quoted path, which Windows then can't resolve as an
    executable). The hand-rolled tokenizer must get both right, on every platform,
    with no os.name branching."""
    from mcp_server.notifiers.script import _tokenize_command_template

    quoted = _tokenize_command_template('"C:\\Program Files\\my-script.exe" --title \'{title}\'')
    assert quoted[0] == "C:\\Program Files\\my-script.exe"
    assert quoted[1:] == ["--title", "{title}"]

    unquoted = _tokenize_command_template("C:\\Tools\\notify.exe --title '{title}'")
    assert unquoted[0] == "C:\\Tools\\notify.exe"
    assert unquoted[1:] == ["--title", "{title}"]


def test_composite_notifier_dispatch():
    d1 = DummyNotifier(available=True, return_val=True)
    d2 = DummyNotifier(available=False, return_val=False)
    d3 = DummyNotifier(available=True, return_val=True)

    composite = CompositeNotifier(notifiers=[d1, d2, d3])
    assert composite.is_available() is True

    result = composite.notify(
        conversation_id="conv-abc",
        message="Hello World",
        title="Test Title",
        event_type="completed",
        payload={"key": "val"},
    )
    assert result is True
    assert d1.called_with is not None
    assert d1.called_with["conversation_id"] == "conv-abc"
    assert d2.called_with is None
    assert d3.called_with is not None


class BrokenNotifier(BaseNotifier):
    @property
    def name(self) -> str:
        return "broken"

    def is_available(self) -> bool:
        raise RuntimeError("Catastrophic error in is_available")

    def notify(self, conversation_id, message, title=None, event_type="completed", payload=None):
        raise RuntimeError("Catastrophic error in notify")


def test_composite_notifier_fault_tolerance():
    broken = BrokenNotifier()
    d1 = DummyNotifier(available=True, return_val=True)
    composite = CompositeNotifier(notifiers=[broken, d1])

    # Must never raise an exception even if a notifier crashes in is_available or notify
    assert composite.is_available() is True
    assert composite.notify("conv-test", "Message") is True
    assert d1.called_with is not None


def test_global_notify_function():
    res = notify(
        conversation_id="conv-xyz",
        message="Test message",
        payload={"trace_id": "test-trace"},
    )
    assert isinstance(res, bool)


def test_agentapi_notifier_candidate_recovery_and_retry(monkeypatch, tmp_path):
    from mcp_server.notifiers.agentapi import AgentApiNotifier
    import subprocess

    monkeypatch.delenv("ANTIGRAVITY_LS_ADDRESS", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_CSRF_TOKEN", raising=False)

    shared_file = tmp_path / ".test_jetski_env"
    monkeypatch.setattr(
        AgentApiNotifier,
        "_get_candidate_envs",
        lambda self, force_proc_scan=False: [
            ("localhost:9999", "bad-token"),
            ("localhost:1234", "good-token"),
        ],
    )

    saved_sessions = []
    monkeypatch.setattr(
        AgentApiNotifier,
        "_save_shared_env",
        lambda self, addr, token: saved_sessions.append((addr, token)),
    )
    monkeypatch.setattr(
        AgentApiNotifier,
        "_find_agentapi_path",
        lambda self: "/mock/bin/agentapi",
    )

    calls = []

    def mock_run(cmd, capture_output, text, check, timeout, env):
        calls.append(env.get("ANTIGRAVITY_LS_ADDRESS"))
        if env.get("ANTIGRAVITY_LS_ADDRESS") == "localhost:9999":
            raise subprocess.CalledProcessError(1, cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="success")

    monkeypatch.setattr(subprocess, "run", mock_run)

    notifier = AgentApiNotifier()
    success = notifier.notify("conv-test-123", "Task done!")
    assert success is True
    assert calls == ["localhost:9999", "localhost:1234"]
    assert saved_sessions == [("localhost:1234", "good-token")]
    assert os.environ["ANTIGRAVITY_LS_ADDRESS"] == "localhost:1234"
    assert os.environ["ANTIGRAVITY_CSRF_TOKEN"] == "good-token"
