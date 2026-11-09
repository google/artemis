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

"""Token-usage normalization across every response shape ARTEMIS sees.

Google alone reports usage under three different vocabularies:

* LangChain ``usage_metadata`` - ``input_tokens`` / ``output_tokens`` /
  ``input_token_details.cache_read`` (OpenAI-style names appear via
  ``response_metadata.token_usage``).
* ``generate_content`` ``usage_metadata`` - ``prompt_token_count`` /
  ``candidates_token_count`` / ``cached_content_token_count`` /
  ``thoughts_token_count`` / ``tool_use_prompt_token_count``.
* Interactions API ``usage`` - ``total_input_tokens`` /
  ``total_output_tokens`` / ``total_cached_tokens`` / ``total_thought_tokens``
  / ``total_tool_use_tokens``.

:func:`normalize_usage` folds all of them into one dict so meters, traces and
the UI read a single set of keys.
"""

from __future__ import annotations

from typing import Any

_PROMPT_KEYS = ("prompt_tokens", "input_tokens", "prompt_token_count", "total_input_tokens")
_COMPLETION_KEYS = (
    "completion_tokens",
    "output_tokens",
    "candidates_token_count",
    "total_output_tokens",
)
_TOTAL_KEYS = ("total_tokens", "total_token_count")
_CACHED_KEYS = (
    "cached_tokens",
    "cached_content_token_count",
    "cachedContentTokenCount",
    "total_cached_tokens",
)
_THOUGHT_KEYS = (
    "thought_tokens",
    "thoughts_token_count",
    "reasoning_tokens",
    "total_thought_tokens",
)
_TOOL_USE_KEYS = ("tool_use_tokens", "tool_use_prompt_token_count", "total_tool_use_tokens")

NORMALIZED_USAGE_KEYS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cached_tokens",
    "thought_tokens",
    "tool_use_tokens",
)


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _read(raw: Any, key: str) -> Any:
    if isinstance(raw, dict):
        return raw.get(key)
    return getattr(raw, key, None)


def _first(raw: Any, keys: tuple[str, ...]) -> int:
    for key in keys:
        value = _int(_read(raw, key))
        if value:
            return value
    return 0


def normalize_usage(raw: Any) -> dict[str, int] | None:
    """Normalizes a usage payload (dict or object) into the shared key set.

    Returns ``None`` when the payload carries no usable token counts, so
    callers can distinguish "no usage reported" from an all-zero report.
    """
    if raw is None:
        return None

    prompt = _first(raw, _PROMPT_KEYS)
    completion = _first(raw, _COMPLETION_KEYS)
    total = _first(raw, _TOTAL_KEYS) or (prompt + completion)

    cached = 0
    details = _read(raw, "input_token_details")
    if details is not None:
        cached = _int(_read(details, "cache_read"))
    if not cached:
        cached = _first(raw, _CACHED_KEYS)

    thought = 0
    output_details = _read(raw, "output_token_details")
    if output_details is not None:
        thought = _int(_read(output_details, "reasoning"))
    if not thought:
        thought = _first(raw, _THOUGHT_KEYS)

    tool_use = _first(raw, _TOOL_USE_KEYS)

    if total <= 0:
        return None
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cached_tokens": cached,
        "thought_tokens": thought,
        "tool_use_tokens": tool_use,
    }


def usage_from_message(message: Any) -> dict[str, int] | None:
    """Extracts normalized usage from a LangChain message, best-effort.

    Prefers LangChain's ``usage_metadata``; falls back to the raw provider
    payload stored under ``response_metadata`` (``usage_metadata`` for Google,
    ``token_usage`` for OpenAI-compatible providers).
    """
    raw = getattr(message, "usage_metadata", None)
    if isinstance(raw, dict) and raw:
        return normalize_usage(raw)
    meta = getattr(message, "response_metadata", None)
    if isinstance(meta, dict):
        candidate = meta.get("usage_metadata") or meta.get("token_usage")
        if isinstance(candidate, dict) and candidate:
            return normalize_usage(candidate)
    return None
