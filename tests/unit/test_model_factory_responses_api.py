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

"""ARTEMIS_OPENAI_RESPONSES_API opts matching OpenAI-compatible models into the Responses API."""

from unittest.mock import patch

from artemis.llm.router import ModelEndpoint, ModelFactory, ModelProvider


def _kwargs(model_name: str) -> dict:
    with patch("langchain_openai.ChatOpenAI") as chat_openai:
        ModelFactory.create_model(
            ModelEndpoint(provider=ModelProvider.CUSTOM, model_name=model_name)
        )
    return chat_openai.call_args.kwargs


def test_matching_model_uses_the_responses_api(monkeypatch):
    monkeypatch.setenv("ARTEMIS_OPENAI_RESPONSES_API", r"openai\.gpt-6")
    assert _kwargs("global.openai.gpt-6.1-sol")["use_responses_api"] is True


def test_non_matching_model_keeps_chat_completions(monkeypatch):
    monkeypatch.setenv("ARTEMIS_OPENAI_RESPONSES_API", r"openai\.gpt-6")
    assert "use_responses_api" not in _kwargs("qwen.qwen3-vl-235b-a22b")


def test_unset_opt_in_changes_nothing(monkeypatch):
    monkeypatch.delenv("ARTEMIS_OPENAI_RESPONSES_API", raising=False)
    assert "use_responses_api" not in _kwargs("global.openai.gpt-6.1-sol")
