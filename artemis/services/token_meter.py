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

"""Session-level LLM token metering (M0: pure recording, no decisions).

Every gateway-managed LLM call reports its real ``usage_metadata`` here. The
meter accumulates per-session totals and records one ``llm_usage`` trace per
call into the DataEngine, carrying:

- the call's measured ``prompt_tokens`` — the last call's measured input is the
  best available estimate of the *current context base* for the next call
  (the compaction thresholds of M2/M3 will consume this, never char-count
  heuristics);
- provider-reported cache hits (Gemini ``cached_content_token_count`` /
  LangChain ``input_token_details.cache_read``) to establish the cache-hit-rate
  baseline;
- running per-session totals.

This module never raises into the LLM call path and makes no decisions.
"""

import threading
from typing import Any

from artemis.utils.logger import get_logger

logger = get_logger(__name__)

_LOCK = threading.Lock()
_METERS: dict[str, "SessionTokenMeter"] = {}


def extract_usage(response: Any) -> dict[str, int] | None:
    """Extracts unified token usage from an LLM response message, best-effort.

    Prefers LangChain's normalized ``usage_metadata``; falls back to raw
    provider shapes in ``response_metadata``. Returns None when the response
    carries no usable usage numbers.
    """
    usage: dict[str, Any] | None = None
    raw = getattr(response, "usage_metadata", None)
    if isinstance(raw, dict) and raw:
        usage = raw
    else:
        meta = getattr(response, "response_metadata", None)
        if isinstance(meta, dict):
            candidate = meta.get("usage_metadata") or meta.get("token_usage")
            if isinstance(candidate, dict) and candidate:
                usage = candidate
    if usage is None:
        return None

    def _int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    prompt = _int(
        usage.get("input_tokens") or usage.get("prompt_tokens") or usage.get("prompt_token_count")
    )
    completion = _int(
        usage.get("output_tokens")
        or usage.get("completion_tokens")
        or usage.get("candidates_token_count")
    )
    total = _int(usage.get("total_tokens") or usage.get("total_token_count")) or (
        prompt + completion
    )

    cached = 0
    details = usage.get("input_token_details")
    if isinstance(details, dict):
        cached = _int(details.get("cache_read"))
    if not cached:
        cached = _int(
            usage.get("cached_content_token_count")
            or usage.get("cachedContentTokenCount")
            or usage.get("cached_tokens")
        )

    if total <= 0:
        return None
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cached_tokens": cached,
    }


class SessionTokenMeter:
    """Accumulates measured LLM usage for one session."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._lock = threading.Lock()
        self.llm_calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cached_tokens = 0
        self.cache_hit_calls = 0
        self.last_prompt_tokens = 0

    def record(self, usage: dict[str, int], *, update_last_prompt: bool = True) -> dict[str, int]:
        """Accumulates one call's usage; returns a session snapshot.

        ``update_last_prompt=False`` accumulates the totals without touching
        ``last_prompt_tokens`` — background lens calls carry tiny prompts that
        must not masquerade as the live context base consumed by the L2/L3
        compaction thresholds.
        """
        with self._lock:
            self.llm_calls += 1
            self.prompt_tokens += usage.get("prompt_tokens", 0)
            self.completion_tokens += usage.get("completion_tokens", 0)
            cached = usage.get("cached_tokens", 0)
            self.cached_tokens += cached
            if cached > 0:
                self.cache_hit_calls += 1
            if update_last_prompt:
                self.last_prompt_tokens = usage.get("prompt_tokens", 0)
            return self.snapshot()

    def snapshot(self) -> dict[str, int]:
        return {
            "session_llm_calls": self.llm_calls,
            "session_prompt_tokens": self.prompt_tokens,
            "session_completion_tokens": self.completion_tokens,
            "session_cached_tokens": self.cached_tokens,
            "session_cache_hit_calls": self.cache_hit_calls,
        }


def get_meter(session_id: Any) -> SessionTokenMeter:
    """Returns (creating on first use) the meter for a session id."""
    key = str(session_id)
    with _LOCK:
        meter = _METERS.get(key)
        if meter is None:
            meter = SessionTokenMeter(key)
            _METERS[key] = meter
        return meter


def record_llm_usage(
    engine: Any,
    response: Any,
    *,
    source: str | None = None,
    update_last_prompt: bool = True,
) -> dict | None:
    """Meters one LLM response and records an ``llm_usage`` trace, best-effort.

    ``context_base_tokens`` in the payload is this call's measured prompt size —
    the running estimate of the live context for threshold decisions in later
    milestones. Returns the recorded payload, or None when nothing was
    recorded (no engine/session/usage). Never raises.
    """
    try:
        if engine is None:
            return None
        session_id = getattr(engine, "current_session_id", None)
        usage = extract_usage(response)
        if not session_id or usage is None:
            return None

        snapshot = get_meter(session_id).record(usage, update_last_prompt=update_last_prompt)
        payload: dict[str, Any] = {
            **usage,
            "context_base_tokens": usage["prompt_tokens"],
            **snapshot,
        }
        if source:
            payload["source"] = source

        engine.record_trace(
            type="llm_call",
            name="llm_usage",
            payload=payload,
            step_id=getattr(engine, "current_step_id", None),
            status="success",
        )
        return payload
    except Exception as meter_err:
        logger.debug(f"Token metering skipped: {meter_err}")
        return None
