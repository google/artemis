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

"""Session and per-source cache-hit ratios.

A session whose operator calls never hit the provider's prompt cache must be
visible without reading every ``llm_usage`` trace: the session snapshot and
each source's totals carry ``cached_ratio`` (cached / prompt tokens).
"""

import logging
from unittest.mock import Mock
from uuid import uuid4

from langchain_core.messages import AIMessage

from artemis.services.token_meter import (
    SessionTokenMeter,
    cached_ratio,
    format_session_summary,
    get_meter,
    log_session_summary,
    record_llm_usage,
)


def _msg(prompt: int, cached: int = 0) -> AIMessage:
    usage = {"input_tokens": prompt, "output_tokens": 10, "total_tokens": prompt + 10}
    if cached:
        usage["input_token_details"] = {"cache_read": cached}
    return AIMessage(content="ok", usage_metadata=usage)


def test_cached_ratio_is_zero_without_prompt_tokens():
    assert cached_ratio(0, 0) == 0.0
    assert cached_ratio(50, 0) == 0.0
    assert cached_ratio(250, 400) == 0.625


def test_meter_tracks_cached_ratio_per_session_and_per_source():
    meter = SessionTokenMeter("s")
    meter.record({"prompt_tokens": 1000, "cached_tokens": 0}, source="google:operator")
    meter.record({"prompt_tokens": 1000, "cached_tokens": 0}, source="google:operator")
    snapshot = meter.record(
        {"prompt_tokens": 200, "cached_tokens": 150},
        source="lens:step_capsule:gemini",
        update_last_prompt=False,
    )

    # Session ratio blends every call; the call's own source rides along.
    assert snapshot["session_cached_ratio"] == round(150 / 2200, 3)
    assert snapshot["source_cached_ratio"] == 0.75
    assert meter.last_prompt_tokens == 1000

    summary = meter.summary()
    assert summary["session_id"] == "s"
    assert summary["sources"]["google:operator"] == {
        "llm_calls": 2,
        "prompt_tokens": 2000,
        "cached_tokens": 0,
        "cache_hit_calls": 0,
        "cached_ratio": 0.0,  # the operator never hit the cache: visible at a glance
    }
    assert summary["sources"]["lens:step_capsule:gemini"]["cached_ratio"] == 0.75


def test_record_llm_usage_payload_carries_both_ratios():
    engine = Mock()
    engine.current_session_id = uuid4()
    engine.current_step_id = None

    record_llm_usage(engine, _msg(1000), source="google:op")
    payload = record_llm_usage(engine, _msg(1000, cached=800), source="google:op")

    assert payload["session_cached_ratio"] == 0.4
    assert payload["source_cached_ratio"] == 0.4
    # A source-less call still reports the session ratio, never a stale source one.
    payload = record_llm_usage(engine, _msg(1000))
    assert payload["session_cached_ratio"] == round(800 / 3000, 3)
    assert "source_cached_ratio" not in payload


def test_session_summary_logs_once_per_metered_session(caplog):
    session_id = f"ratio-{uuid4()}"
    assert log_session_summary(session_id) is None  # never metered: nothing to say

    meter = get_meter(session_id)
    meter.record({"prompt_tokens": 500, "cached_tokens": 0}, source="google:op")
    meter.record({"prompt_tokens": 100, "cached_tokens": 90}, source="lens:visual")

    line = format_session_summary(session_id)
    assert f"LLM usage for session {session_id}: 2 calls, 600 prompt tokens, 90 cached" in line
    assert "cached_ratio=0.150" in line
    assert "google:op: 1 calls, cached_ratio=0.000" in line
    assert "lens:visual: 1 calls, cached_ratio=0.900" in line

    with caplog.at_level(logging.INFO, logger="artemis.services.token_meter"):
        summary = log_session_summary(session_id)
    assert summary is not None and summary["session_cached_ratio"] == 0.15
    assert any(line in record.getMessage() for record in caplog.records)
