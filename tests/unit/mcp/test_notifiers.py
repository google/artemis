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
from mcp_server.utils import trace_store


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


def test_desktop_notifier_not_enabled(monkeypatch):
    monkeypatch.delenv("ARTEMIS_DESKTOP_NOTIFY", raising=False)
    notifier = DesktopNotifier()
    assert notifier.is_available() is False


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


def test_global_notify_function():
    res = notify(
        conversation_id="conv-xyz",
        message="Test message",
        payload={"trace_id": "test-trace"},
    )
    assert isinstance(res, bool)
