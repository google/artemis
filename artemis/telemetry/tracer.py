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

"""Telemetry Tracer and Span Life Cycle Manager."""

from contextlib import asynccontextmanager
from contextvars import ContextVar
import json
from pathlib import Path
from typing import Any
from collections.abc import AsyncIterator
from uuid import uuid4

from artemis.telemetry.models import SpanType, TelemetrySpan
from artemis.utils.logger import get_logger

logger = get_logger(__name__)

CURRENT_TRACE_ID: ContextVar[str] = ContextVar("CURRENT_TRACE_ID", default="global-session")
CURRENT_PARENT_SPAN_ID: ContextVar[str | None] = ContextVar("CURRENT_PARENT_SPAN_ID", default=None)


class TelemetryTracer:
    """Central tracer for recording and persisting telemetry spans."""

    def __init__(self, trace_id: str | None = None, traces_dir: Path | None = None):
        self.trace_id = trace_id or str(uuid4())
        self.traces_dir = traces_dir or Path("traces") / self.trace_id
        self.spans: list[TelemetrySpan] = []
        self._log_file: Path | None = None

    def _ensure_log_file(self) -> Path:
        if self._log_file is None:
            self.traces_dir.mkdir(parents=True, exist_ok=True)
            self._log_file = self.traces_dir / "telemetry_spans.jsonl"
        return self._log_file

    def record_span(self, span: TelemetrySpan) -> None:
        """Appends and persists completed span."""
        self.spans.append(span)
        try:
            log_file = self._ensure_log_file()
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(span.model_dump(), ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"Failed to persist telemetry span {span.span_id}: {e}")

    @asynccontextmanager
    async def start_span(
        self,
        name: str,
        span_type: SpanType = SpanType.CUSTOM,
        payload: dict[str, Any] | None = None,
    ) -> AsyncIterator[TelemetrySpan]:
        """Asynchronous context manager creating and recording a span."""
        parent_id = CURRENT_PARENT_SPAN_ID.get()
        span = TelemetrySpan(
            trace_id=self.trace_id,
            parent_id=parent_id,
            name=name,
            type=span_type,
            payload=payload or {},
        )

        token = CURRENT_PARENT_SPAN_ID.set(span.span_id)
        try:
            yield span
            span.finish(status="success")
        except Exception as e:
            span.finish(status="failed", error=str(e))
            raise
        finally:
            CURRENT_PARENT_SPAN_ID.reset(token)
            self.record_span(span)
