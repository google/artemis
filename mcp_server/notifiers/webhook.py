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

"""Webhook Notifier for OpenClaw, CI pipelines, and external gateways."""

import json
import logging
import os
import time
from typing import Any
import urllib.request

from mcp_server.notifiers.base import BaseNotifier

logger = logging.getLogger("mcp_server.notifiers.webhook")


class WebhookNotifier(BaseNotifier):
    """Notifier that posts execution events to HTTP/HTTPS webhooks (e.g. OpenClaw / CI / Slack)."""

    ENV_VARS = [
        "OPENCLAW_WEBHOOK_URL",
        "MCP_NOTIFICATION_WEBHOOK",
        "ARTEMIS_WEBHOOK_URL",
    ]

    @property
    def name(self) -> str:
        return "webhook"

    def _get_webhook_url(self) -> str | None:
        for var in self.ENV_VARS:
            val = os.getenv(var)
            if val and val.startswith(("http://", "https://")):
                return val
        return None

    def is_available(self) -> bool:
        return self._get_webhook_url() is not None

    def notify(
        self,
        conversation_id: str,
        message: str,
        title: str | None = None,
        event_type: str = "completed",
        payload: dict[str, Any] | None = None,
    ) -> bool:
        url = self._get_webhook_url()
        if not url:
            return False

        data = {
            "event": event_type,
            "title": title or f"Artemis Task {event_type.capitalize()}",
            "conversation_id": conversation_id,
            "message": message,
            "timestamp": time.time(),
            "payload": payload or {},
        }

        try:
            req_data = json.dumps(data).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=req_data,
                headers={"Content-Type": "application/json", "User-Agent": "Artemis-MCP/3.0"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                if response.status in (200, 201, 202, 204):
                    logger.info(f"Webhook notification delivered to {url}.")
                    return True
        except Exception as e:
            logger.warning(f"Failed to deliver webhook notification to {url}: {e}")
        return False
