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

"""Coordinate targets must be described by the model, never inferred.

The Flash/Validator dialect addresses every target by coordinates, so the
executor refuses a click, long press, focused input, coordinate swipe or click
sequence that does not say what it is aiming at. The description is recorded
by the runner and never reaches the wire.
"""

from unittest.mock import Mock

import pytest

from artemis.mcp.action_executor import McpActionExecutor, _ArgError


def _make_executor(width=1080, height=2400):
    ctx = Mock()
    ctx.device.device_width = width
    ctx.device.device_height = height
    actuator = Mock()
    actuator.controller = Mock()
    return McpActionExecutor(ctx, actuator=actuator)


def _state():
    state = Mock()
    state.indexed_elements = []
    state.indexed_points = [[540, 1440], [745, 1440]]
    state.latest_ui_hierarchy = None
    return state


@pytest.mark.parametrize(
    "name, args, label",
    [
        ("click", {"target": [500, 600]}, "click"),
        ("long_press", {"target": [500, 600]}, "long press"),
        ("input_text", {"text": "hi", "target": [500, 600]}, "input text"),
        ("swipe", {"start": [200, 600], "end": [800, 600]}, "swipe"),
    ],
)
def test_coordinate_targets_without_description_are_refused(name, args, label):
    executor = _make_executor()
    with pytest.raises(_ArgError) as excinfo:
        executor._translate(name, args, _state())
    message = str(excinfo.value)
    assert message.startswith(f"Error during {label}")
    assert "target_description" in message


@pytest.mark.parametrize("blank", ["", "   ", None, 7])
def test_blank_or_non_string_description_is_refused(blank):
    executor = _make_executor()
    with pytest.raises(_ArgError):
        executor._translate("click", {"target": [500, 600], "target_description": blank}, _state())


def test_description_is_accepted_and_kept_off_the_wire():
    executor = _make_executor()
    wire_name, wire_args, _, _ = executor._translate(
        "click", {"target": [500, 600], "target_description": "play button"}, _state()
    )
    assert wire_name == "click"
    assert wire_args == {"target": [500, 600], "times": 1, "delay_ms": 100}
    assert "target_description" not in wire_args

    _, swipe_args, _, _ = executor._translate(
        "swipe",
        {"start": [200, 600], "end": [800, 600], "target_description": "brightness knob"},
        _state(),
    )
    assert swipe_args == {"start": [200, 600], "end": [800, 600], "duration_ms": 400}


def test_recorded_target_is_the_cleaned_description_in_pro_shape():
    """The recorded semantics come from the same check that requires them."""
    executor = _make_executor()
    _, _, _, recorded = executor._translate(
        "click", {"target": [1, 2], "target_description": " ok "}, _state()
    )
    assert recorded == {"target_description": "ok"}

    _, _, _, recorded = executor._translate(
        "long_press", {"target": [1, 2], "target_description": "thumbnail"}, _state()
    )
    assert recorded == {"target_description": "thumbnail"}

    _, _, _, recorded = executor._translate(
        "click_sequence", {"sequence": [[1, 2]], "target_descriptions": ["a "]}, _state()
    )
    assert recorded == {"target_descriptions": ["a"]}

    # No coordinate target: nothing is recorded, nothing inferred.
    _, _, _, recorded = executor._translate("press_key", {"key": "BACK"}, _state())
    assert recorded == {}


def test_coordinate_swipe_records_its_description():
    executor = _make_executor()
    _, wire_args, _, recorded = executor._translate(
        "swipe",
        {"start": [200, 600], "end": [800, 600], "target_description": " brightness knob "},
        _state(),
    )
    assert recorded == {"target_description": "brightness knob"}
    assert "target_description" not in wire_args


def test_targeted_input_records_its_description():
    executor = _make_executor()
    _, wire_args, _, recorded = executor._translate(
        "input_text",
        {"text": "hi", "target": [500, 600], "target_description": "search input"},
        _state(),
    )
    assert wire_args["target"] == [500, 600]
    assert recorded == {"target_description": "search input"}
    assert "target_description" not in wire_args


def test_focused_typing_without_a_target_needs_no_description():
    executor = _make_executor()
    _, wire_args, _, recorded = executor._translate(
        "input_text", {"text": "hi", "target": None}, _state()
    )
    assert wire_args["target"] is None
    assert recorded == {}


def test_focused_typing_ignores_a_stray_description():
    """Typing into the focused field has no coordinate target: a description the
    model tacked on anyway is not recorded, so no self-described target enters
    the history for an action that aimed at nothing."""
    executor = _make_executor()
    _, wire_args, _, recorded = executor._translate(
        "input_text",
        {"text": "hi", "target": None, "target_description": "search input"},
        _state(),
    )
    assert wire_args["target"] is None
    assert recorded == {}


def test_directional_swipe_needs_no_description():
    executor = _make_executor()
    wire_name, wire_args, _, recorded = executor._translate("swipe", {"direction": "up"}, _state())
    assert wire_name == "swipe"
    assert "start" in wire_args and "end" in wire_args
    assert recorded == {}


def test_directional_swipe_ignores_a_stray_description():
    executor = _make_executor()
    _, wire_args, _, recorded = executor._translate(
        "swipe", {"direction": "up", "target_description": "the feed"}, _state()
    )
    assert "target_description" not in wire_args
    assert recorded == {}


def test_click_sequence_requires_one_description_per_entry():
    executor = _make_executor()
    sequence = [[500, 600], [690, 600], [10, 10]]

    with pytest.raises(_ArgError) as excinfo:
        executor._translate("click_sequence", {"sequence": sequence}, _state())
    assert "target_descriptions" in str(excinfo.value)
    assert "3 expected" in str(excinfo.value)

    # Wrong length is refused too.
    with pytest.raises(_ArgError):
        executor._translate(
            "click_sequence",
            {"sequence": sequence, "target_descriptions": ["video body", "skip"]},
            _state(),
        )

    _, wire_args, _, recorded = executor._translate(
        "click_sequence",
        {"sequence": sequence, "target_descriptions": ["video body", "digit 2", "corner"]},
        _state(),
    )
    assert wire_args["sequence"] == [[500, 600], [690, 600], [10, 10]]
    assert "target_descriptions" not in wire_args
    assert recorded == {"target_descriptions": ["video body", "digit 2", "corner"]}


@pytest.mark.parametrize("index_entry", [2, "2", 2.0, [2]])
def test_click_sequence_refuses_element_indices(index_entry):
    """click_sequence takes coordinate pairs only. An index would be resolved
    against the element list and then recorded under the model's own
    description, reading back as a self-described coordinate target."""
    executor = _make_executor()
    sequence = [[500, 600], index_entry, [10, 10]]

    with pytest.raises(_ArgError) as excinfo:
        executor._translate(
            "click_sequence",
            {"sequence": sequence, "target_descriptions": ["video body", "digit 2", "corner"]},
            _state(),
        )
    message = str(excinfo.value)
    assert message.startswith("Error during click sequence")
    assert "entry 2" in message
    assert "coordinate" in message
    assert "ask_explorer" in message


def test_click_sequence_accepts_a_serialized_pair_list():
    executor = _make_executor()
    _, wire_args, _, _ = executor._translate(
        "click_sequence",
        {"sequence": "[[500, 600], [10, 10]]", "target_descriptions": ["video body", "corner"]},
        _state(),
    )
    assert wire_args["sequence"] == [[500, 600], [10, 10]]
