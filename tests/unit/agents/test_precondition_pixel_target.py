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

"""Tests for pixel safety-net target descriptions."""

from pathlib import Path
from unittest.mock import MagicMock

from artemis.agents.validator import precondition_pixel as pp
from langchain_core.messages import HumanMessage, SystemMessage

_PNG = b"fake-image-bytes"


def _texts(messages: list) -> list[str]:
    human = messages[1]
    return [part["text"] for part in human.content if part["type"] == "text"]


def test_named_control_target_block_precedes_images():
    item = {
        "action": "tap",
        "coordinates": [1001, 718],
        "target_text": "进入全屏模式",
        "target_resource_id": "com.google.android.youtube:id/fullscreen_button",
        "target_class": "android.widget.ImageView",
        "target_bounds": [922, 655, 1080, 781],
    }
    state = MagicMock()
    state.operator_native_thinking = "identified 进入全屏模式"
    state.operator_raw_thinking = None

    messages = pp._build_messages("SYSTEM", _PNG, _PNG, item, state)

    assert isinstance(messages[0], SystemMessage)
    assert isinstance(messages[1], HumanMessage)
    first_text = messages[1].content[0]["text"]
    assert first_text.startswith("[Target]")
    assert "Kind: specific UI control" in first_text
    assert "Label: 进入全屏模式" in first_text
    assert "Resource ID: com.google.android.youtube:id/fullscreen_button" in first_text
    assert "Class: android.widget.ImageView" in first_text
    assert "[922, 655, 1080, 781]" in first_text
    assert messages[1].content[1]["text"] == "[Image 1 (Reference)]"
    assert any("Original Thinking" in t for t in _texts(messages))
    assert any("Action: tap" in t for t in _texts(messages))


def test_coordinates_only_target_is_labelled_as_surface():
    item = {"action": "tap", "coordinates": [540, 400]}

    messages = pp._build_messages("SYSTEM", _PNG, _PNG, item, state=None)

    first_text = messages[1].content[0]["text"]
    assert "Kind: coordinates only" in first_text
    assert "Label:" not in first_text
    assert "Resource ID:" not in first_text
    assert not any("Original Thinking" in t for t in _texts(messages))


def test_label_alone_counts_as_named_control():
    item = {"action": "tap", "coordinates": [10, 10], "target_text": "Submit"}
    text = pp._describe_target(item)
    assert "Kind: specific UI control" in text
    assert "Label: Submit" in text
    assert "Resource ID" not in text


def test_unlabelled_hit_test_element_is_still_a_control():
    item = {
        "action": "tap",
        "coordinates": [1001, 718],
        "target_class": "android.widget.ImageView",
        "target_bounds": [922, 655, 1080, 781],
        "target_label_source": "hit_test",
    }
    text = pp._describe_target(item)
    assert "Kind: specific UI control" in text
    assert "unlabelled" in text
    assert "Class: android.widget.ImageView" in text
    assert "[922, 655, 1080, 781]" in text


def test_prompt_rules_are_keyed_on_target_kind():
    prompt = Path(pp.__file__).with_name("pixel_safety_net.md").read_text(encoding="utf-8")
    assert "`Kind: specific UI control`" in prompt
    assert "`Kind: coordinates only`" in prompt
    assert "hidden" in prompt
    assert "Identify what appears under the red dot" in prompt
