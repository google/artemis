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

"""tool_result_messages: one helper decides how a tool result — with or
without images — enters the conversation, per LLM provider."""

from enum import StrEnum
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage, ToolMessage

from artemis.tools.tool_wrapper import (
    resolve_image_carrier,
    split_multimodal_result,
    tool_result_messages,
)

IMAGE = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}
SHOT = [{"type": "text", "text": "Screenshot of step 7 (pre-action) is attached."}, IMAGE]


class _Provider(StrEnum):
    GOOGLE = "google"
    VERTEX_AI = "vertexai"


def _llm(provider):
    return SimpleNamespace(endpoint=SimpleNamespace(provider=provider))


@pytest.mark.parametrize(
    ("provider", "carrier"),
    [
        ("google", "tool"),
        ("anthropic", "tool"),
        ("vertexai", "human"),
        ("openai", "human"),
        ("openrouter", "human"),
        ("xai", "human"),
        ("ollama", "human"),
        (None, "human"),
    ],
)
def test_resolve_image_carrier_by_provider_name(provider, carrier):
    assert resolve_image_carrier(provider) == carrier
    assert resolve_image_carrier(_llm(provider)) == carrier


def test_resolve_image_carrier_accepts_enums_and_endpoint_less_llms():
    assert resolve_image_carrier(_Provider.GOOGLE) == "tool"
    assert resolve_image_carrier(_llm(_Provider.VERTEX_AI)) == "human"
    assert resolve_image_carrier(SimpleNamespace()) == "human"


def test_split_multimodal_result():
    assert split_multimodal_result("plain") == ("plain", [])
    assert split_multimodal_result(None) == ("", [])
    assert split_multimodal_result(ToolMessage(content="x", tool_call_id="t")) == ("x", [])
    text, images = split_multimodal_result(SHOT)
    assert text == "Screenshot of step 7 (pre-action) is attached."
    assert images == [IMAGE]


def test_text_only_results_stay_a_single_tool_message():
    msgs = tool_result_messages("tc-1", "Step 3 replay", name="replay_steps", status="error")
    assert len(msgs) == 1
    assert isinstance(msgs[0], ToolMessage)
    assert msgs[0].content == "Step 3 replay"
    assert msgs[0].tool_call_id == "tc-1"
    assert msgs[0].name == "replay_steps"
    assert msgs[0].status == "error"

    # Text-block lists pass through unchanged; other objects are stringified.
    blocks = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
    assert tool_result_messages("tc", blocks)[0].content == blocks
    assert tool_result_messages("tc", {"k": 1})[0].content == "{'k': 1}"


def test_tool_carrier_puts_the_image_inside_the_tool_message():
    msgs = tool_result_messages("tc-2", SHOT, name="get_step_screenshot", llm=_llm("google"))
    assert len(msgs) == 1
    assert isinstance(msgs[0], ToolMessage)
    assert msgs[0].content == SHOT
    assert msgs[0].name == "get_step_screenshot"


def test_human_carrier_splits_text_and_image_with_a_caption():
    msgs = tool_result_messages(
        "tc-3", SHOT, name="get_step_screenshot", status="success", llm=_llm("openai")
    )
    assert len(msgs) == 2
    tool_msg, human_msg = msgs
    assert isinstance(tool_msg, ToolMessage)
    assert tool_msg.content == "Screenshot of step 7 (pre-action) is attached."
    assert tool_msg.tool_call_id == "tc-3"
    assert isinstance(human_msg, HumanMessage)
    assert human_msg.content[0] == {
        "type": "text",
        "text": "[Screenshot returned by get_step_screenshot for step 7]",
    }
    assert human_msg.content[1] == IMAGE


def test_explicit_carrier_wins_over_the_llm():
    msgs = tool_result_messages("tc", SHOT, image_carrier="human", llm=_llm("google"))
    assert len(msgs) == 2
    msgs = tool_result_messages("tc", SHOT, image_carrier="tool", llm=_llm("openai"))
    assert len(msgs) == 1
