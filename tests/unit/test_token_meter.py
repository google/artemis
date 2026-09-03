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

"""Tests for the M0 session token metering pipeline (pure recording)."""

from unittest.mock import Mock
from uuid import uuid4

from langchain_core.messages import AIMessage

from artemis.services.token_meter import (
    SessionTokenMeter,
    extract_usage,
    get_meter,
    record_llm_usage,
)


def _msg_with_usage(prompt=1000, completion=50, cached=0):
    usage = {
        "input_tokens": prompt,
        "output_tokens": completion,
        "total_tokens": prompt + completion,
    }
    if cached:
        usage["input_token_details"] = {"cache_read": cached}
    return AIMessage(content="ok", usage_metadata=usage)


def test_extract_usage_from_usage_metadata():
    usage = extract_usage(_msg_with_usage(prompt=1200, completion=80, cached=900))
    assert usage == {
        "prompt_tokens": 1200,
        "completion_tokens": 80,
        "total_tokens": 1280,
        "cached_tokens": 900,
    }


def test_extract_usage_from_response_metadata_fallback():
    msg = AIMessage(content="ok")
    msg.response_metadata = {
        "usage_metadata": {
            "prompt_token_count": 500,
            "candidates_token_count": 25,
            "total_token_count": 525,
            "cached_content_token_count": 400,
        }
    }
    usage = extract_usage(msg)
    assert usage == {
        "prompt_tokens": 500,
        "completion_tokens": 25,
        "total_tokens": 525,
        "cached_tokens": 400,
    }


def test_extract_usage_absent_returns_none():
    assert extract_usage(AIMessage(content="no usage")) is None
    assert extract_usage(object()) is None


def test_meter_accumulates_session_totals():
    meter = SessionTokenMeter("s1")
    meter.record({"prompt_tokens": 100, "completion_tokens": 10, "cached_tokens": 0})
    snapshot = meter.record({"prompt_tokens": 300, "completion_tokens": 20, "cached_tokens": 250})
    assert snapshot == {
        "session_llm_calls": 2,
        "session_prompt_tokens": 400,
        "session_completion_tokens": 30,
        "session_cached_tokens": 250,
        "session_cache_hit_calls": 1,
    }
    assert meter.last_prompt_tokens == 300


def test_record_llm_usage_records_trace_with_context_base():
    engine = Mock()
    engine.current_session_id = uuid4()
    engine.current_step_id = uuid4()

    payload = record_llm_usage(
        engine, _msg_with_usage(prompt=2000, completion=100, cached=1500), source="google:test"
    )

    assert payload is not None
    assert payload["context_base_tokens"] == 2000
    assert payload["cached_tokens"] == 1500
    assert payload["source"] == "google:test"

    engine.record_trace.assert_called_once()
    kwargs = engine.record_trace.call_args.kwargs
    assert kwargs["type"] == "llm_call"
    assert kwargs["name"] == "llm_usage"
    assert kwargs["step_id"] == engine.current_step_id
    assert kwargs["payload"]["prompt_tokens"] == 2000


def test_record_llm_usage_accumulates_across_calls_same_session():
    engine = Mock()
    engine.current_session_id = uuid4()
    engine.current_step_id = None

    record_llm_usage(engine, _msg_with_usage(prompt=100, completion=5))
    payload = record_llm_usage(engine, _msg_with_usage(prompt=250, completion=10, cached=90))

    assert payload["session_llm_calls"] == 2
    assert payload["session_prompt_tokens"] == 350
    assert payload["session_cached_tokens"] == 90
    assert payload["session_cache_hit_calls"] == 1

    meter = get_meter(engine.current_session_id)
    assert meter.last_prompt_tokens == 250


def test_record_llm_usage_never_raises_and_skips_gracefully():
    # No engine
    assert record_llm_usage(None, _msg_with_usage()) is None

    # Engine without a session
    engine = Mock()
    engine.current_session_id = None
    assert record_llm_usage(engine, _msg_with_usage()) is None
    engine.record_trace.assert_not_called()

    # Response without usage
    engine2 = Mock()
    engine2.current_session_id = uuid4()
    assert record_llm_usage(engine2, AIMessage(content="nope")) is None
    engine2.record_trace.assert_not_called()

    # record_trace blowing up must not propagate
    engine3 = Mock()
    engine3.current_session_id = uuid4()
    engine3.record_trace.side_effect = RuntimeError("boom")
    assert record_llm_usage(engine3, _msg_with_usage()) is None


def test_record_llm_usage_lens_calls_do_not_touch_last_prompt_tokens():
    """Background lens calls accumulate totals but never overwrite the live
    context base (last_prompt_tokens) consumed by the compaction thresholds."""
    engine = Mock()
    engine.current_session_id = uuid4()
    engine.current_step_id = None

    record_llm_usage(engine, _msg_with_usage(prompt=15000))
    meter = get_meter(engine.current_session_id)
    assert meter.last_prompt_tokens == 15000

    payload = record_llm_usage(
        engine,
        _msg_with_usage(prompt=300, completion=40),
        source="lens:visual_transition:test-model",
        update_last_prompt=False,
    )
    assert payload["source"] == "lens:visual_transition:test-model"
    assert meter.last_prompt_tokens == 15000  # untouched by the lens call
    assert meter.llm_calls == 2
    assert meter.prompt_tokens == 15300
