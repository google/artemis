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

"""Token-usage normalization across the LangChain, generate_content and Interactions shapes."""

from types import SimpleNamespace

from artemis.llm.google import normalize_usage, usage_from_message
from langchain_core.messages import AIMessage


def test_langchain_usage_metadata_shape():
    usage = normalize_usage(
        {
            "input_tokens": 1200,
            "output_tokens": 80,
            "total_tokens": 1280,
            "input_token_details": {"cache_read": 900},
            "output_token_details": {"reasoning": 30},
        }
    )
    assert usage == {
        "prompt_tokens": 1200,
        "completion_tokens": 80,
        "total_tokens": 1280,
        "cached_tokens": 900,
        "thought_tokens": 30,
        "tool_use_tokens": 0,
    }


def test_generate_content_usage_metadata_object_shape():
    raw = SimpleNamespace(
        prompt_token_count=500,
        candidates_token_count=25,
        total_token_count=560,
        cached_content_token_count=400,
        thoughts_token_count=35,
        tool_use_prompt_token_count=None,
    )
    assert normalize_usage(raw) == {
        "prompt_tokens": 500,
        "completion_tokens": 25,
        "total_tokens": 560,
        "cached_tokens": 400,
        "thought_tokens": 35,
        "tool_use_tokens": 0,
    }


def test_interactions_usage_shape_counts_tool_use_and_thoughts():
    raw = SimpleNamespace(
        total_input_tokens=191,
        total_output_tokens=655,
        total_tokens=10571,
        total_cached_tokens=0,
        total_thought_tokens=12,
        total_tool_use_tokens=9725,
    )
    assert normalize_usage(raw) == {
        "prompt_tokens": 191,
        "completion_tokens": 655,
        "total_tokens": 10571,
        "cached_tokens": 0,
        "thought_tokens": 12,
        "tool_use_tokens": 9725,
    }


def test_total_is_derived_when_missing_and_empty_payloads_return_none():
    assert normalize_usage({"prompt_tokens": 3, "completion_tokens": 4})["total_tokens"] == 7
    assert normalize_usage(None) is None
    assert normalize_usage({}) is None
    assert normalize_usage({"prompt_tokens": "not-a-number"}) is None
    assert normalize_usage(SimpleNamespace()) is None


def test_usage_from_message_prefers_langchain_then_response_metadata():
    msg = AIMessage(content="ok")
    msg.usage_metadata = {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}
    assert usage_from_message(msg)["prompt_tokens"] == 10

    fallback = AIMessage(content="ok")
    fallback.response_metadata = {"token_usage": {"prompt_tokens": 7, "completion_tokens": 1}}
    assert usage_from_message(fallback) == {
        "prompt_tokens": 7,
        "completion_tokens": 1,
        "total_tokens": 8,
        "cached_tokens": 0,
        "thought_tokens": 0,
        "tool_use_tokens": 0,
    }

    assert usage_from_message(AIMessage(content="none")) is None
    assert usage_from_message(object()) is None
