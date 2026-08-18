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

"""File-based notification recorder."""

import json
import logging
import os
import time
from typing import Any

from mcp_server.notifiers.base import BaseNotifier
from mcp_server.utils import trace_store

logger = logging.getLogger("mcp_server.notifiers.file")


class FileNotifier(BaseNotifier):
    """Notifier that records all notification events directly to the trace directory."""

    @property
    def name(self) -> str:
        return "file"

    def is_available(self) -> bool:
        return True

    def notify(
        self,
        conversation_id: str,
        message: str,
        title: str | None = None,
        event_type: str = "completed",
        payload: dict[str, Any] | None = None,
    ) -> bool:
        trace_id = (payload or {}).get("trace_id")
        if not trace_id:
            return False

        try:
            trace_dir = trace_store.get_trace_dir(trace_id)
            os.makedirs(trace_dir, exist_ok=True)
            log_file = os.path.join(trace_dir, "notifications.jsonl")

            entry = {
                "timestamp": time.time(),
                "event_type": event_type,
                "title": title,
                "conversation_id": conversation_id,
                "message": message,
                "payload": payload or {},
            }
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return True
        except Exception as e:
            logger.debug(f"File notification record failed: {e}")
            return False
