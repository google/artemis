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

"""Tool-call id deduplication on the OpenAI wire.

Some OpenAI-compatible servers (local model endpoints) restart their
tool_call id counter on every response, so a conversation accumulates
assistant/tool pairs all named ``call_0``. Strict validators reject the
ambiguous ids; the gateway renames repeats before the request leaves.
"""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from artemis.llm.router import ModelEndpoint, ModelProvider
from artemis.services.llm import RobustChatModelWrapper


def _wrapper(provider: ModelProvider) -> RobustChatModelWrapper:
    endpoint = ModelEndpoint(provider=provider, model_name="m")
    return RobustChatModelWrapper(object(), endpoint=endpoint)


def _ai(call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"id": call_id, "name": "click", "args": {"target": 1}, "type": "tool_call"}],
    )


def test_repeated_tool_call_ids_are_renamed_with_their_results():
    messages = [
        HumanMessage(content="go"),
        _ai("call_0"),
        ToolMessage(tool_call_id="call_0", content="ok"),
        HumanMessage(content="next"),
        _ai("call_0"),
        ToolMessage(tool_call_id="call_0", content="ok"),
    ]

    (result,) = _wrapper(ModelProvider.CUSTOM)._dedupe_tool_call_ids((messages,))

    assert result[1].tool_calls[0]["id"] == "call_0"
    assert result[2].tool_call_id == "call_0"
    # Second round keeps the conversation unambiguous.
    assert result[4].tool_calls[0]["id"] == "call_0#1"
    assert result[5].tool_call_id == "call_0#1"
    # Originals are not mutated.
    assert messages[4].tool_calls[0]["id"] == "call_0"
    assert messages[5].tool_call_id == "call_0"


def test_unique_ids_pass_through_unchanged():
    messages = [_ai("call_0"), ToolMessage(tool_call_id="call_0", content="ok")]
    args = (messages,)
    assert _wrapper(ModelProvider.CUSTOM)._dedupe_tool_call_ids(args) is args


def test_parallel_calls_with_the_same_id_map_in_order():
    messages = [
        AIMessage(
            content="",
            tool_calls=[
                {"id": "call_0", "name": "a", "args": {}, "type": "tool_call"},
                {"id": "call_0", "name": "b", "args": {}, "type": "tool_call"},
            ],
        ),
        ToolMessage(tool_call_id="call_0", content="first"),
        ToolMessage(tool_call_id="call_0", content="second"),
    ]

    (result,) = _wrapper(ModelProvider.OPENAI)._dedupe_tool_call_ids((messages,))

    assert [c["id"] for c in result[0].tool_calls] == ["call_0", "call_0#1"]
    assert result[1].tool_call_id == "call_0"
    assert result[2].tool_call_id == "call_0#1"


def test_non_openai_wire_providers_keep_ids_as_serialized():
    messages = [_ai("call_0"), ToolMessage(tool_call_id="call_0", content="ok"), _ai("call_0")]
    args = (messages,)
    for provider in (ModelProvider.GOOGLE, ModelProvider.VERTEX_AI, ModelProvider.ANTHROPIC):
        assert _wrapper(provider)._dedupe_tool_call_ids(args) is args


def test_non_message_inputs_pass_through():
    wrapper = _wrapper(ModelProvider.CUSTOM)
    assert wrapper._dedupe_tool_call_ids(("plain prompt",)) == ("plain prompt",)
    assert wrapper._dedupe_tool_call_ids(()) == ()
