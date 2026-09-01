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

"""Failure classification, retry policies, and circuit breaking for LLM calls.

This is the single source of truth for how ARTEMIS reacts to a failed model
call.  Every provider/transport exception is converted into a ``Failure``
carrying explicit recovery decisions, and every retry loop in the codebase is
expected to consult ``retry_policy_for`` instead of inventing its own rules.

The taxonomy was promoted from ``artemis.agents.video_analyzer.reliability``
(which now extends it with media-specific categories) so that the same
classification and circuit-breaker quality applies to every agent call, not
just the video pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import random
import threading
import time
from typing import Any


class FailureCategory(StrEnum):
    RATE_LIMIT = "rate_limit"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    AUTHENTICATION = "authentication"
    BAD_REQUEST = "bad_request"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


#: Categories that count toward circuit-breaker state: transient provider-side
#: trouble where hammering the endpoint makes things worse.
TRANSIENT_CATEGORIES: frozenset[str] = frozenset(
    {
        FailureCategory.RATE_LIMIT.value,
        FailureCategory.PROVIDER_UNAVAILABLE.value,
        FailureCategory.TIMEOUT.value,
        FailureCategory.CONNECTION.value,
    }
)


@dataclass(frozen=True)
class Failure:
    category: FailureCategory
    retryable: bool
    should_fallback: bool


def extract_status_code(error: BaseException) -> int | None:
    """Best-effort extraction of an HTTP-ish status code from an exception."""
    code = getattr(error, "code", None) or getattr(error, "status_code", None)
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


_RATE_LIMIT_MARKERS = ("rate limit", "quota", "resource_exhausted")
_UNAVAILABLE_MARKERS = ("unavailable", "overloaded", "high demand", "service unavailable")
_AUTH_MARKERS = ("unauthorized", "forbidden", "api key", "credential")
# "deadline exceeded"/"deadline_exceeded" cover google-genai SDK timeouts that
# surface without an HTTP status code attached.
_TIMEOUT_MARKERS = ("timed out", "timeout", "deadline exceeded", "deadline_exceeded")


def classify_failure(error: BaseException) -> Failure:
    """Convert a provider/transport exception into explicit recovery decisions."""
    if isinstance(error, KeyboardInterrupt) or type(error).__name__ == "CancelledError":
        return Failure(FailureCategory.CANCELLED, False, False)

    code = extract_status_code(error)
    message = str(error).lower()

    if code == 429 or any(marker in message for marker in _RATE_LIMIT_MARKERS):
        return Failure(FailureCategory.RATE_LIMIT, True, True)
    if code in {500, 502, 503, 504} or any(marker in message for marker in _UNAVAILABLE_MARKERS):
        return Failure(FailureCategory.PROVIDER_UNAVAILABLE, True, True)
    if isinstance(error, TimeoutError) or any(marker in message for marker in _TIMEOUT_MARKERS):
        return Failure(FailureCategory.TIMEOUT, True, True)
    if isinstance(error, (ConnectionError, OSError)):
        return Failure(FailureCategory.CONNECTION, True, True)
    if code in {401, 403} or any(marker in message for marker in _AUTH_MARKERS):
        # Not retryable against the same endpoint, but a fallback endpoint may
        # use a different (working) credential.
        return Failure(FailureCategory.AUTHENTICATION, False, True)
    if code in {400, 404, 413, 415, 422}:
        # Programmer/request errors: retrying is useless and falling back only
        # hides the bug inside a different model's output.
        return Failure(FailureCategory.BAD_REQUEST, False, False)
    return Failure(FailureCategory.UNKNOWN, True, True)


# ---------------------------------------------------------------------------
# Typed terminal errors
# ---------------------------------------------------------------------------


class LLMCallError(Exception):
    """Base class for terminal LLM call failures raised by the gateway."""

    def __init__(self, message: str, *, failure: Failure, cause: BaseException | None = None):
        super().__init__(message)
        self.failure = failure
        if cause is not None:
            self.__cause__ = cause


class LLMPermanentError(LLMCallError):
    """A non-retryable failure (auth, bad request). Retrying cannot help."""


class LLMExhaustedError(LLMCallError):
    """Retryable failures exhausted every attempt and the pause deadline."""


# ---------------------------------------------------------------------------
# Retry policies
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryPolicy:
    """How many attempts a failure category deserves and how to space them."""

    max_attempts: int
    base_delay: float
    max_delay: float

    def delay_for(self, attempt: int) -> float:
        """Backoff delay before retry number ``attempt`` (1-based)."""
        delay = min(self.max_delay, self.base_delay * (2 ** max(0, attempt - 1)))
        return delay + random.uniform(0.1, 0.5)


_DEFAULT_RETRY_POLICIES: dict[FailureCategory, RetryPolicy] = {
    FailureCategory.RATE_LIMIT: RetryPolicy(max_attempts=4, base_delay=10.0, max_delay=60.0),
    FailureCategory.PROVIDER_UNAVAILABLE: RetryPolicy(
        max_attempts=4, base_delay=5.0, max_delay=30.0
    ),
    FailureCategory.TIMEOUT: RetryPolicy(max_attempts=3, base_delay=2.0, max_delay=15.0),
    FailureCategory.CONNECTION: RetryPolicy(max_attempts=3, base_delay=2.0, max_delay=15.0),
    FailureCategory.UNKNOWN: RetryPolicy(max_attempts=2, base_delay=1.0, max_delay=5.0),
    # Non-retryable categories get a single attempt by definition.
    FailureCategory.AUTHENTICATION: RetryPolicy(max_attempts=1, base_delay=0.0, max_delay=0.0),
    FailureCategory.BAD_REQUEST: RetryPolicy(max_attempts=1, base_delay=0.0, max_delay=0.0),
    FailureCategory.CANCELLED: RetryPolicy(max_attempts=1, base_delay=0.0, max_delay=0.0),
}


def retry_policy_for(category: FailureCategory | str) -> RetryPolicy:
    """Look up the retry policy for a failure category.

    Accepts the generic ``FailureCategory`` or any string-valued category from
    a domain extension (e.g. the video pipeline's ``VideoFailureCategory``).
    Extension-only categories fall back to the UNKNOWN policy, so callers must
    gate on ``failure.retryable`` before consulting attempt counts.
    """
    if not isinstance(category, FailureCategory):
        try:
            category = FailureCategory(str(category))
        except ValueError:
            category = FailureCategory.UNKNOWN
    return _DEFAULT_RETRY_POLICIES.get(category, _DEFAULT_RETRY_POLICIES[FailureCategory.UNKNOWN])


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


@dataclass
class _CircuitState:
    consecutive_failures: int = 0
    open_until: float = 0.0
    half_open_in_flight: bool = False


class CircuitBreaker:
    """Small circuit breaker keyed by provider/model identity.

    Only transient categories (see ``TRANSIENT_CATEGORIES``) trip the breaker;
    programmer errors and auth failures never open it.  ``failure`` may be any
    object with a ``category`` attribute whose value matches this module's
    category strings, so domain extensions (e.g. the video pipeline's failure
    type) plug in without conversion.
    """

    def __init__(self, threshold: int = 3, cooldown_seconds: float = 60.0) -> None:
        self.threshold = max(1, int(threshold))
        self.cooldown_seconds = max(0.1, float(cooldown_seconds))
        self._states: dict[str, _CircuitState] = {}
        self._lock = threading.RLock()

    def allow(self, key: str, *, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        with self._lock:
            state = self._states.get(key)
            if state is None:
                return True
            if state.open_until:
                if current < state.open_until or state.half_open_in_flight:
                    return False
                # Half-open: permit exactly one trial. Other concurrent calls
                # continue to bypass the primary until it succeeds or fails.
                state.half_open_in_flight = True
                state.consecutive_failures = max(0, self.threshold - 1)
                return True
            return not state.half_open_in_flight

    def open_remaining(self, key: str, *, now: float | None = None) -> float:
        """Seconds until the circuit for ``key`` half-opens (0 if not open)."""
        current = time.time() if now is None else now
        with self._lock:
            state = self._states.get(key)
            if state is None or not state.open_until:
                return 0.0
            return max(0.0, state.open_until - current)

    def record_success(self, key: str) -> None:
        with self._lock:
            self._states.pop(key, None)

    def record_failure(self, key: str, failure: Any, *, now: float | None = None) -> None:
        category = str(getattr(failure, "category", ""))
        if category not in TRANSIENT_CATEGORIES:
            with self._lock:
                state = self._states.get(key)
                if state is not None and state.half_open_in_flight:
                    self._states.pop(key, None)
            return
        current = time.time() if now is None else now
        with self._lock:
            state = self._states.setdefault(key, _CircuitState())
            state.half_open_in_flight = False
            state.consecutive_failures += 1
            if state.consecutive_failures >= self.threshold:
                state.open_until = current + self.cooldown_seconds

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {
                key: {
                    "consecutive_failures": state.consecutive_failures,
                    "open_until": state.open_until,
                    "half_open_in_flight": state.half_open_in_flight,
                }
                for key, state in self._states.items()
            }
