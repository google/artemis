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

"""Endpoint plumbing for local OpenAI-compatible providers (e.g. a local OpenAI-compatible server)."""

from types import SimpleNamespace

from artemis.config.llm import LLMConfig
from artemis.llm.router import ModelEndpoint, ModelFactory, ModelProvider
from artemis.services.llm import _resolve_endpoint
from third_party.mobile_use.utils.file import strip_json_comments


def _ctx(**node_overrides) -> SimpleNamespace:
    utils = {
        "outputter": {
            "provider": "google",
            "model": "g",
            "fallback": {"provider": "google", "model": "g"},
        },
        "hopper": {
            "provider": "google",
            "model": "g",
            "fallback": {"provider": "google", "model": "g"},
        },
        "video_analyzer": None,
        "object_detector": None,
    }
    utils.update(node_overrides.pop("utils", {}))
    config = LLMConfig.model_validate(
        {
            "planner": {
                "provider": "google",
                "model": "g",
                "fallback": {"provider": "google", "model": "g"},
            },
            "summarizer": {
                "provider": "google",
                "model": "g",
                "fallback": {"provider": "google", "model": "g"},
            },
            "operator": {
                "provider": "google",
                "model": "g",
                "fallback": {"provider": "google", "model": "g"},
            },
            "operator_summarizer": {
                "provider": "google",
                "model": "g",
                "fallback": {"provider": "google", "model": "g"},
            },
            "log_reader_sub_agent": {
                "provider": "google",
                "model": "g",
                "fallback": {"provider": "google", "model": "g"},
            },
            "log_analyzer": {
                "provider": "google",
                "model": "g",
                "fallback": {"provider": "google", "model": "g"},
            },
            "diagnoser": {
                "provider": "google",
                "model": "g",
                "fallback": {"provider": "google", "model": "g"},
            },
            "checker": {
                "provider": "google",
                "model": "g",
                "fallback": {"provider": "google", "model": "g"},
            },
            "planner_avatar": {
                "provider": "google",
                "model": "g",
                "fallback": {"provider": "google", "model": "g"},
            },
            "history_analyzer_expert": {
                "provider": "google",
                "model": "g",
                "fallback": {"provider": "google", "model": "g"},
            },
            "diagnoser_expert": {
                "provider": "google",
                "model": "g",
                "fallback": {"provider": "google", "model": "g"},
            },
            "explorer": {
                "provider": "google",
                "model": "g",
                "fallback": {"provider": "google", "model": "g"},
            },
            "utils": utils,
            **node_overrides,
        }
    )
    return SimpleNamespace(llm_config=config)


_LOCAL_NODE = {
    "provider": "custom",
    "model": "qwen-vl",
    "api_base": "http://127.0.0.1:8080/v1",
    "coordinate_format": "xy_px",
    "max_tokens": 1024,
    "fallback": {
        "provider": "custom",
        "model": "qwen-vl",
        "api_base": "http://127.0.0.1:8080/v1",
        "coordinate_format": "xy_px",
    },
}


def test_resolve_endpoint_forwards_api_base_and_coordinate_format():
    ctx = _ctx(utils={"object_detector": _LOCAL_NODE})
    ep = _resolve_endpoint(ctx, "object_detector", is_utils=True)
    assert ep.provider == ModelProvider.CUSTOM
    assert ep.model_name == "qwen-vl"
    assert ep.api_base == "http://127.0.0.1:8080/v1"
    assert ep.coordinate_format == "xy_px"
    assert ep.max_tokens == 1024


def test_resolve_endpoint_fallback_carries_local_fields():
    ctx = _ctx(utils={"object_detector": _LOCAL_NODE})
    ep = _resolve_endpoint(ctx, "object_detector", is_utils=True, use_fallback=True)
    assert ep.api_base == "http://127.0.0.1:8080/v1"
    assert ep.coordinate_format == "xy_px"


def test_resolve_endpoint_defaults_keep_google_shape():
    ctx = _ctx()
    ep = _resolve_endpoint(ctx, "operator")
    assert ep.provider == ModelProvider.GOOGLE
    assert ep.api_base is None
    assert ep.coordinate_format is None


def test_custom_endpoint_builds_chat_openai_with_local_base(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    ep = ModelEndpoint(
        provider=ModelProvider.CUSTOM,
        model_name="qwen-vl",
        api_base="http://127.0.0.1:8080/v1",
    )
    model = ModelFactory.create_model(ep)
    assert type(model).__name__ == "ChatOpenAI"
    assert str(model.openai_api_base) == "http://127.0.0.1:8080/v1"


def test_empty_openai_env_key_falls_back_to_placeholder(monkeypatch):
    """An exported-but-empty OPENAI_API_KEY must not reach ChatOpenAI."""
    monkeypatch.setenv("OPENAI_API_KEY", "")
    ep = ModelEndpoint(
        provider=ModelProvider.CUSTOM,
        model_name="qwen3.8-9b",
        api_base="http://127.0.0.1:8080/v1",
    )
    model = ModelFactory.create_model(ep)
    assert type(model).__name__ == "ChatOpenAI"
    assert model.openai_api_key.get_secret_value() == "EMPTY"


def test_strip_json_comments_preserves_urls_and_strings():
    text = (
        "{ // header\n"
        '  "api_base": "http://127.0.0.1:8080/v1", // trailing\n'
        '  "note": "x /* not a comment */ y",\n'
        '  "escaped": "a \\"//\\" b"\n'
        "}"
    )
    import json

    parsed = json.loads(strip_json_comments(text))
    assert parsed["api_base"] == "http://127.0.0.1:8080/v1"
    assert parsed["note"] == "x /* not a comment */ y"
    assert parsed["escaped"] == 'a "//" b'
