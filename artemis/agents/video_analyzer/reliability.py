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

"""Failure classification and circuit breaking for video model calls.

Thin media-aware extension of :mod:`artemis.llm.reliability`, which owns the
generic taxonomy and circuit breaker.  This module adds the media-processing
category and the ``should_split`` recovery decision (bisecting long clips).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from artemis.llm.reliability import (
    CircuitBreaker,
    FailureCategory,
    classify_failure,
    extract_status_code,
)


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


_MEDIA_MARKERS = ("ffmpeg", "codec", "corrupt", "invalid video", "invalid audio", "media")


def classify_video_failure(error: BaseException) -> VideoFailure:
    """Convert provider/transport/media exceptions into recovery decisions."""
    generic = classify_failure(error)
    message = str(error).lower()
    has_media_marker = any(marker in message for marker in _MEDIA_MARKERS)

    # Media markers take precedence over generic connection / bad-request /
    # unknown classification (rate-limit, unavailable, timeout, and auth
    # signals still win, matching the original ordering of checks).
    if has_media_marker and generic.category in {
        FailureCategory.CONNECTION,
        FailureCategory.BAD_REQUEST,
        FailureCategory.UNKNOWN,
    }:
        return VideoFailure(VideoFailureCategory.MEDIA_PROCESSING, False, True, True)

    category = VideoFailureCategory(generic.category.value)
    should_split = category in {VideoFailureCategory.TIMEOUT, VideoFailureCategory.UNKNOWN} or (
        category is VideoFailureCategory.BAD_REQUEST and extract_status_code(error) == 413
    )
    # The video pipeline keeps its historical behavior of falling back to the
    # universal analyzer even for bad requests (a different provider may accept
    # the same media payload).
    should_fallback = generic.should_fallback or category is VideoFailureCategory.BAD_REQUEST
    if category is VideoFailureCategory.CANCELLED:
        should_fallback = False
    return VideoFailure(category, generic.retryable, should_split, should_fallback)


class VideoCircuitBreaker(CircuitBreaker):
    """Context-scoped circuit breaker keyed by provider/model identity."""
