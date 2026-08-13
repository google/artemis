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

"""Unit tests for Telemetry & Span Lifecycle Tracing."""

import pytest
from pathlib import Path
from artemis.telemetry.models import SpanType
from artemis.telemetry.tracer import TelemetryTracer


@pytest.mark.asyncio
async def test_telemetry_span_lifecycle(tmp_path: Path):
    """Verify TelemetryTracer properly records and persists nested spans."""
    tracer = TelemetryTracer(trace_id="test-session-001", traces_dir=tmp_path)

    async with tracer.start_span("parent_agent_step", span_type=SpanType.AGENT) as parent_span:
        assert parent_span.status == "running"
        assert parent_span.name == "parent_agent_step"

        async with tracer.start_span(
            "child_device_action", span_type=SpanType.DEVICE_ACTION, payload={"action": "click"}
        ) as child_span:
            assert child_span.parent_id == parent_span.span_id
            assert child_span.payload["action"] == "click"

    # Spans should be persisted in JSONL
    assert len(tracer.spans) == 2
    child_recorded = tracer.spans[0]
    parent_recorded = tracer.spans[1]

    assert child_recorded.name == "child_device_action"
    assert child_recorded.status == "success"
    assert child_recorded.duration_ms is not None

    assert parent_recorded.name == "parent_agent_step"
    assert parent_recorded.status == "success"

    # File existence
    log_file = tmp_path / "telemetry_spans.jsonl"
    assert log_file.exists()
    content = log_file.read_text(encoding="utf-8")
    assert "child_device_action" in content
    assert "parent_agent_step" in content
