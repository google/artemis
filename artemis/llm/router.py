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

"""Dynamic Multi-Provider Model Router for ARTEMIS.

Provides role-based LLM/VLM dispatching, dynamic fallback chains,
and unified provider configuration across OpenAI, Anthropic, OpenRouter,
Ollama and any OpenAI-compatible endpoint.
"""

from enum import StrEnum
import hashlib
import os
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel, Field

from artemis.config.settings import settings
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)


class ModelProvider(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    OPENROUTER = "openrouter"
    XAI = "xai"
    OLLAMA = "ollama"
    VLLM = "vllm"
    CUSTOM = "custom"

    @classmethod
    def from_string(cls, val: Any) -> "ModelProvider":
        """Converts a string or enum into a canonical ModelProvider.

        ``None``/empty means "use the default" (OpenAI); an unrecognized
        non-empty value raises instead of silently routing to the wrong
        provider with the wrong credentials.
        """
        if isinstance(val, cls):
            return val
        if val is None:
            return cls.OPENAI
        s = str(val).lower().replace("_", "").replace("-", "").strip()
        if not s:
            return cls.OPENAI
        mapping = {
            "openai": cls.OPENAI,
            "anthropic": cls.ANTHROPIC,
            "claude": cls.ANTHROPIC,
            "openrouter": cls.OPENROUTER,
            "xai": cls.XAI,
            "grok": cls.XAI,
            "ollama": cls.OLLAMA,
            "vllm": cls.VLLM,
            "custom": cls.CUSTOM,
        }
        provider = mapping.get(s)
        if provider is None:
            raise ValueError(
                f"Unknown LLM provider {val!r}. Valid providers: {sorted(set(mapping))}"
            )
        return provider


class ModelEndpoint(BaseModel):
    """Configuration definition for an LLM/VLM model endpoint."""

    provider: ModelProvider = Field(default=ModelProvider.OPENAI, description="Model provider")
    model_name: str = Field(default="gpt-4o-mini", description="Model name identifier")
    api_key: str | None = Field(default=None, description="API Key or secret")
    api_base: str | None = Field(default=None, description="Custom API endpoint base URL")
    temperature: float = Field(default=0.0, description="Sampling temperature")
    max_tokens: int | None = Field(default=None, description="Maximum completion tokens")
    timeout_seconds: float = Field(default=60.0, description="Request timeout in seconds")
    is_multimodal: bool = Field(
        default=True, description="Whether endpoint accepts image/video inputs"
    )
    reasoning_effort: str | None = Field(
        default=None, description="Reasoning effort: 'low', 'medium', 'high'"
    )
    thinking_budget: int | None = Field(default=None, description="Thinking budget token count")

    def cache_key(self) -> tuple:
        """Key the client cache by endpoint settings, hashing the API key."""
        api_key_digest = (
            hashlib.sha256(self.api_key.encode("utf-8")).hexdigest()[:16] if self.api_key else None
        )
        return (
            self.provider.value,
            self.model_name,
            self.temperature,
            self.max_tokens,
            self.timeout_seconds,
            self.thinking_budget,
            self.reasoning_effort,
            self.api_base,
            api_key_digest,
        )


class ModelFactory:
    """Unified factory for instantiating and caching LangChain chat models across providers."""

    _cache: dict[tuple, BaseChatModel] = {}

    @classmethod
    def get_model(cls, endpoint: ModelEndpoint) -> BaseChatModel:
        key = endpoint.cache_key()
        if key not in cls._cache:
            cls._cache[key] = cls.create_model(endpoint)
        return cls._cache[key]

    @classmethod
    def create_model(cls, endpoint: ModelEndpoint) -> BaseChatModel:
        if os.environ.get("ARTEMIS_FAKE_LLM") == "1":
            from artemis.llm.fake_model import FakeChatModel

            delay = float(os.environ.get("ARTEMIS_FAKE_LLM_DELAY_S", "0") or 0)
            logger.warning(
                f"ARTEMIS_FAKE_LLM=1 — returning FakeChatModel (delay={delay}s) "
                f"instead of {endpoint.provider}/{endpoint.model_name}"
            )
            return FakeChatModel(delay_s=delay)

        provider = ModelProvider.from_string(endpoint.provider)

        if provider == ModelProvider.OPENAI:
            from langchain_openai import ChatOpenAI

            api_key = (
                endpoint.api_key
                or (settings.OPENAI_API_KEY.get_secret_value() if settings.OPENAI_API_KEY else None)
                or os.environ.get("OPENAI_API_KEY", "EMPTY")
            )
            base_url = endpoint.api_base or (
                str(settings.OPENAI_BASE_URL) if settings.OPENAI_BASE_URL else None
            )
            kwargs = {
                "model": endpoint.model_name,
                "temperature": endpoint.temperature,
                "max_tokens": endpoint.max_tokens,
                "api_key": api_key,
                "base_url": base_url,
                "timeout": endpoint.timeout_seconds,
            }
            if endpoint.reasoning_effort:
                kwargs["reasoning_effort"] = endpoint.reasoning_effort
            return ChatOpenAI(**{k: v for k, v in kwargs.items() if v is not None})

        elif provider == ModelProvider.ANTHROPIC:
            from langchain_anthropic import ChatAnthropic

            api_key = (
                endpoint.api_key
                or (
                    settings.ANTHROPIC_API_KEY.get_secret_value()
                    if settings.ANTHROPIC_API_KEY
                    else None
                )
                or os.environ.get("ANTHROPIC_API_KEY")
            )
            kwargs = {
                "model": endpoint.model_name,
                "temperature": endpoint.temperature,
                "api_key": api_key,
                "timeout": endpoint.timeout_seconds,
            }
            budget = endpoint.thinking_budget
            if not budget and endpoint.reasoning_effort:
                effort_map = {"low": 2048, "medium": 8192, "high": 32768}
                budget = effort_map.get(endpoint.reasoning_effort.lower())
            if budget:
                kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
                kwargs["temperature"] = 1.0
            return ChatAnthropic(**{k: v for k, v in kwargs.items() if v is not None})

        elif provider == ModelProvider.OPENROUTER:
            from langchain_openai import ChatOpenAI

            api_key = (
                endpoint.api_key
                or (
                    settings.OPEN_ROUTER_API_KEY.get_secret_value()
                    if settings.OPEN_ROUTER_API_KEY
                    else None
                )
                or os.environ.get("OPEN_ROUTER_API_KEY")
            )
            return ChatOpenAI(
                model=endpoint.model_name,
                temperature=endpoint.temperature,
                api_key=api_key,
                base_url=endpoint.api_base or "https://openrouter.ai/api/v1",
                timeout=endpoint.timeout_seconds,
            )

        elif provider == ModelProvider.XAI:
            from langchain_openai import ChatOpenAI

            api_key = (
                endpoint.api_key
                or (settings.XAI_API_KEY.get_secret_value() if settings.XAI_API_KEY else None)
                or os.environ.get("XAI_API_KEY")
            )
            return ChatOpenAI(
                model=endpoint.model_name,
                temperature=endpoint.temperature,
                api_key=api_key,
                base_url=endpoint.api_base or "https://api.x.ai/v1",
                timeout=endpoint.timeout_seconds,
            )

        elif provider in (ModelProvider.OLLAMA, ModelProvider.VLLM, ModelProvider.CUSTOM):
            from langchain_openai import ChatOpenAI

            api_key = endpoint.api_key or os.environ.get("OPENAI_API_KEY", "EMPTY")
            base_url = endpoint.api_base or os.environ.get(
                "OPENAI_BASE_URL", "http://localhost:8000/v1"
            )
            kwargs = {
                "model": endpoint.model_name,
                "temperature": endpoint.temperature,
                "max_tokens": endpoint.max_tokens,
                "api_key": api_key,
                "base_url": base_url,
                "timeout": endpoint.timeout_seconds,
            }
            if endpoint.reasoning_effort:
                kwargs["reasoning_effort"] = endpoint.reasoning_effort
            return ChatOpenAI(**{k: v for k, v in kwargs.items() if v is not None})

        else:
            raise ValueError(f"Unsupported model provider: {provider}")
