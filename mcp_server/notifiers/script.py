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


def _tokenize_command_template(template: str) -> list[str]:
    """Split a command template into argv tokens.

    Recognizes single/double-quoted runs so a placeholder like '{message}'
    or a quoted path like "C:\\Program Files\\x.exe" stays one token, and
    strips the matching outer quote characters.

    Deliberately does NOT treat backslash as an escape character. stdlib
    shlex's posix=True mode does, which silently strips backslashes from any
    *unquoted* token - corrupting an ordinary unquoted Windows path (e.g.
    C:\\Tools\\notify.exe) with no error raised. Its posix=False mode avoids
    that but then leaves literal quote characters inside a *quoted* path
    token (e.g. "C:\\Program Files\\x.exe" keeps its surrounding quotes),
    which Windows then fails to resolve as an executable (WinError 5).
    Neither posix mode works for both quoted and unquoted Windows paths, and
    branching on os.name to pick one just swaps which case breaks. Not
    special-casing backslash at all avoids the whole problem on both
    platforms.
    """
    tokens: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for ch in template:
        if quote:
            if ch == quote:
                quote = None
            else:
                current.append(ch)
        elif ch in ("'", '"'):
            quote = ch
        elif ch.isspace():
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(ch)
    if quote:
        raise ValueError(f"unbalanced {quote!r} quote in command template")
    if current:
        tokens.append("".join(current))
    return tokens


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
        values = {
            "{title}": str(formatted_title),
            "{message}": str(message),
            "{conversation_id}": str(conversation_id),
            "{event_type}": str(event_type),
            "{trace_id}": str(trace_id),
        }

        # Tokenize the *template* (trusted: set by whoever configures
        # ARTEMIS_NOTIFY_CMD/MCP_NOTIFY_COMMAND on their own machine) so
        # quoting like 'my-script --message "{message}"' resolves to argv
        # boundaries the way the operator intended. Placeholders are then
        # substituted as literal text *within* each resulting token and the
        # argv list is executed with shell=False. Untrusted values (message,
        # title, ...; ultimately derived from task text and on-device content
        # an agent read) are never interpreted by a shell, so quotes,
        # backticks, `$()`, `;`, `&&`, etc. embedded in them can't break out
        # into additional commands - there's no shell parser in the loop for
        # them to break out of. See _tokenize_command_template for why this
        # uses a purpose-built tokenizer instead of stdlib shlex.
        #
        # Trade-off: this intentionally drops any implicit shell features the
        # template itself relied on (pipelines, redirection, `&&`, `$VAR`
        # expansion). Templates that explicitly re-invoke a shell (`sh -c ...`,
        # `cmd /c ...`) or call a `.bat`/`.cmd` file directly are unaffected by
        # this fix - those hand the whole assembled string back to a shell
        # themselves and remain the operator's responsibility to write safely.
        try:
            try:
                tokens = _tokenize_command_template(cmd_template)
            except ValueError as e:
                logger.warning(f"Invalid ARTEMIS_NOTIFY_CMD/MCP_NOTIFY_COMMAND template: {e}")
                return False

            if not tokens:
                logger.warning("Empty command template after tokenization")
                return False

            argv = []
            for token in tokens:
                for placeholder, value in values.items():
                    token = token.replace(placeholder, value)
                argv.append(token)

            subprocess.run(
                argv,
                shell=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            logger.info(f"Custom script notification command executed: {argv[0]!r} (+{len(argv) - 1} args)")
            return True
        except Exception as e:
            logger.warning(f"Failed to execute custom script notification: {e}")
            return False
