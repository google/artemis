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
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=3,
                    )
                    return True
            elif sys.platform == "darwin":
                if shutil.which("osascript"):
                    # Untrusted content (message/title may echo text the agent
                    # observed on an attacker-controlled app or page) must never
                    # be spliced into the AppleScript source: any '"' or newline
                    # in it would let the injected text terminate the string
                    # literal and run as its own statement (e.g. `do shell
                    # script ...`). Pass it out of band via the environment and
                    # read it back with `system attribute` instead.
                    script = (
                        'display notification (system attribute "ARTEMIS_TOAST_BODY") '
                        'with title (system attribute "ARTEMIS_TOAST_TITLE")'
                    )
                    env = {**os.environ, "ARTEMIS_TOAST_BODY": clean_body, "ARTEMIS_TOAST_TITLE": header}
                    subprocess.run(
                        ["osascript", "-e", script],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=3,
                        env=env,
                    )
                    return True
            elif sys.platform == "win32":
                # Same class of risk as the macOS branch above: header/clean_body
                # must never be interpolated into the PowerShell source text.
                # Read them back from the environment instead so a quote or
                # semicolon in untrusted content can't break out of the
                # CreateTextNode(...) string literal and run as PowerShell.
                ps_cmd = (
                    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null; "
                    "$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
                    '$textNodes = $template.GetElementsByTagName("text"); '
                    "$textNodes.Item(0).AppendChild($template.CreateTextNode($env:ARTEMIS_TOAST_TITLE)) > $null; "
                    "$textNodes.Item(1).AppendChild($template.CreateTextNode($env:ARTEMIS_TOAST_BODY)) > $null; "
                    "$toast = [Windows.UI.Notifications.ToastNotification]::new($template); "
                    '[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Artemis").Show($toast);'
                )
                env = {**os.environ, "ARTEMIS_TOAST_TITLE": header, "ARTEMIS_TOAST_BODY": clean_body}
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps_cmd],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    env=env,
                )
                return True
        except Exception as e:
            logger.debug(f"Desktop notification failed: {e}")
        return False
