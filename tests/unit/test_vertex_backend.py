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

"""Unit tests for routing Gemini endpoints to Vertex AI."""

from types import SimpleNamespace

import pytest

from artemis.agents.explorer.explorer import Explorer
from artemis.llm.google import use_vertex_backend, vertex_location, vertex_project
from artemis.llm.router import ModelEndpoint, ModelFactory, ModelProvider


class _RecordingChatModel:
    """Stands in for ChatGoogleGenerativeAI and keeps the constructor kwargs."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.fixture
def recording_chat_model(monkeypatch):
    monkeypatch.setattr("langchain_google_genai.ChatGoogleGenerativeAI", _RecordingChatModel)
    monkeypatch.setattr("artemis.llm.router._patch_langchain_google_genai", lambda: None)


@pytest.fixture
def clean_vertex_env(monkeypatch):
    for name in (
        "GOOGLE_GENAI_USE_VERTEXAI",
        "VERTEX_AI_PROJECT",
        "VERTEX_AI_LOCATION",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.mark.parametrize("value", ["true", "True", "1", "yes"])
def test_use_vertex_backend_reads_genai_flag(clean_vertex_env, value):
    clean_vertex_env.setenv("GOOGLE_GENAI_USE_VERTEXAI", value)
    assert use_vertex_backend() is True


@pytest.mark.parametrize("value", ["", "false", "0", "no"])
def test_use_vertex_backend_off(clean_vertex_env, value):
    clean_vertex_env.setenv("GOOGLE_GENAI_USE_VERTEXAI", value)
    assert use_vertex_backend() is False


def test_vertex_location_defaults_to_global(clean_vertex_env):
    assert vertex_location() == "global"
    clean_vertex_env.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    assert vertex_location() == "us-central1"
    clean_vertex_env.setenv("VERTEX_AI_LOCATION", "europe-west4")
    assert vertex_location() == "europe-west4"


def test_vertex_project_prefers_artemis_variable(clean_vertex_env):
    assert vertex_project() is None
    clean_vertex_env.setenv("GOOGLE_CLOUD_PROJECT", "from-gcloud")
    assert vertex_project() == "from-gcloud"
    clean_vertex_env.setenv("VERTEX_AI_PROJECT", "from-artemis")
    assert vertex_project() == "from-artemis"


def test_google_endpoint_uses_api_key_by_default(clean_vertex_env, recording_chat_model):
    ep = ModelEndpoint(
        provider=ModelProvider.GOOGLE, model_name="gemini-3.8-flash", api_key="test-key"
    )
    model = ModelFactory.create_model(ep)
    assert model.kwargs["api_key"] == "test-key"
    assert "vertexai" not in model.kwargs


def test_google_endpoint_moves_to_vertex_with_flag(clean_vertex_env, recording_chat_model):
    clean_vertex_env.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    clean_vertex_env.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    ep = ModelEndpoint(
        provider=ModelProvider.GOOGLE, model_name="gemini-3.5-flash-lite", api_key="test-key"
    )
    model = ModelFactory.create_model(ep)
    assert model.kwargs["vertexai"] is True
    assert model.kwargs["project"] == "test-project"
    assert model.kwargs["location"] == "global"
    assert "api_key" not in model.kwargs
    assert model.kwargs["model"] == "gemini-3.5-flash-lite"


def test_flag_keeps_explicit_project_and_location(clean_vertex_env, recording_chat_model):
    clean_vertex_env.setenv("GOOGLE_GENAI_USE_VERTEXAI", "1")
    clean_vertex_env.setenv("VERTEX_AI_PROJECT", "artemis-project")
    clean_vertex_env.setenv("VERTEX_AI_LOCATION", "us-central1")
    ep = ModelEndpoint(provider=ModelProvider.GOOGLE, model_name="gemini-3.8-flash")
    model = ModelFactory.create_model(ep)
    assert model.kwargs["project"] == "artemis-project"
    assert model.kwargs["location"] == "us-central1"


def test_explorer_skips_native_engine_on_vertex(clean_vertex_env, monkeypatch):
    explorer = SimpleNamespace(
        model_name="gemini-3.8-flash", ctx=SimpleNamespace(_genai_client=object())
    )
    assert Explorer._detect_native_engine(explorer) is True
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    assert Explorer._detect_native_engine(explorer) is False
