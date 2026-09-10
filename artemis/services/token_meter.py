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

"""Session-level LLM token usage and cache-hit metering.

Every gateway-managed LLM call reports its real ``usage_metadata`` here. The
meter accumulates per-session totals and records one ``llm_usage`` trace per
call into the DataEngine, carrying:

- the call's measured ``prompt_tokens`` (``context_base_tokens`` in the trace
  payload, kept for the timeline). Note: the session meter's
  ``last_prompt_tokens`` is whichever call happened last in the session —
  Planner, Validator, Checker, sub-agents included — so the compaction
  thresholds do NOT read it; the operator records its own prompt size on the
  transcript ledger (``TranscriptLedger.record_prompt_tokens``);
- provider-reported cache hits (Gemini ``cached_content_token_count`` /
  LangChain ``input_token_details.cache_read``) to establish the cache-hit-rate
  baseline;
- running per-session totals, including ``session_cached_ratio``
  (cached / prompt tokens over the session) and the same ratio for the call's
  own ``source`` (``source_cached_ratio``), so a session whose operator calls
  never hit the prompt cache is visible on its ``llm_usage`` traces and in the
  session summary line (:func:`log_session_summary`).

This module never raises into the LLM call path and makes no decisions.
"""

import threading
from typing import Any

from artemis.data_engine.context_vars import CURRENT_NODE_NAME
from artemis.llm.google import usage_from_message
from artemis.utils.logger import get_logger

logger = get_logger(__name__)

_LOCK = threading.Lock()
_METERS: dict[str, "SessionTokenMeter"] = {}

# Size of the model context window the live Operator/FlashRunner prompt is
# measured against when the UI reports "context used". The prompt size of the
# last executor call (``context_base_tokens`` on its ``llm_usage`` trace) is
# the numerator; this is the denominator.
OPERATOR_CONTEXT_WINDOW_TOKENS = 1_000_000

# Traced node names whose LLM calls carry the live executor context. Both the
# Pro Operator and the Flash runner are "the operator" from the user's view.
OPERATOR_NODE_NAMES = frozenset({"operator", "flashrunner"})


def extract_usage(response: Any) -> dict[str, int] | None:
    """Extracts unified token usage from an LLM response message, best-effort.

    Thin wrapper over :func:`artemis.llm.google.usage.usage_from_message`
    that keeps the meter's historical four-key shape. Returns None when the
    response carries no usable usage numbers.
    """
    usage = usage_from_message(response)
    if usage is None:
        return None
    return {
        "prompt_tokens": usage["prompt_tokens"],
        "completion_tokens": usage["completion_tokens"],
        "total_tokens": usage["total_tokens"],
        "cached_tokens": usage["cached_tokens"],
    }


def cached_ratio(cached_tokens: int, prompt_tokens: int) -> float:
    """``cached / prompt`` rounded to three decimals; 0.0 with no prompt tokens."""
    if prompt_tokens <= 0:
        return 0.0
    return round(cached_tokens / prompt_tokens, 3)


class SessionTokenMeter:
    """Accumulates measured LLM usage for one session, in total and per source."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._lock = threading.Lock()
        self.llm_calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cached_tokens = 0
        self.cache_hit_calls = 0
        self.last_prompt_tokens = 0
        # Per-source totals (source = gateway endpoint key or lens label):
        # {"llm_calls", "prompt_tokens", "cached_tokens", "cache_hit_calls"}.
        self.sources: dict[str, dict[str, int]] = {}

    def record(
        self,
        usage: dict[str, int],
        *,
        update_last_prompt: bool = True,
        source: str | None = None,
    ) -> dict[str, Any]:
        """Accumulates one call's usage; returns a session snapshot.

        ``update_last_prompt=False`` accumulates the totals without touching
        ``last_prompt_tokens`` — background lens calls carry tiny prompts that
        must not masquerade as the live context base consumed by the L2/L3
        compaction thresholds. With a ``source`` the call also joins that
        source's totals and the snapshot carries ``source_cached_ratio``.
        """
        with self._lock:
            prompt = usage.get("prompt_tokens", 0)
            cached = usage.get("cached_tokens", 0)
            self.llm_calls += 1
            self.prompt_tokens += prompt
            self.completion_tokens += usage.get("completion_tokens", 0)
            self.cached_tokens += cached
            if cached > 0:
                self.cache_hit_calls += 1
            if update_last_prompt:
                self.last_prompt_tokens = prompt
            snapshot = self._snapshot_locked()
            if source:
                totals = self.sources.setdefault(
                    source,
                    {"llm_calls": 0, "prompt_tokens": 0, "cached_tokens": 0, "cache_hit_calls": 0},
                )
                totals["llm_calls"] += 1
                totals["prompt_tokens"] += prompt
                totals["cached_tokens"] += cached
                if cached > 0:
                    totals["cache_hit_calls"] += 1
                snapshot["source_cached_ratio"] = cached_ratio(
                    totals["cached_tokens"], totals["prompt_tokens"]
                )
            return snapshot

    def _snapshot_locked(self) -> dict[str, Any]:
        return {
            "session_llm_calls": self.llm_calls,
            "session_prompt_tokens": self.prompt_tokens,
            "session_completion_tokens": self.completion_tokens,
            "session_cached_tokens": self.cached_tokens,
            "session_cache_hit_calls": self.cache_hit_calls,
            "session_cached_ratio": cached_ratio(self.cached_tokens, self.prompt_tokens),
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()

    def summary(self) -> dict[str, Any]:
        """Session totals plus every source's totals with its ``cached_ratio``."""
        with self._lock:
            summary: dict[str, Any] = {"session_id": self.session_id, **self._snapshot_locked()}
            summary["sources"] = {
                source: {
                    **totals,
                    "cached_ratio": cached_ratio(totals["cached_tokens"], totals["prompt_tokens"]),
                }
                for source, totals in self.sources.items()
            }
            return summary


def format_session_summary(session_id: Any) -> str:
    """One log line: session cache ratio, then each source's calls and ratio."""
    summary = get_meter(session_id).summary()
    parts = [
        f"LLM usage for session {summary['session_id']}:"
        f" {summary['session_llm_calls']} calls,"
        f" {summary['session_prompt_tokens']} prompt tokens,"
        f" {summary['session_cached_tokens']} cached"
        f" (cached_ratio={summary['session_cached_ratio']:.3f},"
        f" cache-hit calls {summary['session_cache_hit_calls']})"
    ]
    for source, totals in summary["sources"].items():
        parts.append(
            f"{source}: {totals['llm_calls']} calls, cached_ratio={totals['cached_ratio']:.3f}"
        )
    return "; ".join(parts)


def log_session_summary(session_id: Any) -> dict[str, Any] | None:
    """Log the session's usage summary (INFO) at session end, best-effort.

    Returns the summary dict, or None when the session never metered a call
    (nothing is logged then). Never raises.
    """
    try:
        key = str(session_id)
        with _LOCK:
            meter = _METERS.get(key)
        if meter is None or meter.llm_calls == 0:
            return None
        logger.info(format_session_summary(key))
        return meter.summary()
    except Exception as exc:
        logger.debug(f"Token meter session summary skipped: {exc}")
        return None


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

        snapshot = get_meter(session_id).record(
            usage, update_last_prompt=update_last_prompt, source=source
        )
        payload: dict[str, Any] = {
            **usage,
            "context_base_tokens": usage["prompt_tokens"],
            **snapshot,
        }
        if source:
            payload["source"] = source
        node = CURRENT_NODE_NAME.get()
        if node:
            payload["node"] = str(node)

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
