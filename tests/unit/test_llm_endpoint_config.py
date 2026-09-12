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

"""Per-endpoint configuration regressions, including the shipped JSONC examples."""

from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import SecretStr
import pytest

from artemis.config import llm as llm_config
from artemis.config.settings import Settings
from artemis.llm import router
from artemis.services.llm import _resolve_endpoint

EXAMPLES = Path(__file__).resolve().parents[2] / "config" / "examples"
PRIMARY = "https://primary.example.test"
BACKUP = "https://backup.example.test"


@pytest.fixture
def base_config():
    return {
        "default": {
            "provider": "anthropic",
            "model": "primary-model",
            "api_base": PRIMARY,
            "fallback": {
                "provider": "anthropic",
                "model": "backup-model",
                "api_base": BACKUP,
            },
        }
    }


@pytest.mark.parametrize("load_path", ["unified", "deep-merge", "profile-file"])
@pytest.mark.parametrize("node,is_utils", [("planner", False), ("outputter", True)])
@pytest.mark.parametrize(
    "override,expected_primary,expected_fallback",
    [
        ({"model": "another-model"}, PRIMARY, BACKUP),
        ({"provider": "openai"}, None, BACKUP),
        ({"fallback": {"provider": "openai"}}, PRIMARY, None),
        ({"provider": "openai", "fallback": {"provider": "openai"}}, None, None),
        (
            {
                "provider": "openai",
                "api_base": "https://explicit.example.test/v1",
                "fallback": {"provider": "openai", "api_base": "https://fallback.example.test/v1"},
            },
            "https://explicit.example.test/v1",
            "https://fallback.example.test/v1",
        ),
        ({"api_base": None, "fallback": {"api_base": None}}, None, None),
    ],
    ids=[
        "model-only",
        "primary-provider",
        "fallback-provider",
        "both-providers",
        "explicit-urls",
        "explicit-null",
    ],
)
def test_url_inheritance_across_loading_paths(
    base_config,
    monkeypatch,
    tmp_path,
    load_path,
    node,
    is_utils,
    override,
    expected_primary,
    expected_fallback,
):
    original = deepcopy(base_config)
    base = llm_config.LLMConfig.model_validate(llm_config._expand_default_into_nodes(base_config))
    original_model = base.model_dump()
    expanded_override = {"utils": {node: override}} if is_utils else {node: override}
    if load_path == "unified":
        config = llm_config.LLMConfig.model_validate(
            llm_config._expand_default_into_nodes({**base_config, "nodes": {node: override}})
        )
    elif load_path == "deep-merge":
        config = llm_config.deep_merge_llm_config(base, expanded_override)
    else:
        from artemis.sdk.types.task import AgentProfile

        monkeypatch.setattr(llm_config, "get_default_llm_config", lambda: base)
        path = tmp_path / "profile.json"
        path.write_text(json.dumps(expanded_override), encoding="utf-8")
        config = AgentProfile(name="gateway", from_file=str(path)).llm_config

    context = SimpleNamespace(llm_config=config)
    primary = _resolve_endpoint(context, node, is_utils=is_utils)
    fallback = _resolve_endpoint(context, node, is_utils=is_utils, use_fallback=True)
    assert primary.api_base == expected_primary
    assert fallback.api_base == expected_fallback
    assert primary.provider.value == override.get("provider", "anthropic")
    assert fallback.provider.value == override.get("fallback", {}).get("provider", "anthropic")
    assert config.operator.api_base == PRIMARY  # Sibling nodes must not change.
    assert config.operator.fallback.api_base == BACKUP
    assert base_config == original
    assert base.model_dump() == original_model


def test_resolved_urls_reach_openai_clients_and_separate_cache(base_config, monkeypatch):
    with patch.dict(os.environ, {}, clear=True):
        monkeypatch.setattr(
            router, "settings", Settings(_env_file=None, OPENAI_API_KEY=SecretStr("test-key"))
        )
        monkeypatch.setattr(router.ModelFactory, "_cache", {})
        base_config["default"]["provider"] = "openai"
        base_config["default"]["fallback"]["provider"] = "openai"
        base_config["nodes"] = {"operator": {"api_base": "https://operator.example.test/v1"}}
        config = llm_config.LLMConfig.model_validate(
            llm_config._expand_default_into_nodes(base_config)
        )
        context = SimpleNamespace(llm_config=config)
        planner = _resolve_endpoint(context, "planner")
        operator = _resolve_endpoint(context, "operator")
        fallback = _resolve_endpoint(context, "planner", use_fallback=True)
        models = [router.ModelFactory.get_model(ep) for ep in (planner, operator, fallback)]

        assert models[0] is router.ModelFactory.get_model(planner.model_copy())
        assert models[0] is not models[1]  # Same model name, different server.
        assert [str(m.root_client.base_url).rstrip("/") for m in models] == [
            PRIMARY,
            "https://operator.example.test/v1",
            BACKUP,
        ]


@pytest.mark.parametrize(
    "example,node,is_utils,use_fallback,provider,api_base",
    [
        ("anthropic-gateway.jsonc", "planner", False, False, "anthropic", None),
        ("anthropic-gateway.jsonc", "planner", False, True, "anthropic", None),
        (
            "multi-endpoint.jsonc",
            "planner",
            False,
            False,
            "openai",
            "https://primary.example.com/v1",
        ),
        (
            "multi-endpoint.jsonc",
            "operator",
            False,
            False,
            "openai",
            "https://vision.example.com/v1",
        ),
        (
            "multi-endpoint.jsonc",
            "operator",
            False,
            True,
            "openai",
            "https://backup.example.com/v1",
        ),
        ("multi-endpoint.jsonc", "outputter", True, False, "anthropic", None),
        ("multi-endpoint.jsonc", "outputter", True, True, "anthropic", None),
    ],
)
def test_documented_examples_parse_and_resolve(
    monkeypatch, example, node, is_utils, use_fallback, provider, api_base
):
    monkeypatch.setattr(llm_config, "get_config_path", lambda *args: EXAMPLES / example)
    config = llm_config.parse_llm_config()
    endpoint = _resolve_endpoint(
        SimpleNamespace(llm_config=config), node, is_utils=is_utils, use_fallback=use_fallback
    )
    assert endpoint.provider.value == provider
    assert endpoint.api_base == api_base
