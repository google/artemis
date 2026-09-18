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

"""Unit tests for the custom / Ollama / vLLM provider plumbing.

Covers the gaps that motivated the
``feat(custom-llm-providers): complete validation and credential
management for custom/ollama/vllm providers`` change:

* ``LLM.validate_provider`` raises a clear error when no base URL is set,
  but tolerates a missing API key for fully local Ollama / vLLM servers.
* ``Settings.get_api_key`` and ``Settings.set_api_key`` route the
  ``custom`` / ``ollama`` / ``vllm`` providers through ``CUSTOM_LLM_API_KEY``
  while still falling back to ``OPENAI_API_KEY`` for backwards compatibility.
"""

from pydantic import SecretStr

from artemis.config.llm import LLM
from artemis.config.settings import Settings


def test_validate_custom_provider_requires_base_url(monkeypatch):
    """A custom endpoint without OPENAI_BASE_URL or CUSTOM_LLM_BASE_URL fails loudly."""
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("CUSTOM_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CUSTOM_LLM_API_KEY", raising=False)

    llm = LLM(provider="custom", model="MiniMax-M3")
    with __import__("pytest").raises(Exception, match="OPENAI_BASE_URL"):
        llm.validate_provider("Planner")


def test_validate_custom_provider_requires_api_key(monkeypatch):
    """A custom endpoint must declare an API key (hosted providers always do)."""
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.minimax.io/v1")
    monkeypatch.delenv("CUSTOM_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CUSTOM_LLM_API_KEY", raising=False)

    llm = LLM(provider="custom", model="MiniMax-M3")
    with __import__("pytest").raises(Exception, match="OPENAI_API_KEY"):
        llm.validate_provider("Planner")


def test_validate_custom_provider_ok_with_openai_key(monkeypatch):
    """Hosted custom endpoints work when OPENAI_API_KEY is set (backwards compat)."""
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.minimax.io/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-dummy")
    monkeypatch.delenv("CUSTOM_LLM_API_KEY", raising=False)

    llm = LLM(provider="custom", model="MiniMax-M3")
    llm.validate_provider("Planner")  # no raise


def test_validate_ollama_provider_tolerates_missing_api_key(monkeypatch):
    """Local Ollama servers ignore the bearer token; missing key is fine."""
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.delenv("CUSTOM_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CUSTOM_LLM_API_KEY", raising=False)

    for provider in ("ollama", "vllm"):
        llm = LLM(provider=provider, model="llava:13b")
        llm.validate_provider(f"Node-{provider}")  # no raise


def test_validate_custom_provider_uses_custom_env_vars(monkeypatch):
    """CUSTOM_LLM_BASE_URL / CUSTOM_LLM_API_KEY take precedence."""
    monkeypatch.setenv("CUSTOM_LLM_BASE_URL", "https://api.minimax.io/v1")
    monkeypatch.setenv("CUSTOM_LLM_API_KEY", "test-key-dummy")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    llm = LLM(provider="custom", model="MiniMax-M3")
    llm.validate_provider("Planner")  # no raise


def test_settings_get_api_key_prefers_custom_field(monkeypatch):
    """get_api_key('custom') returns CUSTOM_LLM_API_KEY when set, else OPENAI_API_KEY."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CUSTOM_LLM_API_KEY", raising=False)

    settings = Settings(
        OPENAI_API_KEY=SecretStr("dummy-openai-key"),
        CUSTOM_LLM_API_KEY=SecretStr("dummy-custom-key"),
    )
    assert settings.get_api_key("custom").get_secret_value() == "dummy-custom-key"
    assert settings.get_api_key("ollama").get_secret_value() == "dummy-custom-key"
    assert settings.get_api_key("vllm").get_secret_value() == "dummy-custom-key"


def test_settings_get_api_key_falls_back_to_openai(monkeypatch):
    """Without CUSTOM_LLM_API_KEY, get_api_key('custom') falls back to OPENAI_API_KEY."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CUSTOM_LLM_API_KEY", raising=False)

    settings = Settings(OPENAI_API_KEY=SecretStr("dummy-openai-key"))
    assert settings.get_api_key("custom").get_secret_value() == "dummy-openai-key"
    assert settings.get_api_key("ollama").get_secret_value() == "dummy-openai-key"


def test_settings_set_api_key_writes_custom_field(monkeypatch):
    """set_api_key('custom') populates CUSTOM_LLM_API_KEY and mirrors to OPENAI_API_KEY."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CUSTOM_LLM_API_KEY", raising=False)

    settings = Settings()
    settings.set_api_key("custom", "dummy-rotated-key", persist_to_env=False)

    assert settings.CUSTOM_LLM_API_KEY.get_secret_value() == "dummy-rotated-key"
    assert settings.OPENAI_API_KEY.get_secret_value() == "dummy-rotated-key"
    assert settings.get_api_key("custom").get_secret_value() == "dummy-rotated-key"
