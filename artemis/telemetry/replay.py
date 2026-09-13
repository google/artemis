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

"""Trace Replay and Inspection utilities."""

from pathlib import Path
from typing import Any
from artemis.telemetry.storage.json_storage import JsonStorage


class TraceReplayer:
    """Loads and formats historic task traces for replay and visual inspection."""

    def __init__(self, traces_dir: Path | str = "traces"):
        self.storage = JsonStorage(storage_dir=traces_dir)

    def get_trace_summary(self, trace_id: str) -> dict[str, Any]:
        spans = self.storage.read_spans(trace_id)
        return {
            "trace_id": trace_id,
            "total_spans": len(spans),
            "spans": spans,
        }
