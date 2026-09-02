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

"""Tests for record-time target semantics enrichment in McpActionExecutor."""

from unittest.mock import Mock

from artemis.mcp.action_executor import McpActionExecutor


def _make_executor(width=1080, height=2400):
    ctx = Mock()
    ctx.device.device_width = width
    ctx.device.device_height = height
    actuator = Mock()
    actuator.controller = Mock()
    return McpActionExecutor(ctx, actuator=actuator)


def _state_with_elements(elements):
    state = Mock()
    state.indexed_elements = elements
    return state


def test_click_target_semantics_from_pre_action_frame():
    executor = _make_executor()
    # Normalized (500, 600) on a 1080x2400 device -> pixel (540, 1440).
    state = _state_with_elements(
        [
            {
                "text": "Confirm",
                "bounds": [500, 1400, 600, 1480],
                "class": "android.widget.Button",
                "resource_id": "btn_confirm",
                "is_ocr": False,
            }
        ]
    )
    semantics = executor._target_semantics("click", {"target": [500, 600]}, state)
    assert semantics == {
        "target_label_source": "hit_test",
        "target_text": "Confirm",
        "target_class": "android.widget.Button",
        "target_resource_id": "btn_confirm",
    }


def test_semantics_degrade_to_none_without_element_data():
    executor = _make_executor()

    # Empty perception data
    semantics = executor._target_semantics(
        "click", {"target": [500, 600]}, _state_with_elements([])
    )
    assert semantics == {"target_label_source": "none"}

    # No state at all
    semantics = executor._target_semantics("click", {"target": [500, 600]}, None)
    assert semantics == {"target_label_source": "none"}


def test_non_targeted_actions_and_missing_targets_skip_enrichment():
    executor = _make_executor()
    state = _state_with_elements([{"text": "X", "bounds": [0, 0, 10, 10]}])

    assert executor._target_semantics("press_key", {"key": "BACK"}, state) is None
    # input_text without a coordinate target (focused field typing)
    assert executor._target_semantics("input_text", {"text": "hi", "target": None}, state) is None
    # Multi-point actions without resolvable points skip enrichment too.
    assert executor._target_semantics("click_sequence", {"sequence": []}, state) is None
    assert executor._target_semantics("swipe", {"start": None, "end": None}, state) is None


def test_click_sequence_hit_tests_every_point_first_point_is_main_label():
    """M5: multi-point actions get per-point best-effort semantics; the first
    point's fields are hoisted as the action's main label."""
    executor = _make_executor()
    state = _state_with_elements(
        [
            {
                "text": "Digit 1",
                "bounds": [500, 1400, 600, 1480],
                "class": "android.widget.Button",
                "is_ocr": False,
            },
            {
                "text": "Digit 2",
                "bounds": [700, 1400, 800, 1480],
                "class": "android.widget.Button",
                "is_ocr": False,
            },
        ]
    )
    # Normalized points -> pixels: (500,600)->(540,1440) hits Digit 1;
    # (690,600)->(745,1440) hits Digit 2; (10,10)->(10,24) hits nothing.
    semantics = executor._target_semantics(
        "click_sequence",
        {"sequence": [[500, 600], [690, 600], [10, 10]], "delay_ms": 50},
        state,
    )
    assert semantics["target_text"] == "Digit 1"
    assert semantics["target_label_source"] == "hit_test"
    per_point = semantics["points_semantics"]
    assert len(per_point) == 3
    assert per_point[0]["target_text"] == "Digit 1"
    assert per_point[1]["target_text"] == "Digit 2"
    assert per_point[2] == {"target_label_source": "none"}


def test_swipe_hit_tests_start_and_end_start_is_main_label():
    executor = _make_executor()
    state = _state_with_elements(
        [
            {
                "text": "Brightness slider",
                "bounds": [100, 1400, 900, 1480],
                "class": "android.widget.SeekBar",
                "is_ocr": False,
            }
        ]
    )
    semantics = executor._target_semantics(
        "swipe", {"start": [200, 600], "end": [800, 600], "duration_ms": 400}, state
    )
    assert semantics["target_text"] == "Brightness slider"
    assert semantics["start_semantics"]["target_text"] == "Brightness slider"
    assert semantics["end_semantics"]["target_text"] == "Brightness slider"
    assert semantics["target_label_source"] == "hit_test"


def test_long_press_uses_ocr_fallback():
    executor = _make_executor()
    state = _state_with_elements(
        [
            {
                "text": "OCR Word",
                "bounds": [520, 1420, 560, 1460],
                "class": None,
                "resource_id": None,
                "is_ocr": True,
            }
        ]
    )
    semantics = executor._target_semantics("long_press", {"target": [500, 600]}, state)
    assert semantics["target_label_source"] == "ocr"
    assert semantics["target_text"] == "OCR Word"
