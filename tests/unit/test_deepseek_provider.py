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

"""Offline request-shape coverage for the DeepSeek provider."""

import json
from unittest.mock import Mock

import httpx
import langchain_openai
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, SecretStr
import pytest

from artemis.config.llm import LLM
from artemis.config.settings import Settings, settings
from artemis.llm.router import ModelEndpoint, ModelFactory, ModelProvider
from artemis.services.llm import RobustChatModelWrapper


@pytest.fixture(autouse=True)
def provider_keys(monkeypatch):
    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", SecretStr("deepseek-test-secret"))
    monkeypatch.setattr(settings, "OPENAI_API_KEY", SecretStr("unrelated-openai-secret"))
    monkeypatch.setattr(settings, "OPENAI_BASE_URL", "https://unrelated.invalid/v1")


def test_deepseek_config_and_key_validation(monkeypatch):
    config = LLM(provider="deepseek", model="deepseek-flash")
    config.validate_provider("Hopper")
    assert ModelProvider.from_string("DeepSeek") == ModelProvider.DEEPSEEK
    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", None)
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        config.validate_provider("Hopper")
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        ModelFactory.create_model(ModelEndpoint(provider=ModelProvider.DEEPSEEK))


def test_factory_uses_dedicated_credentials_and_disables_thinking():
    model = ModelFactory.create_model(
        ModelEndpoint(provider=ModelProvider.DEEPSEEK, model_name="deepseek-flash")
    )
    assert model.openai_api_base == "https://api.deepseek.com"
    assert model.openai_api_key.get_secret_value() == "deepseek-test-secret"
    assert model.extra_body == {"thinking": {"type": "disabled"}}


def test_explicit_endpoint_overrides():
    model = ModelFactory.create_model(
        ModelEndpoint(
            provider=ModelProvider.DEEPSEEK,
            model_name="deepseek-flash",
            api_key="endpoint-test-secret",
            api_base="https://explicit.invalid/v1",
            max_tokens=256,
            timeout_seconds=12,
        )
    )
    assert model.openai_api_key.get_secret_value() == "endpoint-test-secret"
    assert model.openai_api_base == "https://explicit.invalid/v1"
    assert model.max_tokens == 256
    assert model.request_timeout == 12


def test_settings_key_access_and_placeholder_filter(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    local = Settings(_env_file=None, DEEPSEEK_API_KEY="your_api_key_here")
    assert local.get_api_key("deepseek") is None
    local.set_api_key("deepseek", "runtime-test-secret")
    assert local.get_api_key("deepseek").get_secret_value() == "runtime-test-secret"
    assert "runtime-test-secret" not in str(local)


@pytest.mark.parametrize("provider", [ModelProvider.OPENAI, ModelProvider.GOOGLE])
def test_other_providers_keep_their_structured_output_defaults(provider):
    model = Mock()
    wrapper = RobustChatModelWrapper(model, endpoint=ModelEndpoint(provider=provider))
    wrapper.with_structured_output(dict)
    model.with_structured_output.assert_called_once_with(dict)


def test_explicit_structured_method_is_preserved():
    model = Mock()
    wrapper = RobustChatModelWrapper(model, endpoint=ModelEndpoint(provider=ModelProvider.DEEPSEEK))
    wrapper.with_structured_output(dict, method="json_mode")
    model.with_structured_output.assert_called_once_with(dict, method="json_mode")


@pytest.mark.asyncio
async def test_structured_output_emits_compatible_request_and_parses_result(monkeypatch):
    class AppResult(BaseModel):
        found: bool
        package: str

    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert request.url.host == "api.deepseek.com"
        assert request.headers["authorization"] == "Bearer deepseek-test-secret"
        assert payload["thinking"] == {"type": "disabled"}
        assert "response_format" not in payload
        assert payload["tool_choice"]["function"]["name"] == "AppResult"
        return httpx.Response(
            200,
            json={
                "id": "test-completion",
                "object": "chat.completion",
                "created": 0,
                "model": "deepseek-flash",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "test-call",
                                    "type": "function",
                                    "function": {
                                        "name": "AppResult",
                                        "arguments": json.dumps(
                                            {"found": True, "package": "com.android.settings"}
                                        ),
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10},
            },
        )

    original = langchain_openai.ChatOpenAI
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(
            langchain_openai,
            "ChatOpenAI",
            lambda **kwargs: original(**kwargs, http_async_client=client),
        )
        endpoint = ModelEndpoint(provider=ModelProvider.DEEPSEEK, model_name="deepseek-flash")
        raw = ModelFactory.create_model(endpoint)
        # Match get_llm's callback binding before the gateway chooses its schema method.
        model = RobustChatModelWrapper(raw.with_config(callbacks=[]), endpoint=endpoint)
        result = await model.with_structured_output(AppResult).ainvoke(
            [HumanMessage(content="Find Settings in com.android.settings.")]
        )
    assert result == AppResult(found=True, package="com.android.settings")
    assert len(requests) == 1
