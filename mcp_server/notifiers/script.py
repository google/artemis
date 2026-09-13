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

"""Custom Script / Command Hook Notifier for universal IDE and editor integration."""

import logging
import os
import subprocess
from typing import Any

from mcp_server.notifiers.base import BaseNotifier

logger = logging.getLogger("mcp_server.notifiers.script")


class ScriptNotifier(BaseNotifier):
    """Notifier that executes a user-defined command or script when an event occurs.

    This adapter enables universal integration with any custom IDE, editor (Neovim/Emacs),
    or automation platform by allowing users to define ARTEMIS_NOTIFY_CMD or MCP_NOTIFY_COMMAND.
    Placeholders like {title}, {message}, {conversation_id}, {event_type}, and {trace_id}
    are automatically replaced before execution.
    """

    ENV_VARS = [
        "ARTEMIS_NOTIFY_CMD",
        "MCP_NOTIFY_COMMAND",
    ]

    @property
    def name(self) -> str:
        return "script"

    def _get_command_template(self) -> str | None:
        for var in self.ENV_VARS:
            val = os.getenv(var)
            if val and val.strip():
                return val.strip()
        return None

    def is_available(self) -> bool:
        return self._get_command_template() is not None

    def notify(
        self,
        conversation_id: str,
        message: str,
        title: str | None = None,
        event_type: str = "completed",
        payload: dict[str, Any] | None = None,
    ) -> bool:
        cmd_template = self._get_command_template()
        if not cmd_template:
            return False

        trace_id = (payload or {}).get("trace_id", "")
        formatted_title = title or f"Artemis Task {event_type.capitalize()}"

        # Replace template placeholders safely
        try:
            cmd = (
                cmd_template.replace("{title}", str(formatted_title))
                .replace("{message}", str(message))
                .replace("{conversation_id}", str(conversation_id))
                .replace("{event_type}", str(event_type))
                .replace("{trace_id}", str(trace_id))
            )
            subprocess.run(
                cmd,
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            logger.info(f"Custom script notification command executed: {cmd[:60]}...")
            return True
        except Exception as e:
            logger.warning(f"Failed to execute custom script notification: {e}")
            return False
