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
and unified provider configuration across Gemini, Vertex AI, OpenAI,
Anthropic, OpenRouter, and local Ollama/vLLM endpoints.
"""

from enum import StrEnum
import os
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel, Field

from artemis.config.settings import settings
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


class ModelProvider(StrEnum):
    GOOGLE = "google"
    GEMINI = "google"
    VERTEX_AI = "vertexai"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    OPENROUTER = "openrouter"
    XAI = "xai"
    OLLAMA = "ollama"
    VLLM = "vllm"
    CUSTOM = "custom"

    @classmethod
    def from_string(cls, val: Any) -> "ModelProvider":
        """Converts any string, enum, or mock representation into a canonical ModelProvider."""
        if isinstance(val, cls):
            return val
        s = str(val).lower().replace("_", "").replace("-", "").strip()
        mapping = {
            "google": cls.GOOGLE,
            "gemini": cls.GOOGLE,
            "vertexai": cls.VERTEX_AI,
            "vertex": cls.VERTEX_AI,
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
        return mapping.get(s, cls.GOOGLE)


class ModelEndpoint(BaseModel):
    """Configuration definition for an LLM/VLM model endpoint."""

    provider: ModelProvider = Field(default=ModelProvider.GOOGLE, description="Model provider")
    model_name: str = Field(default="gemini-2.5-flash", description="Model name identifier")
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
    thinking_level: str | None = Field(
        default=None, description="Thinking level: 'minimal', 'low', 'medium', 'high'"
    )
    include_thoughts: bool | None = Field(
        default=None, description="Whether to include thought traces in response"
    )
    enable_grounding: bool = Field(
        default=False,
        description="Whether to enable Google Search grounding for Gemini endpoints",
    )

    def cache_key(self) -> tuple:
        """Returns a deterministic hashable cache key for connection pooling."""
        return (
            self.provider.value,
            self.model_name,
            self.temperature,
            self.timeout_seconds,
            self.thinking_budget,
            self.thinking_level,
            self.include_thoughts,
            self.reasoning_effort,
            self.enable_grounding,
            self.api_base,
        )


def _patch_langchain_google_genai():
    """Patches ChatGoogleGenerativeAI._process_tool_config to preserve and ensure include_server_side_tool_invocations."""
    try:
        from langchain_google_genai.chat_models import ChatGoogleGenerativeAI
        from google.genai.types import ToolConfig

        if getattr(ChatGoogleGenerativeAI, "_is_artemis_patched", False):
            return

        original_process = ChatGoogleGenerativeAI._process_tool_config

        def patched_process_tool_config(self, tool_choice, tool_config, formatted_tools):
            config = original_process(self, tool_choice, tool_config, formatted_tools)

            has_builtin = False
            has_functions = False
            if formatted_tools:
                for tool in formatted_tools:
                    tool_dict = tool if isinstance(tool, dict) else getattr(tool, "__dict__", {})
                    if any(
                        tool_dict.get(k) is not None
                        for k in (
                            "google_search",
                            "code_execution",
                            "google_maps",
                            "google_search_retrieval",
                        )
                    ):
                        has_builtin = True
                    if tool_dict.get("function_declarations"):
                        has_functions = True

            if tool_config:
                normalized = (
                    ToolConfig.model_validate(tool_config)
                    if isinstance(tool_config, dict)
                    else tool_config
                )
                if getattr(normalized, "include_server_side_tool_invocations", None) is not None:
                    if config is None:
                        config = ToolConfig(
                            include_server_side_tool_invocations=normalized.include_server_side_tool_invocations
                        )
                    else:
                        config.include_server_side_tool_invocations = (
                            normalized.include_server_side_tool_invocations
                        )

            if has_builtin and has_functions:
                if config is None:
                    config = ToolConfig(include_server_side_tool_invocations=True)
                else:
                    config.include_server_side_tool_invocations = True

            return config

        ChatGoogleGenerativeAI._process_tool_config = patched_process_tool_config
        ChatGoogleGenerativeAI._is_artemis_patched = True
    except Exception as e:
        logger.warning(f"Could not patch ChatGoogleGenerativeAI._process_tool_config: {e}")


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

        if provider == ModelProvider.GOOGLE:
            _patch_langchain_google_genai()
            from langchain_google_genai import (
                ChatGoogleGenerativeAI,
                HarmBlockThreshold,
                HarmCategory,
            )

            api_key = (
                endpoint.api_key
                or (settings.GOOGLE_API_KEY.get_secret_value() if settings.GOOGLE_API_KEY else None)
                or os.environ.get("GOOGLE_API_KEY")
                or os.environ.get("GEMINI_API_KEY")
            )
            thinking_level = endpoint.thinking_level
            if endpoint.model_name and any(v in endpoint.model_name for v in ("2.5", "2.0", "1.5")):
                thinking_level = None

            kwargs: dict[str, Any] = {
                "model": endpoint.model_name,
                "temperature": endpoint.temperature,
                "max_output_tokens": endpoint.max_tokens,
                "api_key": api_key,
                "timeout": endpoint.timeout_seconds,
                "thinking_budget": endpoint.thinking_budget,
                "thinking_level": thinking_level,
                "include_thoughts": endpoint.include_thoughts,
                "safety_settings": {
                    HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
                    HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
                    HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
                    HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
                },
            }
            return ChatGoogleGenerativeAI(**{k: v for k, v in kwargs.items() if v is not None})

        elif provider == ModelProvider.VERTEX_AI:
            from langchain_google_vertexai import (
                ChatVertexAI,
                HarmBlockThreshold,
                HarmCategory,
            )

            kwargs = {
                "model_name": endpoint.model_name,
                "temperature": endpoint.temperature,
                "max_output_tokens": endpoint.max_tokens,
                "timeout": endpoint.timeout_seconds,
                "thinking_budget": endpoint.thinking_budget,
                "safety_settings": {
                    HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
                    HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
                    HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
                    HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
                },
            }
            return ChatVertexAI(**{k: v for k, v in kwargs.items() if v is not None})

        elif provider == ModelProvider.OPENAI:
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
