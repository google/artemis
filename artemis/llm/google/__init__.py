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

"""Google / Gemini specific knowledge shared by every call site.

Two concerns live here so they are defined exactly once:

* :mod:`artemis.llm.google.provider` - "is this endpoint Google?" and the
  Gemini model capability table (thinking levels, agentic video).
* :mod:`artemis.llm.google.usage` - token-usage normalization across the
  LangChain, ``generate_content`` and Interactions API response shapes.

Both modules are dependency-free (no settings, no SDK client) so they can be
imported from configuration code without creating import cycles.
"""

from artemis.llm.google.provider import (
    VideoProcessing,
    gemini_version,
    is_agentic_video_auto_eligible,
    is_gemini_model,
    is_google_chat_model,
    is_google_family_provider,
    is_google_provider,
    resolve_video_processing,
    strip_provider_prefix,
    supports_agentic_video,
    supports_thinking_level,
)
from artemis.llm.google.usage import normalize_usage, usage_from_message

__all__ = [
    "VideoProcessing",
    "gemini_version",
    "is_agentic_video_auto_eligible",
    "is_gemini_model",
    "is_google_chat_model",
    "is_google_family_provider",
    "is_google_provider",
    "normalize_usage",
    "resolve_video_processing",
    "strip_provider_prefix",
    "supports_agentic_video",
    "supports_thinking_level",
    "usage_from_message",
]
