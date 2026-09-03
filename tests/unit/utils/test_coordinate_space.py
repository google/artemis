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

"""Coordinate-space contract of recorded actions.

Pro records pixels (``coordinate_space="pixel"``), Flash records the model's
own 0–1000 values (``coordinate_space="normalized"``); every model-facing
render normalizes, and the explicit marker — never a magnitude heuristic —
is what makes that pass idempotent.
"""

from artemis.utils.coordinates import (
    COORDINATE_SPACE_KEY,
    COORDINATE_SPACE_NORMALIZED,
    COORDINATE_SPACE_PIXEL,
    normalize_action_dict,
    normalize_any_structure,
)

W, H = 1080, 2400


def test_pixel_record_is_normalized_once_and_stamped():
    pixel = {
        "action": "tap",
        "coordinates": [540, 1200],
        "normalized_coordinates": [500, 500],
        COORDINATE_SPACE_KEY: COORDINATE_SPACE_PIXEL,
    }
    once = normalize_action_dict(pixel, W, H)
    assert once["coordinates"] == [500, 500]
    assert once[COORDINATE_SPACE_KEY] == COORDINATE_SPACE_NORMALIZED
    assert pixel["coordinates"] == [540, 1200]  # input untouched

    twice = normalize_action_dict(once, W, H)
    assert twice == once


def test_normalized_record_is_returned_verbatim_even_when_values_look_like_pixels():
    """No magnitude guessing: a Flash record of [320, 399] stays [320, 399]
    (the buggy double pass used to turn it into [296, 166])."""
    flash = {
        "action": "click",
        "coordinates": [320, 399],
        COORDINATE_SPACE_KEY: COORDINATE_SPACE_NORMALIZED,
        "args": {"target": [320, 399]},
    }
    out = normalize_action_dict(flash, W, H)
    assert out == flash
    assert out is not flash

    big = dict(flash, coordinates=[1080, 2400])
    assert normalize_action_dict(big, W, H)["coordinates"] == [1080, 2400]


def test_legacy_unstamped_record_is_converted_once_then_idempotent():
    legacy = {"action": "swipe", "coordinates": [540, 1800, 540, 600]}
    once = normalize_action_dict(legacy, W, H)
    assert once["coordinates"] == [500, 750, 500, 250]
    assert once[COORDINATE_SPACE_KEY] == COORDINATE_SPACE_NORMALIZED
    assert normalize_action_dict(once, W, H) == once

    # A dict without coordinate fields gets no marker (nothing was converted).
    assert COORDINATE_SPACE_KEY not in normalize_action_dict({"action": "press_key"}, W, H)


def test_normalize_any_structure_recurses_into_incident_containers():
    """An execution incident holds an ``action`` *dict* — it is a container,
    not an action item, so its nested action is normalized too."""
    report = {
        "status": "failed",
        "execution": [
            {
                "action": "tap",
                "coordinates": [540, 1200],
                COORDINATE_SPACE_KEY: COORDINATE_SPACE_PIXEL,
                "attempts": ["Error"],
            }
        ],
        "incident": {
            "kind": "exec_error",
            "action": {
                "action": "tap",
                "coordinates": [540, 1200],
                COORDINATE_SPACE_KEY: COORDINATE_SPACE_PIXEL,
            },
        },
    }
    out = normalize_any_structure(report, W, H)
    assert out["execution"][0]["coordinates"] == [500, 500]
    assert out["incident"]["action"]["coordinates"] == [500, 500]
    assert out["incident"]["kind"] == "exec_error"
    assert normalize_any_structure(out, W, H) == out
