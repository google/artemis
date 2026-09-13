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

"""Unit tests for ModelFactory instantiation and grounding tool wiring."""

from artemis.llm.router import ModelEndpoint, ModelFactory, ModelProvider


def test_model_factory_anthropic_instantiation():
    """Verify Anthropic Claude model instantiation with thinking budget."""
    anth_ep = ModelEndpoint(
        provider=ModelProvider.ANTHROPIC,
        model_name="claude-3-7-sonnet-20250219",
        api_key="sk-ant-test-key",
        reasoning_effort="high",
    )
    model = ModelFactory.create_model(anth_ep)
    assert model.model == "claude-3-7-sonnet-20250219"
    assert getattr(model, "thinking", None) == {"type": "enabled", "budget_tokens": 32768}


def test_model_factory_openai_instantiation():
    """Verify OpenAI model instantiation with reasoning effort."""
    oai_ep = ModelEndpoint(
        provider=ModelProvider.OPENAI,
        model_name="o3-mini",
        api_key="sk-test-key",
        reasoning_effort="medium",
    )
    model = ModelFactory.create_model(oai_ep)
    assert model.model_name == "o3-mini"
    assert getattr(model, "reasoning_effort", None) == "medium"


def test_robust_chat_model_wrapper_grounding_google():
    """Verify RobustChatModelWrapper auto-injects google_search and server-side tool config for Gemini."""
    from artemis.services.llm import RobustChatModelWrapper
    from langchain_core.tools import tool

    @tool
    def dummy_tool(query: str) -> str:
        """A test tool."""
        return query

    class MockGeminiModel:
        def __init__(self):
            self.bound_tools = []
            self.bound_kwargs = {}

        def bind_tools(self, tools, *args, **kwargs):
            self.bound_tools = tools
            self.bound_kwargs = kwargs
            return self

    # Case 1: enable_grounding=True automatically attaches google_search and tool_config
    ep_google = ModelEndpoint(
        provider=ModelProvider.GOOGLE,
        model_name="gemini-2.5-flash",
        enable_grounding=True,
    )
    wrapper = RobustChatModelWrapper(MockGeminiModel(), endpoint=ep_google)
    bound = wrapper.bind_tools([dummy_tool])

    assert any(isinstance(t, dict) and "google_search" in t for t in bound.base_model.bound_tools)
    assert bound.base_model.bound_kwargs.get("tool_config") == {
        "include_server_side_tool_invocations": True
    }

    # Case 2: Explicit google_search in tools automatically ensures tool_config
    ep_google_no_ground = ModelEndpoint(
        provider=ModelProvider.GOOGLE,
        model_name="gemini-2.5-flash",
        enable_grounding=False,
    )
    wrapper_explicit = RobustChatModelWrapper(MockGeminiModel(), endpoint=ep_google_no_ground)
    bound_explicit = wrapper_explicit.bind_tools([dummy_tool, {"google_search": {}}])

    assert any(
        isinstance(t, dict) and "google_search" in t for t in bound_explicit.base_model.bound_tools
    )
    assert bound_explicit.base_model.bound_kwargs.get("tool_config") == {
        "include_server_side_tool_invocations": True
    }


def test_robust_chat_model_wrapper_grounding_non_google():
    """Verify RobustChatModelWrapper ignores grounding and strips google_search for non-Google providers."""
    from artemis.services.llm import RobustChatModelWrapper
    from langchain_core.tools import tool

    @tool
    def dummy_tool(query: str) -> str:
        """A test tool."""
        return query

    class MockOpenAIModel:
        def __init__(self):
            self.bound_tools = []
            self.bound_kwargs = {}

        def bind_tools(self, tools, *args, **kwargs):
            self.bound_tools = tools
            self.bound_kwargs = kwargs
            return self

    ep_openai = ModelEndpoint(
        provider=ModelProvider.OPENAI,
        model_name="gpt-4o",
        enable_grounding=True,
    )
    wrapper = RobustChatModelWrapper(MockOpenAIModel(), endpoint=ep_openai)
    bound = wrapper.bind_tools([dummy_tool, {"google_search": {}}])

    # Should ignore grounding and strip {"google_search": {}}
    assert len(bound.base_model.bound_tools) == 1
    assert bound.base_model.bound_tools[0].name == "dummy_tool"
    assert "tool_config" not in bound.base_model.bound_kwargs


def test_chat_google_generative_ai_process_tool_config_patch():
    """Verify patched ChatGoogleGenerativeAI preserves include_server_side_tool_invocations even with tool_choice."""
    from artemis.llm.router import ModelFactory
    from langchain_google_genai import ChatGoogleGenerativeAI
    from langchain_core.tools import tool

    @tool
    def dummy_tool(query: str) -> str:
        """A test tool."""
        return query

    # Trigger patching via Google model creation
    ep = ModelEndpoint(provider=ModelProvider.GOOGLE, model_name="gemini-2.5-flash", api_key="fake")
    model = ModelFactory.create_model(ep)
    assert isinstance(model, ChatGoogleGenerativeAI)

    formatted_tools = model._format_tools([dummy_tool, {"google_search": {}}], None)

    # 1. With tool_choice="auto"
    config_auto = model._process_tool_config("auto", None, formatted_tools)
    assert config_auto is not None
    assert config_auto.include_server_side_tool_invocations is True

    # 2. With tool_choice="any"
    config_any = model._process_tool_config("any", None, formatted_tools)
    assert config_any is not None
    assert config_any.include_server_side_tool_invocations is True

    # 3. With explicit tool_config
    config_explicit = model._process_tool_config(
        None, {"include_server_side_tool_invocations": True}, formatted_tools
    )
    assert config_explicit is not None
    assert config_explicit.include_server_side_tool_invocations is True
