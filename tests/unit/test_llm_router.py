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

"""Tests for the multi-provider model router and endpoint resolution.

Covers ModelProvider alias parsing, ModelFactory construction of local
OpenAI-compatible endpoints (Ollama/vLLM/custom), and the config-to-endpoint
passthrough in ``_resolve_endpoint`` (api_base / api_key / max_tokens).
"""

from types import SimpleNamespace
from unittest.mock import patch
import json

import pytest

from artemis.config.llm import LLM, LLMWithFallback
from artemis.llm.router import ModelEndpoint, ModelFactory, ModelProvider
from artemis.services import llm as llm_service
from artemis.services.llm import _resolve_endpoint


class TestModelProviderFromString:
    def test_local_and_custom_providers_parse(self):
        assert ModelProvider.from_string("ollama") is ModelProvider.OLLAMA
        assert ModelProvider.from_string("vllm") is ModelProvider.VLLM
        assert ModelProvider.from_string("custom") is ModelProvider.CUSTOM

    def test_cloud_aliases_parse(self):
        assert ModelProvider.from_string("google") is ModelProvider.GOOGLE
        assert ModelProvider.from_string("gemini") is ModelProvider.GOOGLE
        assert ModelProvider.from_string("vertexai") is ModelProvider.VERTEX_AI
        assert ModelProvider.from_string("vertex") is ModelProvider.VERTEX_AI
        assert ModelProvider.from_string("openai") is ModelProvider.OPENAI
        assert ModelProvider.from_string("anthropic") is ModelProvider.ANTHROPIC
        assert ModelProvider.from_string("claude") is ModelProvider.ANTHROPIC
        assert ModelProvider.from_string("openrouter") is ModelProvider.OPENROUTER
        assert ModelProvider.from_string("xai") is ModelProvider.XAI
        assert ModelProvider.from_string("grok") is ModelProvider.XAI

    def test_normalization_is_case_and_separator_insensitive(self):
        assert ModelProvider.from_string(" Ollama ") is ModelProvider.OLLAMA
        assert ModelProvider.from_string("V-LLM") is ModelProvider.VLLM
        assert ModelProvider.from_string("Vertex_AI") is ModelProvider.VERTEX_AI
        assert ModelProvider.from_string("OPENAI") is ModelProvider.OPENAI

    def test_none_and_empty_mean_default_google(self):
        assert ModelProvider.from_string(None) is ModelProvider.GOOGLE
        assert ModelProvider.from_string("") is ModelProvider.GOOGLE
        assert ModelProvider.from_string("   ") is ModelProvider.GOOGLE

    def test_enum_passes_through(self):
        assert ModelProvider.from_string(ModelProvider.OLLAMA) is ModelProvider.OLLAMA

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown LLM provider"):
            ModelProvider.from_string("sentient-toaster")


class TestModelFactoryLocalEndpoints:
    """create_model() for OLLAMA/VLLM/CUSTOM builds a langchain_openai ChatOpenAI."""

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        monkeypatch.delenv("ARTEMIS_FAKE_LLM", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    def _build(self, endpoint: ModelEndpoint):
        with patch("langchain_openai.ChatOpenAI") as mock_cls:
            mock_cls.return_value = object()
            result = ModelFactory.create_model(endpoint)
        assert mock_cls.call_count == 1
        return result, mock_cls.call_args.kwargs

    def test_ollama_endpoint_uses_configured_api_base(self):
        endpoint = ModelEndpoint(
            provider=ModelProvider.OLLAMA,
            model_name="qwen2.5vl:7b",
            api_base="http://localhost:11434/v1",
            max_tokens=2048,
            timeout_seconds=120.0,
        )
        _, kwargs = self._build(endpoint)
        assert kwargs["model"] == "qwen2.5vl:7b"
        assert kwargs["base_url"] == "http://localhost:11434/v1"
        assert kwargs["max_tokens"] == 2048
        assert kwargs["timeout"] == 120.0
        # Local servers accept any non-empty key; "EMPTY" is the default.
        assert kwargs["api_key"] == "EMPTY"

    def test_custom_endpoint_falls_back_to_localhost_default(self):
        endpoint = ModelEndpoint(provider=ModelProvider.CUSTOM, model_name="edge-vlm")
        _, kwargs = self._build(endpoint)
        assert kwargs["base_url"] == "http://localhost:8000/v1"

    def test_endpoint_api_key_takes_precedence(self):
        endpoint = ModelEndpoint(
            provider=ModelProvider.VLLM,
            model_name="qwen2.5vl-7b-awq",
            api_base="http://edge-box:8000/v1",
            api_key="sk-local-secret",
        )
        _, kwargs = self._build(endpoint)
        assert kwargs["api_key"] == "sk-local-secret"
        assert kwargs["base_url"] == "http://edge-box:8000/v1"

    def test_none_values_are_not_forwarded(self):
        endpoint = ModelEndpoint(provider=ModelProvider.OLLAMA, model_name="m")
        _, kwargs = self._build(endpoint)
        assert "max_tokens" not in kwargs
        assert "reasoning_effort" not in kwargs

    def test_reasoning_effort_forwarded_when_set(self):
        endpoint = ModelEndpoint(
            provider=ModelProvider.CUSTOM, model_name="m", reasoning_effort="low"
        )
        _, kwargs = self._build(endpoint)
        assert kwargs["reasoning_effort"] == "low"


class TestResolveEndpointPassthrough:
    """_resolve_endpoint forwards endpoint fields from LLM config to ModelEndpoint."""

    @staticmethod
    def _ctx(cfg: LLMWithFallback) -> SimpleNamespace:
        return SimpleNamespace(llm_config=SimpleNamespace(get_agent=lambda name: cfg))

    def test_endpoint_fields_pass_through(self):
        cfg = LLMWithFallback(
            provider="ollama",
            model="qwen2.5vl:7b",
            api_base="http://localhost:11434/v1",
            api_key="sk-local",
            max_tokens=4096,
            timeout_seconds=180.0,
            is_multimodal=True,
            fallback=LLM(provider="ollama", model="qwen2.5vl:3b"),
        )
        endpoint = _resolve_endpoint(self._ctx(cfg), "planner")
        assert endpoint.provider is ModelProvider.OLLAMA
        assert endpoint.model_name == "qwen2.5vl:7b"
        assert endpoint.api_base == "http://localhost:11434/v1"
        assert endpoint.api_key == "sk-local"
        assert endpoint.max_tokens == 4096
        assert endpoint.timeout_seconds == 180.0
        assert endpoint.is_multimodal is True

    def test_fallback_endpoint_gets_same_treatment(self):
        cfg = LLMWithFallback(
            provider="google",
            model="gemini-3.8-flash",
            fallback=LLM(
                provider="custom",
                model="edge-vlm",
                api_base="http://192.168.1.10:8000/v1",
                api_key="sk-edge",
                max_tokens=1024,
                timeout_seconds=30.0,
                is_multimodal=False,
            ),
        )
        endpoint = _resolve_endpoint(self._ctx(cfg), "planner", use_fallback=True)
        assert endpoint.provider is ModelProvider.CUSTOM
        assert endpoint.model_name == "edge-vlm"
        assert endpoint.api_base == "http://192.168.1.10:8000/v1"
        assert endpoint.api_key == "sk-edge"
        assert endpoint.max_tokens == 1024
        assert endpoint.timeout_seconds == 30.0
        assert endpoint.is_multimodal is False

    def test_unset_fields_keep_endpoint_defaults(self):
        cfg = LLMWithFallback(
            provider="google",
            model="gemini-3.8-flash",
            fallback=LLM(provider="google", model="gemini-3.7-flash"),
        )
        endpoint = _resolve_endpoint(self._ctx(cfg), "planner")
        assert endpoint.api_base is None
        assert endpoint.api_key is None
        assert endpoint.max_tokens is None
        assert endpoint.timeout_seconds == 60.0
        assert endpoint.is_multimodal is True

    def test_legacy_timeout_field_still_honored(self):
        cfg = LLMWithFallback(
            provider="ollama",
            model="qwen2.5vl:7b",
            timeout=42.0,
            fallback=LLM(provider="ollama", model="qwen2.5vl:3b"),
        )
        endpoint = _resolve_endpoint(self._ctx(cfg), "planner")
        assert endpoint.timeout_seconds == 42.0

    def test_timeout_seconds_takes_precedence_over_legacy_timeout(self):
        cfg = LLMWithFallback(
            provider="ollama",
            model="qwen2.5vl:7b",
            timeout=42.0,
            timeout_seconds=90.0,
            fallback=LLM(provider="ollama", model="qwen2.5vl:3b"),
        )
        endpoint = _resolve_endpoint(self._ctx(cfg), "planner")
        assert endpoint.timeout_seconds == 90.0


class TestValidateProviderLocalEndpoints:
    """Local providers must not demand cloud API keys."""

    def test_local_providers_skip_cloud_key_requirement(self):
        for provider in ("ollama", "vllm", "custom"):
            LLM(provider=provider, model="any-model").validate_provider("TestNode")

    def test_openai_still_requires_key(self, monkeypatch):
        monkeypatch.setattr(llm_service.settings, "OPENAI_API_KEY", None, raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(Exception, match="OPENAI_API_KEY"):
            LLM(provider="openai", model="gpt-4o").validate_provider("TestNode")


class TestStripJsonComments:
    """strip_json_comments must not eat URLs inside string literals."""

    def test_urls_inside_strings_survive(self):
        from artemis.utils.file import strip_json_comments

        text = '{"api_base": "http://localhost:11434/v1"} // trailing comment'
        assert json.loads(strip_json_comments(text)) == {"api_base": "http://localhost:11434/v1"}

    def test_line_and_block_comments_are_removed(self):
        from artemis.utils.file import strip_json_comments

        text = (
            '{\n// line comment\n"a": 1, /* block // comment */ "b": "x // y",\n'
            '"c": "escaped \\" // not a comment"\n}'
        )
        assert json.loads(strip_json_comments(text)) == {
            "a": 1,
            "b": "x // y",
            "c": 'escaped " // not a comment',
        }


class TestLocalOllamaPreset:
    """The bundled local-ollama preset must parse and target a local endpoint."""

    @staticmethod
    def _preset(path):
        from artemis.utils.file import load_jsonc

        with open(path, encoding="utf-8") as f:
            return load_jsonc(f)["presets"]["local-ollama"]

    def test_repo_config_preset(self):
        from artemis.config.paths import ROOT_DIR

        preset = self._preset(ROOT_DIR / "config" / "artemis.jsonc")
        assert preset["provider"] == "ollama"
        assert preset["api_base"] == "http://localhost:11434/v1"
        assert preset["fallback"]["provider"] == "ollama"

    def test_bundled_resource_preset(self):
        from artemis.config.paths import ROOT_DIR

        preset = self._preset(ROOT_DIR / "artemis" / "resources" / "config" / "artemis.jsonc")
        assert preset["provider"] == "ollama"
        assert preset["api_base"] == "http://localhost:11434/v1"
