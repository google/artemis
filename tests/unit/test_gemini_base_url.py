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

"""Custom Gemini endpoint routing (gateway / proxy deployments)."""

from artemis.config import settings
from artemis.llm.router import ModelEndpoint, ModelFactory, ModelProvider
import pytest


@pytest.fixture(autouse=True)
def _clear_model_cache():
    """create_model is cached per endpoint; keep each case independent."""
    ModelFactory._cache.clear()
    yield
    ModelFactory._cache.clear()


def _endpoint(**kwargs) -> ModelEndpoint:
    return ModelEndpoint(
        provider=ModelProvider.GOOGLE,
        model_name="gemini-2.5-flash",
        api_key="test-key",
        **kwargs,
    )


def test_endpoint_api_base_reaches_the_gemini_client():
    """A per-endpoint api_base must be forwarded, as it already is for OpenAI."""
    model = ModelFactory.create_model(_endpoint(api_base="https://gateway.example/gemini"))
    assert model.base_url == "https://gateway.example/gemini"


def test_settings_base_url_is_the_fallback(monkeypatch):
    """GEMINI_BASE_URL applies when the endpoint carries no api_base."""
    monkeypatch.setattr(
        settings, "GEMINI_BASE_URL", "https://gateway.example/gemini", raising=False
    )
    model = ModelFactory.create_model(_endpoint())
    assert model.base_url == "https://gateway.example/gemini"


def test_endpoint_api_base_wins_over_settings(monkeypatch):
    """Explicit per-endpoint configuration outranks the global default."""
    monkeypatch.setattr(settings, "GEMINI_BASE_URL", "https://global.example/gemini", raising=False)
    model = ModelFactory.create_model(_endpoint(api_base="https://per-endpoint.example/gemini"))
    assert model.base_url == "https://per-endpoint.example/gemini"


def test_public_api_stays_the_default(monkeypatch):
    """With nothing configured the client must keep its own default endpoint."""
    monkeypatch.setattr(settings, "GEMINI_BASE_URL", None, raising=False)
    model = ModelFactory.create_model(_endpoint())
    assert not model.base_url
