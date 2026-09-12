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

"""Desktop Toast / OS notification adapter."""

import logging
import os
import shutil
import subprocess
import sys
from typing import Any

from mcp_server.notifiers.base import BaseNotifier

logger = logging.getLogger("mcp_server.notifiers.desktop")


def escape_applescript_string(text: str) -> str:
    """Escapes a string for safe inclusion in an AppleScript double-quoted literal.

    - Escapes backslashes (\\ -> \\\\)
    - Escapes double quotes (" -> \\")
    - Replaces newlines and carriage returns with spaces
    - Preserves single quotes/apostrophes, Unicode, and punctuation safely
    """
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return escaped.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def escape_powershell_string(text: str) -> str:
    """Escapes a string for safe inclusion in a PowerShell double-quoted string.

    - Escapes backticks (` -> ``)
    - Escapes double quotes (" -> `")
    - Escapes dollar signs ($ -> `$)
    - Replaces newlines and carriage returns with spaces
    """
    escaped = text.replace("`", "``").replace('"', '`"').replace("$", "`$")
    return escaped.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


class DesktopNotifier(BaseNotifier):
    """Notifier that displays native desktop notifications across macOS, Linux, and Windows."""

    @property
    def name(self) -> str:
        return "desktop"

    def is_available(self) -> bool:
        try:
            val = os.getenv("ARTEMIS_DESKTOP_NOTIFY", "").lower()
            if val in ("0", "false", "no", "off"):
                return False
            if os.getenv("CI", "").lower() in ("1", "true", "yes") and val not in (
                "1",
                "true",
                "yes",
            ):
                return False
            if sys.platform == "linux" and not shutil.which("notify-send"):
                return False
            if sys.platform == "darwin" and not shutil.which("osascript"):
                return False
            return True
        except Exception:
            return False

    def notify(
        self,
        conversation_id: str,
        message: str,
        title: str | None = None,
        event_type: str = "completed",
        payload: dict[str, Any] | None = None,
    ) -> bool:
        if not self.is_available():
            return False

        header = title or f"☕ Artemis Task {event_type.capitalize()}"
        clean_body = message.split("\n\n")[0][:120]

        try:
            if sys.platform == "linux":
                if shutil.which("notify-send"):
                    subprocess.run(
                        ["notify-send", header, clean_body],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=3,
                    )
                    return True
            elif sys.platform == "darwin":
                if shutil.which("osascript"):
                    esc_body = escape_applescript_string(clean_body)
                    esc_header = escape_applescript_string(header)
                    script = f'display notification "{esc_body}" with title "{esc_header}"'
                    subprocess.run(
                        ["osascript", "-e", script],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=3,
                    )
                    return True
            elif sys.platform == "win32":
                esc_header = escape_powershell_string(header)
                esc_body = escape_powershell_string(clean_body)
                ps_cmd = (
                    f"[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null; "
                    f"$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
                    f'$textNodes = $template.GetElementsByTagName("text"); '
                    f'$textNodes.Item(0).AppendChild($template.CreateTextNode("{esc_header}")) > $null; '
                    f'$textNodes.Item(1).AppendChild($template.CreateTextNode("{esc_body}")) > $null; '
                    f"$toast = [Windows.UI.Notifications.ToastNotification]::new($template); "
                    f'[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Artemis").Show($toast);'
                )
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps_cmd],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
                return True
        except Exception as e:
            logger.debug(f"Desktop notification failed: {e}")
        return False
