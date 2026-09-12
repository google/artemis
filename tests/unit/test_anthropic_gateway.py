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

"""Gateway regressions against real SDK requests, with all HTTP sends intercepted."""

import os
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from pydantic import SecretStr
import pytest

from artemis.config import llm as llm_config
from artemis.config.settings import Settings
from artemis.core.diagnostics.probes import credentials_probe
from artemis.llm import router
from artemis.llm.router import ModelEndpoint, ModelFactory
from mcp_server.tools import diagnose

TOKEN = "test-gateway-token-123456"
API_KEY = "test-official-key-123456"
BASE_URL = "https://gateway.example.test/anthropic"


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    # Do not read the developer's .env, enable tracing, or reuse cached clients.
    with patch.dict(os.environ, {}, clear=True):
        settings = Settings(_env_file=None)
        for module in (router, llm_config, credentials_probe):
            monkeypatch.setattr(module, "settings", settings)
        monkeypatch.setattr(ModelFactory, "_cache", {})
        yield settings


@pytest.fixture
def http_stub(monkeypatch):
    state = SimpleNamespace(requests=[], status=200, error="")

    def respond(request):
        state.requests.append(request)
        if state.status != 200:
            body = {"error": {"message": state.error}}
        elif request.method == "GET":
            body = {"data": []}
        else:
            body = {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "gateway-model",
                "content": [{"type": "text", "text": "gateway reply"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 2},
            }
        return httpx.Response(state.status, request=request, json=body)

    def send(client, request, **kwargs):
        return respond(request)

    async def asend(client, request, **kwargs):
        return respond(request)

    monkeypatch.setattr(httpx.Client, "send", send)
    monkeypatch.setattr(httpx.AsyncClient, "send", asend)
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("async_call", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize(
    "auth_case", ["settings-token", "env-token", "both-credentials", "explicit-key", "api-key"]
)
async def test_requests_use_exactly_the_selected_credential(
    isolated_settings, monkeypatch, http_stub, async_call, auth_case
):
    if auth_case in ("both-credentials", "explicit-key", "api-key"):
        isolated_settings.ANTHROPIC_API_KEY = SecretStr(API_KEY)
        monkeypatch.setenv("ANTHROPIC_API_KEY", API_KEY)
    if auth_case != "api-key":
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", TOKEN)
        if auth_case != "env-token":
            isolated_settings.ANTHROPIC_AUTH_TOKEN = SecretStr(TOKEN)
            monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "lower-priority-env-token")

    explicit_key = "test-scoped-key" if auth_case == "explicit-key" else None
    model = ModelFactory.create_model(
        ModelEndpoint(
            provider="anthropic",
            model_name="gateway-model",
            api_base=BASE_URL,
            api_key=explicit_key,
        )
    )
    result = await model.ainvoke("ping") if async_call else model.invoke("ping")

    assert result.content == "gateway reply"
    assert len(http_stub.requests) == 1
    request = http_stub.requests[0]
    assert str(request.url) == f"{BASE_URL}/v1/messages"
    if auth_case in ("explicit-key", "api-key"):
        assert request.headers["x-api-key"] == (explicit_key or API_KEY)
        assert "authorization" not in request.headers
    else:
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert "x-api-key" not in request.headers  # Empty headers are also forbidden.
    assert TOKEN not in request.content.decode()
    assert TOKEN not in repr(model)
    assert TOKEN not in str(model.model_dump())
    assert TOKEN not in str(model.to_json())


@pytest.mark.parametrize(
    ("endpoint_base", "settings_base", "env_base", "expected"),
    [
        (BASE_URL, "https://settings.test", "https://env.test", BASE_URL),
        (None, BASE_URL, "https://env.test", BASE_URL),
        (None, None, BASE_URL, BASE_URL),
        (None, None, None, "https://api.anthropic.com"),
    ],
)
def test_anthropic_base_url_precedence(
    isolated_settings, monkeypatch, http_stub, endpoint_base, settings_base, env_base, expected
):
    isolated_settings.ANTHROPIC_AUTH_TOKEN = SecretStr(TOKEN)
    isolated_settings.ANTHROPIC_BASE_URL = settings_base
    if env_base:
        monkeypatch.setenv("ANTHROPIC_BASE_URL", env_base)
    model = ModelFactory.create_model(
        ModelEndpoint(provider="anthropic", model_name="gateway-model", api_base=endpoint_base)
    )
    model.invoke("ping")
    assert str(http_stub.requests[0].url) == f"{expected}/v1/messages"


@pytest.mark.parametrize("token_source", ["settings", "env"])
def test_token_alone_satisfies_startup_validation(isolated_settings, monkeypatch, token_source):
    if token_source == "settings":
        isolated_settings.ANTHROPIC_AUTH_TOKEN = SecretStr(TOKEN)
    else:
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", TOKEN)
    llm_config.LLM(provider="anthropic", model="gateway-model").validate_provider("planner")


@pytest.mark.parametrize("placeholder", ["", "your_gateway_token_here", "<token>", "null"])
def test_placeholder_tokens_are_not_revived_from_env(isolated_settings, monkeypatch, placeholder):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", placeholder)
    settings = Settings(_env_file=None)
    monkeypatch.setattr(llm_config, "settings", settings)
    assert settings.ANTHROPIC_AUTH_TOKEN is None or not placeholder
    assert settings.get_anthropic_auth_token() is None
    with pytest.raises(Exception, match="requires ANTHROPIC_API_KEY"):
        llm_config.LLM(provider="anthropic", model="gateway-model").validate_provider("planner")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auth_case", ["settings-token", "env-token", "both-credentials", "api-key"]
)
async def test_diagnostic_probe_preserves_destination_and_auth(
    isolated_settings, monkeypatch, http_stub, auth_case
):
    if auth_case in ("both-credentials", "api-key"):
        isolated_settings.ANTHROPIC_API_KEY = SecretStr(API_KEY)
    if auth_case == "env-token":
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", TOKEN)
        monkeypatch.setenv("ANTHROPIC_BASE_URL", BASE_URL)
    else:
        isolated_settings.ANTHROPIC_BASE_URL = f"{BASE_URL}/"
        if auth_case != "api-key":
            isolated_settings.ANTHROPIC_AUTH_TOKEN = SecretStr(TOKEN)

    probe = await credentials_probe.LLMCredentialsProbe().probe()
    result = await diagnose._verify_credentials(probe)

    assert len(result) == 1
    assert result[0]["valid"] is True
    assert len(http_stub.requests) == 1
    request = http_stub.requests[0]
    assert str(request.url) == f"{BASE_URL}/v1/models"
    if auth_case == "api-key":
        assert request.headers["x-api-key"] == API_KEY
        assert "authorization" not in request.headers
    else:
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert "x-api-key" not in request.headers
    assert TOKEN not in str(result)
    assert TOKEN not in str(diagnose._scrub(probe.metadata))


@pytest.mark.asyncio
async def test_diagnostic_failure_redacts_gateway_token(isolated_settings, http_stub):
    isolated_settings.ANTHROPIC_AUTH_TOKEN = SecretStr(TOKEN)
    isolated_settings.ANTHROPIC_BASE_URL = BASE_URL
    http_stub.status = 401
    http_stub.error = f"Rejected credential {TOKEN}"
    result = await diagnose._verify_credentials(
        await credentials_probe.LLMCredentialsProbe().probe()
    )
    assert result[0]["valid"] is False
    assert "401" in result[0]["message"]
    assert TOKEN not in str(result)
    assert "***" in result[0]["message"]
