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

"""JSON Lines Telemetry Storage Backend."""

import json
from pathlib import Path
from typing import Any
from artemis.telemetry.models import TelemetrySpan


class JsonStorage:
    """Persists spans into newline-delimited JSON files."""

    def __init__(self, storage_dir: Path | str = "traces"):
        self.storage_dir = Path(storage_dir)

    def write_span(self, trace_id: str, span: TelemetrySpan) -> None:
        target_dir = self.storage_dir / trace_id
        target_dir.mkdir(parents=True, exist_ok=True)
        log_file = target_dir / "spans.jsonl"

        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(span.model_dump(), ensure_ascii=False) + "\n")

    def read_spans(self, trace_id: str) -> list[dict[str, Any]]:
        log_file = self.storage_dir / trace_id / "spans.jsonl"
        if not log_file.exists():
            return []

        spans = []
        with open(log_file, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    spans.append(json.loads(line))
        return spans
