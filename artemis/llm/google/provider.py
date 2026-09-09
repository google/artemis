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

"""Google provider identity and the Gemini model capability table.

Provider identity has two tiers:

* :func:`is_google_provider` - the Gemini Developer API (``google`` /
  ``gemini``). Native ``google-genai`` features (Files API, explicit caching,
  the Interactions API) are only reachable here.
* :func:`is_google_family_provider` - additionally Vertex AI, which shares
  Google credentials but not the developer-API-only features.

Model IDs use ``gemini-<major>.<minor>-<tier>[<suffix>]``. Release suffixes
(``preview``, ``latest``, ``exp``, and numeric tokens) retain base-family
capabilities. Variants such as ``image`` and ``tts`` are excluded from agentic
video support, but their version still determines thinking-level support.
"""

from __future__ import annotations

import re
from typing import Any, Literal, NamedTuple

VideoProcessing = Literal["auto", "agentic", "static"]
ResolvedVideoProcessing = Literal["agentic", "static"]

_GOOGLE_API_PROVIDERS = frozenset({"google", "gemini"})
_GOOGLE_FAMILY_PROVIDERS = _GOOGLE_API_PROVIDERS | frozenset({"vertexai", "vertex"})

_GEMINI_MODEL_RE = re.compile(r"gemini-(\d+)\.(\d+)-(flash-lite|flash|pro)", re.IGNORECASE)
_RELEASE_SUFFIX_TOKENS = frozenset({"preview", "latest", "exp"})


class _GeminiModel(NamedTuple):
    version: tuple[int, int]
    tier: str
    #: False for variant ids (``-image``, ``-tts``, ...) that carry their own
    #: capabilities and must not inherit the family's answers.
    is_base: bool


# Agentic video understanding (Interactions API, ``processing: "agentic"``)
# launched on Gemini 3.6/3.7/3.8 Flash and 3.5 Flash-Lite. Later releases in
# the same families are assumed to keep it.
_AGENTIC_VIDEO_MIN_FLASH = (3, 6)
_AGENTIC_VIDEO_MIN_FLASH_LITE = (3, 5)


def _normalize_provider(value: Any) -> str:
    if value is None:
        return ""
    value = getattr(value, "value", value)
    return str(value).lower().replace("_", "").replace("-", "").strip()


def is_google_provider(value: Any) -> bool:
    """True for the Gemini Developer API (``google`` / ``gemini``).

    Accepts a provider name, a provider enum, or anything exposing ``.value``.
    """
    return _normalize_provider(value) in _GOOGLE_API_PROVIDERS


def is_google_family_provider(value: Any) -> bool:
    """True for every provider that authenticates with Google credentials."""
    return _normalize_provider(value) in _GOOGLE_FAMILY_PROVIDERS


def is_google_chat_model(model: Any) -> bool:
    """True when a LangChain chat model is backed by Google (by class name).

    Only used when no endpoint metadata is available; the class-name sniff is
    the historical fallback and is kept in one place here.
    """
    cls = getattr(model, "__class__", None)
    name = cls.__name__ if cls else ""
    return "Google" in name or "Gemini" in name or "VertexAI" in name


def strip_provider_prefix(model_name: Any) -> str:
    """Drops a ``provider/`` prefix (``google/gemini-3.8-flash`` -> ``gemini-3.8-flash``)."""
    name = str(model_name or "")
    return name.rsplit("/", 1)[-1] if "/" in name else name


def is_gemini_model(model_name: Any) -> bool:
    """True when the model name refers to any Gemini model."""
    return "gemini" in strip_provider_prefix(model_name).lower()


def _is_release_suffix(rest: str) -> bool:
    """Whether what follows the tier is a release suffix (see the module docstring)."""
    if not rest:
        return True
    if not rest.startswith("-"):
        return False
    return all(token in _RELEASE_SUFFIX_TOKENS or token.isdigit() for token in rest[1:].split("-"))


def _parse_gemini_model(model_name: Any) -> _GeminiModel | None:
    name = strip_provider_prefix(model_name).lower()
    match = _GEMINI_MODEL_RE.search(name)
    if not match:
        return None
    return _GeminiModel(
        version=(int(match.group(1)), int(match.group(2))),
        tier=match.group(3),
        is_base=_is_release_suffix(name[match.end() :]),
    )


def gemini_version(model_name: Any) -> tuple[int, int] | None:
    """``(major, minor)`` parsed from a ``gemini-X.Y-*`` name, else ``None``."""
    parsed = _parse_gemini_model(model_name)
    return parsed.version if parsed else None


def _gemini_tier(model_name: Any) -> str | None:
    """The tier of a *base* model id; ``None`` for variants and non-Gemini names."""
    parsed = _parse_gemini_model(model_name)
    return parsed.tier if parsed and parsed.is_base else None


def supports_thinking_level(model_name: Any) -> bool:
    """Whether the model accepts ``thinking_level`` (Gemini 3 and later).

    Gemini 1.x/2.x only understand ``thinking_budget``. Names that do not
    carry a parsable version (e.g. the robotics ER models) are assumed to be
    current and therefore accept it.
    """
    version = gemini_version(model_name)
    return version is None or version[0] >= 3


def supports_agentic_video(model_name: Any) -> bool:
    """Whether the Gemini API accepts ``processing: "agentic"`` for this model.

    Only base Flash / Flash-Lite ids qualify; variants (``-image``, ``-tts``,
    ``-live``, ...) do not inherit the family's support.
    """
    parsed = _parse_gemini_model(model_name)
    if parsed is None or not parsed.is_base:
        return False
    if parsed.tier == "flash":
        return parsed.version >= _AGENTIC_VIDEO_MIN_FLASH
    if parsed.tier == "flash-lite":
        return parsed.version >= _AGENTIC_VIDEO_MIN_FLASH_LITE
    return False


def is_agentic_video_auto_eligible(model_name: Any) -> bool:
    """Models that get agentic video processing under ``processing: "auto"``.

    Flash-Lite is deliberately excluded: it accepts the mode but mis-reads
    timestamps in practice, so it must be opted into explicitly.
    """
    return supports_agentic_video(model_name) and _gemini_tier(model_name) == "flash"


def resolve_video_processing(configured: Any, model_name: Any) -> ResolvedVideoProcessing:
    """Turns the configured video-processing knob into a concrete mode.

    ``static`` is always honoured. ``agentic`` is honoured only for models
    that support it and silently degrades otherwise. ``auto`` (and any
    unrecognised value) enables agentic processing for the auto-eligible
    models and keeps static processing for everything else.
    """
    mode = str(configured or "auto").strip().lower() if isinstance(configured, str) else "auto"
    if mode == "static":
        return "static"
    if mode == "agentic":
        return "agentic" if supports_agentic_video(model_name) else "static"
    return "agentic" if is_agentic_video_auto_eligible(model_name) else "static"
