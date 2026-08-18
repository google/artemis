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

"""Universal MCP Multi-Environment Notification System."""

from typing import Any, Dict, Optional

from mcp_server.notifiers.agentapi import AgentApiNotifier
from mcp_server.notifiers.base import BaseNotifier
from mcp_server.notifiers.composite import CompositeNotifier
from mcp_server.notifiers.desktop import DesktopNotifier
from mcp_server.notifiers.file import FileNotifier
from mcp_server.notifiers.webhook import WebhookNotifier

_default_notifier = CompositeNotifier()


def get_default_notifier() -> CompositeNotifier:
    """Returns the global default composite notifier instance."""
    return _default_notifier


def notify(
    conversation_id: str,
    message: str,
    title: str | None = None,
    event_type: str = "completed",
    payload: dict[str, Any] | None = None,
) -> bool:
    """Dispatches a notification across all available channels (AgentAPI, Webhook, Desktop, File)."""
    return _default_notifier.notify(
        conversation_id=conversation_id,
        message=message,
        title=title,
        event_type=event_type,
        payload=payload,
    )


def notify_jetski(conversation_id: str, message: str) -> bool:
    """Backward-compatible alias for notify()."""
    return notify(conversation_id=conversation_id, message=message, event_type="completed")


__all__ = [
    "BaseNotifier",
    "AgentApiNotifier",
    "WebhookNotifier",
    "DesktopNotifier",
    "FileNotifier",
    "CompositeNotifier",
    "get_default_notifier",
    "notify",
    "notify_jetski",
]
