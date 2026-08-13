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

"""Android IME and keyboard text typing helper."""

import asyncio
import base64
from typing import Any
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


def _escape_for_adb_text(s: str) -> str:
    """Escapes special characters for adb shell input text."""
    return (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("'", "\\'")
        .replace("`", "\\`")
        .replace("$", "\\$")
        .replace("&", "\\&")
        .replace("|", "\\|")
        .replace(";", "\\;")
        .replace("<", "\\<")
        .replace(">", "\\>")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .replace("*", "\\*")
        .replace("?", "\\?")
        .replace("~", "\\~")
        .replace(" ", "%s")
    )


class AndroidInputIME:
    """Handles text input, unicode typing, and key combination injection."""

    def __init__(self, adb_device: Any, ui_client: Any = None):
        self.device = adb_device
        self.ui_client = ui_client

    async def type_text(self, text: str, clear_existing: bool = True) -> bool:
        """Types text using adb input text, UIAutomator, or broadcast IME."""
        try:
            if clear_existing:
                # Safe & robust clearing: Move to End -> Shift+Home selection -> Delete -> Fallback backspaces
                clear_cmd = (
                    "input keyevent 123 && "
                    "input keyevent --meta 1 122 && "
                    "input keyevent 67 && "
                    "input keyevent 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67"
                )
                await asyncio.to_thread(self.device.shell, clear_cmd)
            else:
                # Append mode: move cursor to the end of existing text
                await asyncio.to_thread(
                    self.device.shell,
                    "input keyevent 123",  # KEYCODE_MOVE_END
                )

            # 1. Fast path for ASCII text: Use ADB input text directly.
            if text.isascii():
                lines = text.split("\n")
                for i, line in enumerate(lines):
                    if i > 0:
                        # Send Enter key between lines
                        await asyncio.to_thread(self.device.shell, "input keyevent 66")
                    if line:
                        escaped = _escape_for_adb_text(line)
                        await asyncio.to_thread(self.device.shell, f"input text {escaped}")
                return True

            # 2. For non-ASCII / Unicode text: Try UIAutomator client
            if self.ui_client and hasattr(self.ui_client, "send_text"):
                try:
                    res = self.ui_client.send_text(text)
                    if asyncio.iscoroutine(res):
                        await res
                    return True
                except Exception as e:
                    logger.warning(f"UIAutomator send_text failed: {e}")

            # 3. Check if ADBKeyboard is currently active
            try:
                default_ime = await asyncio.to_thread(
                    self.device.shell, "settings get secure default_input_method"
                )
                if "adbkeyboard" in str(default_ime).lower():
                    b64_text = base64.b64encode(text.encode("utf-8")).decode("utf-8")
                    broadcast_cmd = f"am broadcast -a ADB_INPUT_B64 --es msg '{b64_text}'"
                    await asyncio.to_thread(self.device.shell, broadcast_cmd)
                    return True
            except Exception:
                pass

            # 4. Fallback: ADB input text
            lines = text.split("\n")
            for i, line in enumerate(lines):
                if i > 0:
                    await asyncio.to_thread(self.device.shell, "input keyevent 66")
                if line:
                    escaped = _escape_for_adb_text(line)
                    await asyncio.to_thread(self.device.shell, f"input text {escaped}")
            return True
        except Exception as e:
            logger.error(f"Failed to type text '{text}': {e}")
            return False
