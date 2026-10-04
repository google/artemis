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

"""Unit tests for provider-aware lens-model routing (get_lens_llm).

Covers the routing matrix behind the VisualStepSummarizer 404 fix: the
Gemini-name shortcut, the explicit provider knob, summarizer-node provider
inheritance, and the diagnosability warning for non-Gemini models routed to
the Gemini API.
"""

from unittest.mock import Mock, patch

import pytest
from langchain_openai import ChatOpenAI

from artemis.config.llm import LLM, LLMConfig, LLMWithFallback
from artemis.services.llm import (
    _inherit_lens_provider,
    _warn_if_non_google_routes_to_google,
    get_lens_llm,
)
from artemis.llm.router import ModelProvider


def _llm_cfg(provider: str, model: str = "m") -> LLMConfig:
    """Minimal LLMConfig with only the summarizer node under test."""
    cfg = Mock(spec=LLMConfig)
    cfg.summarizer = LLMWithFallback(
        provider=provider, model=model, fallback=LLM(provider=provider, model="f")
    )
    return cfg


@pytest.fixture
def capture_create(monkeypatch):
    """Capture ModelFactory.create_model calls instead of building clients."""
    calls = []

    def fake_create(endpoint):
        calls.append(endpoint)
        return Mock()

    monkeypatch.setattr("artemis.services.llm.ModelFactory.create_model", fake_create)
    return calls


class TestGeminiNameShortcut:
    def test_bare_gemini_name_routes_to_google(self, capture_create):
        get_lens_llm(None, "gemini-2.5-flash-lite")
        (ep,) = capture_create
        assert ep.provider == ModelProvider.GOOGLE
        assert ep.model_name == "gemini-2.5-flash-lite"

    def test_google_prefixed_name_routes_to_google(self, capture_create):
        get_lens_llm(None, "google/gemini-3.8-flash")
        (ep,) = capture_create
        assert ep.provider == ModelProvider.GOOGLE
        assert ep.model_name == "google/gemini-3.8-flash"

    def test_gemini_prefixed_name_routes_to_google(self, capture_create):
        get_lens_llm(None, "gemini/gemini-2.5-flash-lite")
        (ep,) = capture_create
        assert ep.provider == ModelProvider.GOOGLE


class TestExplicitProviderKnob:
    def test_explicit_custom_provider_for_namespaced_model(self, capture_create):
        get_lens_llm(None, "tensorx/deepseek/deepseek-v4-flash", "custom")
        (ep,) = capture_create
        assert ep.provider == ModelProvider.CUSTOM
        # The full model ID passes through verbatim — never prefix-parsed.
        assert ep.model_name == "tensorx/deepseek/deepseek-v4-flash"

    def test_explicit_provider_beats_gemini_shortcut(self, capture_create):
        # An explicit knob wins even for a Gemini name (provider-hosted
        # gemini-compatible deployments).
        get_lens_llm(None, "gemini-2.5-flash-lite", "openai")
        (ep,) = capture_create
        assert ep.provider == ModelProvider.OPENAI

    def test_unknown_provider_raises(self, capture_create):
        with pytest.raises(ValueError, match="Unknown LLM provider"):
            get_lens_llm(None, "some-model", "notaprovider")

    def test_temperature_and_timeout_forwarded(self, capture_create):
        get_lens_llm(None, "m", "custom", temperature=0.4, timeout=12.0)
        (ep,) = capture_create
        assert ep.temperature == 0.4
        assert ep.timeout_seconds == 12.0


class TestInheritedProvider:
    def test_inherits_summarizer_node_provider(self, capture_create):
        ctx = Mock()
        ctx.llm_config = _llm_cfg("custom")
        get_lens_llm(ctx, "tensorx/deepseek/deepseek-v4-flash")
        (ep,) = capture_create
        assert ep.provider == ModelProvider.CUSTOM

    def test_inherits_google_default(self, capture_create):
        ctx = Mock()
        ctx.llm_config = _llm_cfg("google")
        get_lens_llm(ctx, "vendor/whatever")
        (ep,) = capture_create
        assert ep.provider == ModelProvider.GOOGLE

    def test_no_ctx_llm_config_falls_back_to_default_config(self, capture_create, monkeypatch):
        ctx = Mock()
        ctx.llm_config = None
        monkeypatch.setattr(
            "artemis.config.llm.get_default_llm_config",
            lambda: _llm_cfg("openai"),
        )
        get_lens_llm(ctx, "some/model")
        (ep,) = capture_create
        assert ep.provider == ModelProvider.OPENAI

    def test_resolution_failure_defaults_to_custom(self, capture_create, monkeypatch):
        ctx = Mock()
        ctx.llm_config = None
        monkeypatch.setattr(
            "artemis.services.llm.get_default_llm_config",
            Mock(side_effect=RuntimeError("boom")),
            raising=False,
        )
        get_lens_llm(ctx, "some/model")
        (ep,) = capture_create
        assert ep.provider == ModelProvider.CUSTOM


class TestNonGoogleWarning:
    def test_warns_on_non_gemini_name_to_google(self, caplog):
        with caplog.at_level("WARNING", logger="artemis.services.llm"):
            _warn_if_non_google_routes_to_google(ModelProvider.GOOGLE, "tensorx/deepseek/v4")
        assert any("routed to the Google" in r.message for r in caplog.records)

    def test_no_warning_for_gemini_name(self, caplog):
        with caplog.at_level("WARNING", logger="artemis.services.llm"):
            _warn_if_non_google_routes_to_google(ModelProvider.GOOGLE, "gemini-2.5-flash-lite")
        assert not caplog.records

    def test_no_warning_for_non_google_provider(self, caplog):
        with caplog.at_level("WARNING", logger="artemis.services.llm"):
            _warn_if_non_google_routes_to_google(ModelProvider.CUSTOM, "tensorx/deepseek/v4")
        assert not caplog.records


class TestIntegration:
    def test_tensorx_model_builds_chatopenai_with_gateway_base_url(self, monkeypatch):
        """End-to-end: the reported 404 model now routes to the OpenAI gateway."""
        from artemis.services.llm import ModelFactory

        monkeypatch.setattr("artemis.llm.router.ModelFactory._cache", {}, raising=False)
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}):
            llm = get_lens_llm(None, "tensorx/deepseek/deepseek-v4-flash")
        assert isinstance(llm, ChatOpenAI)
        assert llm.model_name == "tensorx/deepseek/deepseek-v4-flash"
        assert llm.openai_api_base == "https://api.opper.ai/v3/compat/"
