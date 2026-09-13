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

"""Provider identity and Gemini capability table."""

from artemis.llm.google import (
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
from artemis.llm.router import ModelProvider
import pytest


@pytest.mark.parametrize(
    "value, google, family",
    [
        ("google", True, True),
        ("Gemini", True, True),
        (ModelProvider.GOOGLE, True, True),
        (ModelProvider.GEMINI, True, True),
        ("vertexai", False, True),
        ("vertex-ai", False, True),
        (ModelProvider.VERTEX_AI, False, True),
        ("openai", False, False),
        ("anthropic", False, False),
        (None, False, False),
        ("", False, False),
    ],
)
def test_provider_identity_tiers(value, google, family):
    assert is_google_provider(value) is google
    assert is_google_family_provider(value) is family


def test_google_chat_model_is_detected_by_class_name():
    class ChatGoogleGenerativeAI:  # noqa: N801 - mirrors the real class name
        pass

    class ChatVertexAI:
        pass

    class ChatOpenAI:
        pass

    assert is_google_chat_model(ChatGoogleGenerativeAI()) is True
    assert is_google_chat_model(ChatVertexAI()) is True
    assert is_google_chat_model(ChatOpenAI()) is False
    assert is_google_chat_model(None) is False


def test_model_name_helpers():
    assert strip_provider_prefix("google/gemini-3.8-flash") == "gemini-3.8-flash"
    assert strip_provider_prefix("gemini-3.8-flash") == "gemini-3.8-flash"
    assert strip_provider_prefix(None) == ""
    assert is_gemini_model("openrouter/google/gemini-3.7-flash") is True
    assert is_gemini_model("claude-sonnet-5") is False
    assert gemini_version("gemini-3.8-flash") == (3, 8)
    assert gemini_version("gemini-3.5-flash-lite-preview") == (3, 5)
    # Variants still report their family version (thinking-level gate).
    assert gemini_version("gemini-3.8-flash-image") == (3, 8)
    assert gemini_version("gemini-2.5-flash-tts") == (2, 5)
    assert gemini_version("gemini-robotics-er-2-preview") is None


@pytest.mark.parametrize(
    "model, expected",
    [
        ("gemini-3.8-flash", True),
        ("gemini-3.5-flash-lite", True),
        ("gemini-2.5-flash", False),
        ("gemini-2.0-flash", False),
        ("gemini-1.5-pro", False),
        ("gemini-robotics-er-2-preview", True),
        ("gpt-5", True),
    ],
)
def test_supports_thinking_level(model, expected):
    assert supports_thinking_level(model) is expected


@pytest.mark.parametrize(
    "model, supported, auto",
    [
        ("gemini-3.8-flash", True, True),
        ("gemini-3.7-flash", True, True),
        ("gemini-3.6-flash", True, True),
        ("google/gemini-3.9-flash", True, True),
        ("gemini-3.5-flash-lite", True, False),
        ("gemini-3.5-flash", False, False),
        ("gemini-3.8-pro", False, False),
        ("gemini-2.5-flash", False, False),
        ("gemini-robotics-er-2-preview", False, False),
        ("claude-sonnet-5", False, False),
        # Release suffixes stay in the base family.
        ("models/gemini-3.7-flash-preview-09-2026", True, True),
        ("gemini-3.8-flash-latest", True, True),
        ("gemini-3.8-flash-001", True, True),
        ("gemini-3.8-flash-exp", True, True),
        ("gemini-3.5-flash-lite-preview", True, False),
        # Variant models carry their own capabilities and never inherit.
        ("gemini-3.8-flash-image", False, False),
        ("gemini-3.8-flash-tts", False, False),
        ("gemini-3.8-flash-live", False, False),
        ("gemini-3.8-flash-native-audio", False, False),
        ("gemini-3.8-flash-image-preview", False, False),
        ("gemini-3.8-flash-8b", False, False),
    ],
)
def test_agentic_video_capability(model, supported, auto):
    assert supports_agentic_video(model) is supported
    assert is_agentic_video_auto_eligible(model) is auto


def test_resolve_video_processing_honours_knob_and_capability():
    assert resolve_video_processing("auto", "gemini-3.8-flash") == "agentic"
    assert resolve_video_processing("auto", "gemini-3.5-flash-lite") == "static"
    assert resolve_video_processing("auto", "gemini-2.5-flash") == "static"
    assert resolve_video_processing("agentic", "gemini-3.5-flash-lite") == "agentic"
    assert resolve_video_processing("agentic", "gemini-2.5-flash") == "static"
    assert resolve_video_processing("static", "gemini-3.8-flash") == "static"
    # Explicit "agentic" is honoured on Flash-Lite but never on a variant.
    assert resolve_video_processing("agentic", "gemini-3.8-flash-image") == "static"
    assert resolve_video_processing("auto", "gemini-3.8-flash-tts") == "static"
    # Unknown or non-string knobs behave like "auto".
    assert resolve_video_processing(None, "gemini-3.8-flash") == "agentic"
    assert resolve_video_processing(object(), "gemini-3.8-flash") == "agentic"
    assert resolve_video_processing("bogus", "gemini-3.5-flash-lite") == "static"
