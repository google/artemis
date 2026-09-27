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

"""Per-task LLM override: request plumbing and endpoint resolution precedence.

Precedence is task override > ``artemis.jsonc`` node config > built-in default,
and the override lives on the per-task context, so nothing global is mutated.
"""

from types import SimpleNamespace

import pytest

import artemis.sdk.agent as agent_module
from artemis.config.llm import LLM, LLMConfig, LLMConfigUtils, LLMWithFallback
from artemis.llm.router import ModelProvider
from artemis.sdk.agent import Agent
from artemis.sdk.builders.task_request_builder import TaskRequestBuilder
from artemis.sdk.types.agent import AgentConfig, ServerConfig
from artemis.sdk.types.task import AgentProfile, TaskRequestCommon
from artemis.services.llm import _resolve_endpoint


def _node(provider: str = "google", model: str = "gemini-3.8-flash", **extra):
    return SimpleNamespace(provider=provider, model=model, temperature=0.0, **extra)


def _ctx(node, llm_model=None, llm_provider=None):
    """Minimal stand-in for the per-task ArtemisContext."""
    return SimpleNamespace(
        llm_config=SimpleNamespace(
            get_agent=lambda name: node,
            get_utils=lambda name: node,
        ),
        llm_model=llm_model,
        llm_provider=llm_provider,
    )


def _real_llm_config() -> LLMConfig:
    """A real LLMConfig (not a stub) whose operator node is gemini-3.8-flash."""
    node = LLMWithFallback(
        provider="google",
        model="gemini-3.8-flash",
        temperature=0.0,
        fallback=LLM(provider="google", model="gemini-3.7-flash", temperature=0.0),
    )
    return LLMConfig(
        planner=node,
        utils=LLMConfigUtils(outputter=node, hopper=node),
        summarizer=node,
        operator=node,
        operator_summarizer=node,
        log_reader_sub_agent=node,
        log_analyzer=node,
        diagnoser=node,
        checker=node,
        planner_avatar=node,
        history_analyzer_expert=node,
        diagnoser_expert=node,
        explorer=node,
    )


def test_override_wins_over_node_config():
    endpoint = _resolve_endpoint(
        _ctx(_node(), llm_model="gpt-5.1", llm_provider="openai"),
        "operator",
    )

    assert endpoint.provider == ModelProvider.OPENAI
    assert endpoint.model_name == "gpt-5.1"


def test_node_config_used_when_no_override():
    endpoint = _resolve_endpoint(_ctx(_node(provider="anthropic", model="claude-x")), "operator")

    assert endpoint.provider == ModelProvider.ANTHROPIC
    assert endpoint.model_name == "claude-x"


def test_model_only_override_keeps_node_provider():
    endpoint = _resolve_endpoint(
        _ctx(_node(provider="google"), llm_model="gemini-3.8-pro"), "planner"
    )

    assert endpoint.provider == ModelProvider.GOOGLE
    assert endpoint.model_name == "gemini-3.8-pro"


def test_blank_override_falls_back_to_node_config():
    endpoint = _resolve_endpoint(_ctx(_node(), llm_model="  ", llm_provider=""), "planner")

    assert endpoint.provider == ModelProvider.GOOGLE
    assert endpoint.model_name == "gemini-3.8-flash"


def test_override_also_applies_to_resolved_fallback():
    node = _node(fallback=_node(model="gemini-3.7-flash"))

    endpoint = _resolve_endpoint(
        _ctx(node, llm_model="gpt-5.1", llm_provider="openai"),
        "operator",
        use_fallback=True,
    )

    assert endpoint.provider == ModelProvider.OPENAI
    assert endpoint.model_name == "gpt-5.1"


def test_override_does_not_mutate_the_shared_llm_config():
    """The override is per task: the shared LLMConfig is only read."""
    config = _real_llm_config()

    def ctx(**override):
        return SimpleNamespace(llm_config=config, **override)

    endpoint = _resolve_endpoint(
        ctx(llm_model="gpt-5.1", llm_provider="openai"),
        "operator",
    )

    assert (endpoint.provider, endpoint.model_name) == (ModelProvider.OPENAI, "gpt-5.1")
    # The node this task overrode is untouched, fallback included.
    assert (config.operator.provider, config.operator.model) == ("google", "gemini-3.8-flash")
    assert (config.operator.fallback.provider, config.operator.fallback.model) == (
        "google",
        "gemini-3.7-flash",
    )
    # Another node and a later task without an override still see the original.
    assert _resolve_endpoint(ctx(), "checker").model_name == "gemini-3.8-flash"
    # A second task may pin a different model on another node at the same time.
    other = _resolve_endpoint(ctx(llm_model="claude-x", llm_provider="anthropic"), "explorer")
    assert (other.provider, other.model_name) == (ModelProvider.ANTHROPIC, "claude-x")
    assert config.operator is config.checker


def test_builder_rejects_provider_without_model():
    """A provider alone pins nothing, so it is refused at build time."""
    with pytest.raises(ValueError, match="llm_provider requires llm_model"):
        TaskRequestBuilder(goal="Audit checkout").with_llm_override(provider="openai")


def test_builder_rejects_blank_model_with_provider():
    builder = TaskRequestBuilder(goal="Audit checkout")

    with pytest.raises(ValueError, match="llm_provider requires llm_model"):
        builder.with_llm_override(model="   ", provider="openai")


def test_builder_rejects_unknown_provider():
    with pytest.raises(ValueError, match="unknown llm_provider"):
        TaskRequestBuilder(goal="Audit checkout").with_llm_override(
            model="gpt-5.1", provider="NotAProvider"
        )


def test_builder_carries_override_on_the_task_request():
    builder = TaskRequestBuilder(goal="Audit checkout")

    request = builder.with_llm_override(model="  gpt-5.1  ", provider=" OpenAI ").build()

    assert request.llm_model == "gpt-5.1"
    assert request.llm_provider == "openai"


def test_builder_blank_override_leaves_request_unset():
    builder = TaskRequestBuilder(goal="Open Settings")

    request = builder.with_llm_override(model="  ").build()

    assert request.llm_model is None
    assert request.llm_provider is None


def _agent_config() -> AgentConfig:
    profile = AgentProfile(name="default", llm_config=_real_llm_config())
    return AgentConfig(
        agent_profiles={"default": profile},
        task_request_defaults=TaskRequestCommon(goal="Audit checkout"),
        default_profile=profile,
        servers=ServerConfig(adb_host="127.0.0.1", adb_port=5037),
    )


def _device_data_for(tmp_path, monkeypatch, request) -> dict:
    """Run the real ``_prepare_tracing`` and return the device_info it stored.

    Only the DataEngine sink is stubbed: it would otherwise create a session in
    the local database. The device_info assembly itself is the real code.
    """
    recorded: dict = {}

    class RecordingDataEngine:
        def __init__(self, ctx):
            recorded["ctx"] = ctx

        def start_session(self, goal, device_info=None, session_id=None):
            recorded["goal"] = goal
            recorded["device_info"] = device_info

    monkeypatch.setattr(agent_module, "DataEngine", RecordingDataEngine)
    agent = SimpleNamespace(_tmp_traces_dir=tmp_path, _config=_agent_config(), _session_id=None)
    task = SimpleNamespace(request=request, get_name=lambda: "task-1")
    context = SimpleNamespace(device=None, execution_setup=None, data_engine=None)

    Agent._prepare_tracing(agent, task, context)

    return recorded["device_info"]


def test_device_data_records_the_llm_override(tmp_path, monkeypatch):
    """The server producer: the override lands in the schemaless device_info."""
    request = (
        TaskRequestBuilder(goal="Audit checkout")
        .using_profile("pro")
        .with_llm_override(model="  gpt-5.1  ", provider=" OpenAI ")
        .build()
    )

    device_data = _device_data_for(tmp_path, monkeypatch, request)

    assert device_data["llm_model"] == "gpt-5.1"
    assert device_data["llm_provider"] == "openai"
    # Alongside the pre-existing echoes, which the console reads the same way.
    assert device_data["profile"] == "pro"
    assert device_data["run_tuning"]


def test_device_data_omits_the_llm_keys_without_an_override(tmp_path, monkeypatch):
    """Legacy behaviour: no override means no keys, so old and new rows agree."""
    request = TaskRequestBuilder(goal="Open Settings").using_profile("flash").build()

    device_data = _device_data_for(tmp_path, monkeypatch, request)

    assert "llm_model" not in device_data
    assert "llm_provider" not in device_data
    assert device_data["profile"] == "flash"
