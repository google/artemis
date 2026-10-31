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

"""Request shape for adaptive-only Claude models (Sonnet 5.5, Opus 5.5, ...)."""

import json
from unittest.mock import MagicMock

import anthropic
import httpx
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
import pytest

from artemis.llm.claude import (
    THINKING_BINDING_BETA,
    binds_thinking_blocks,
    claude_version,
    effort_for,
    is_adaptive_only,
)
from artemis.llm.router import ModelEndpoint, ModelFactory, ModelProvider


@pytest.mark.parametrize(
    "model, expected",
    [
        ("claude-sonnet-5-5", ("sonnet", (5, 5))),
        ("anthropic.claude-opus-4-8", ("opus", (4, 8))),
        ("claude-sonnet-4-5-20250929", ("sonnet", (4, 5))),
        ("claude-opus-4-20250514", ("opus", (4, 0))),
        ("claude-fable-5", ("fable", (5, 0))),
        ("claude-3-7-sonnet-20250219", None),
        ("gpt-4o", None),
        (None, None),
    ],
)
def test_claude_version(model, expected):
    assert claude_version(model) == expected


@pytest.mark.parametrize(
    "model, adaptive_only, binds",
    [
        ("claude-sonnet-5-5", True, True),
        ("claude-opus-5-5", True, True),
        ("claude-haiku-5-5", True, True),
        ("claude-fable-5-1", True, True),
        ("claude-fable-5", True, False),
        ("claude-opus-5", True, False),
        ("claude-sonnet-5", True, False),
        ("claude-opus-4-7", True, False),
        ("claude-opus-4-6", False, False),
        ("claude-sonnet-4-6", False, False),
        ("claude-haiku-4-5", False, False),
        ("claude-3-7-sonnet-20250219", False, False),
    ],
)
def test_capabilities(model, adaptive_only, binds):
    assert is_adaptive_only(model) is adaptive_only
    assert binds_thinking_blocks(model) is binds


def test_effort_for():
    assert effort_for("HIGH", 2048) == "high"
    assert effort_for("bogus", None) is None
    assert effort_for(None, 1024) == "low"
    assert effort_for(None, 8192) == "medium"
    assert effort_for(None, 32768) == "high"
    assert effort_for(None, None) is None


def _capture_request(model):
    """Sends one tool-use continuation through ``model`` and returns the wire request."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": model.model,
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    model.__dict__["_client"] = anthropic.Anthropic(
        api_key="sk-ant-test",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    history = [
        HumanMessage("open the app"),
        AIMessage(
            content=[
                {"type": "thinking", "thinking": "", "signature": "sig"},
                {"type": "tool_use", "id": "t1", "name": "tap", "input": {}},
            ],
            tool_calls=[{"name": "tap", "args": {}, "id": "t1"}],
        ),
        ToolMessage("tapped", tool_call_id="t1"),
    ]
    model.invoke(history)
    return captured["body"], captured["headers"]


def test_sonnet_5_5_request_drops_stale_thinking_blocks():
    """Sonnet 5.5: no temperature, budget mapped to effort, drop_block enabled."""
    endpoint = ModelEndpoint(
        provider=ModelProvider.ANTHROPIC,
        model_name="claude-sonnet-5-5",
        api_key="sk-ant-test",
        thinking_budget=8192,
    )
    body, headers = _capture_request(ModelFactory.create_model(endpoint))

    assert "temperature" not in body
    assert body["thinking"] == {
        "type": "adaptive",
        "block_binding": {"prefix_mismatch_behavior": "drop_block"},
    }
    assert body["output_config"] == {"effort": "medium"}
    assert THINKING_BINDING_BETA in headers["anthropic-beta"]
    # The earlier thinking block is still replayed unchanged.
    assert body["messages"][1]["content"][0] == {
        "type": "thinking",
        "thinking": "",
        "signature": "sig",
    }


def test_sonnet_5_5_defaults_send_no_sampling_or_budget():
    endpoint = ModelEndpoint(
        provider=ModelProvider.ANTHROPIC, model_name="claude-sonnet-5-5", api_key="sk-ant-test"
    )
    body, _ = _capture_request(ModelFactory.create_model(endpoint))
    assert "temperature" not in body
    assert "budget_tokens" not in body["thinking"]
    assert "output_config" not in body
    assert body["max_tokens"] == 16000


def test_opus_4_8_effort_uses_adaptive_thinking_without_binding():
    endpoint = ModelEndpoint(
        provider=ModelProvider.ANTHROPIC,
        model_name="claude-opus-4-8",
        api_key="sk-ant-test",
        reasoning_effort="high",
    )
    body, headers = _capture_request(ModelFactory.create_model(endpoint))
    assert "temperature" not in body
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "high"}
    assert "anthropic-beta" not in headers


def test_opus_4_8_without_effort_leaves_thinking_unset():
    endpoint = ModelEndpoint(
        provider=ModelProvider.ANTHROPIC, model_name="claude-opus-4-8", api_key="sk-ant-test"
    )
    model = ModelFactory.create_model(endpoint)
    assert model.thinking is None
    assert model.temperature is None


def test_legacy_claude_keeps_budget_tokens():
    endpoint = ModelEndpoint(
        provider=ModelProvider.ANTHROPIC,
        model_name="claude-sonnet-4-6",
        api_key="sk-ant-test",
        reasoning_effort="medium",
    )
    model = ModelFactory.create_model(endpoint)
    assert model.thinking == {"type": "enabled", "budget_tokens": 8192}
    assert model.temperature == 1.0


@pytest.mark.parametrize(
    "model_name, expected_method",
    [("claude-sonnet-5-5", "json_schema"), ("claude-sonnet-4-6", None)],
)
def test_structured_output_avoids_forced_tool_choice(model_name, expected_method):
    """Adaptive-only Claude models reject forced tool_choice, so use json_schema."""
    from artemis.services.llm import RobustChatModelWrapper

    base = MagicMock()
    endpoint = ModelEndpoint(provider=ModelProvider.ANTHROPIC, model_name=model_name)
    RobustChatModelWrapper(base, None, endpoint=endpoint).with_structured_output(dict)
    assert base.with_structured_output.call_args.kwargs.get("method") == expected_method


def test_structured_output_keeps_explicit_method():
    from artemis.services.llm import RobustChatModelWrapper

    base = MagicMock()
    endpoint = ModelEndpoint(provider=ModelProvider.ANTHROPIC, model_name="claude-sonnet-5-5")
    RobustChatModelWrapper(base, None, endpoint=endpoint).with_structured_output(
        dict, method="function_calling"
    )
    assert base.with_structured_output.call_args.kwargs["method"] == "function_calling"
