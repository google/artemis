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

"""Composite Notifier coordinating multi-channel notifications."""

import logging
from typing import Any

from mcp_server.notifiers.agentapi import AgentApiNotifier
from mcp_server.notifiers.base import BaseNotifier
from mcp_server.notifiers.desktop import DesktopNotifier
from mcp_server.notifiers.file import FileNotifier
from mcp_server.notifiers.webhook import WebhookNotifier

logger = logging.getLogger("mcp_server.notifiers.composite")


class CompositeNotifier(BaseNotifier):
    """Aggregates multiple notifiers and broadcasts events across all available channels."""

    def __init__(self, notifiers: list[BaseNotifier] | None = None):
        if notifiers is not None:
            self._notifiers = notifiers
        else:
            self._notifiers = [
                FileNotifier(),
                AgentApiNotifier(),
                WebhookNotifier(),
                DesktopNotifier(),
            ]

    @property
    def name(self) -> str:
        return "composite"

    def is_available(self) -> bool:
        return any(n.is_available() for n in self._notifiers)

    def register_notifier(self, notifier: BaseNotifier) -> None:
        """Registers an additional custom notifier."""
        self._notifiers.append(notifier)

    def notify(
        self,
        conversation_id: str,
        message: str,
        title: str | None = None,
        event_type: str = "completed",
        payload: dict[str, Any] | None = None,
    ) -> bool:
        any_success = False
        for notifier in self._notifiers:
            if notifier.is_available():
                try:
                    success = notifier.notify(
                        conversation_id=conversation_id,
                        message=message,
                        title=title,
                        event_type=event_type,
                        payload=payload,
                    )
                    if success:
                        any_success = True
                except Exception as e:
                    logger.debug(f"Notifier '{notifier.name}' encountered error: {e}")

        return any_success
