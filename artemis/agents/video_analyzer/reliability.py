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

"""Failure classification and circuit breaking for video model calls."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import threading
import time
from typing import Any


class VideoFailureCategory(StrEnum):
    RATE_LIMIT = "rate_limit"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    AUTHENTICATION = "authentication"
    BAD_REQUEST = "bad_request"
    MEDIA_PROCESSING = "media_processing"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class VideoFailure:
    category: VideoFailureCategory
    retryable: bool
    should_split: bool
    should_fallback: bool


def classify_video_failure(error: BaseException) -> VideoFailure:
    """Convert provider/transport/media exceptions into recovery decisions."""

    if isinstance(error, KeyboardInterrupt) or type(error).__name__ == "CancelledError":
        return VideoFailure(VideoFailureCategory.CANCELLED, False, False, False)

    code = getattr(error, "code", None) or getattr(error, "status_code", None)
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = None

    message = str(error).lower()
    if code == 429 or any(marker in message for marker in ("rate limit", "quota", "resource_exhausted")):
        return VideoFailure(VideoFailureCategory.RATE_LIMIT, True, False, True)
    if code in {500, 502, 503, 504} or any(
        marker in message
        for marker in ("unavailable", "overloaded", "high demand", "service unavailable")
    ):
        return VideoFailure(VideoFailureCategory.PROVIDER_UNAVAILABLE, True, False, True)
    if isinstance(error, TimeoutError) or "timed out" in message or "timeout" in message:
        return VideoFailure(VideoFailureCategory.TIMEOUT, True, True, True)
    if isinstance(error, (ConnectionError, OSError)) and not any(
        marker in message for marker in ("ffmpeg", "video", "audio", "codec", "media")
    ):
        return VideoFailure(VideoFailureCategory.CONNECTION, True, False, True)
    if code in {401, 403} or any(
        marker in message for marker in ("unauthorized", "forbidden", "api key", "credential")
    ):
        return VideoFailure(VideoFailureCategory.AUTHENTICATION, False, False, True)
    if any(
        marker in message
        for marker in ("ffmpeg", "codec", "corrupt", "invalid video", "invalid audio", "media")
    ):
        return VideoFailure(VideoFailureCategory.MEDIA_PROCESSING, False, True, True)
    if code in {400, 404, 413, 415, 422}:
        return VideoFailure(VideoFailureCategory.BAD_REQUEST, False, code == 413, True)
    return VideoFailure(VideoFailureCategory.UNKNOWN, True, True, True)


@dataclass
class _CircuitState:
    consecutive_failures: int = 0
    open_until: float = 0.0
    half_open_in_flight: bool = False


class VideoCircuitBreaker:
    """Small context-scoped circuit breaker keyed by provider/model identity."""

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

    def record_success(self, key: str) -> None:
        with self._lock:
            self._states.pop(key, None)

    def record_failure(
        self,
        key: str,
        failure: VideoFailure,
        *,
        now: float | None = None,
    ) -> None:
        if failure.category not in {
            VideoFailureCategory.RATE_LIMIT,
            VideoFailureCategory.PROVIDER_UNAVAILABLE,
            VideoFailureCategory.TIMEOUT,
            VideoFailureCategory.CONNECTION,
        }:
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
