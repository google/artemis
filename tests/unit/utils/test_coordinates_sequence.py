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

"""Unit tests for sequence coordinate normalization and overlay visualization."""

import io
from PIL import Image
from artemis.utils.coordinates import normalize_step_actions
from artemis.utils.visualization import draw_action_overlay_on_image


def test_normalize_step_actions_click_sequence_does_not_inherit_single_action():
    """Verify that click_sequence does NOT inherit single coordinates from action_taken."""
    step_dict = {
        "step_id": "step-123",
        "action_taken": [
            {
                "action": "tap",
                "coordinates": [540, 720],
                "normalized_coordinates": [500, 300],
            }
        ],
        "generic_tools": [
            {
                "name": "click_sequence",
                "payload": {
                    "args": {
                        "sequence": "[[500, 300], [876, 360]]",
                        "delay_ms": "50",
                    },
                    "result": {
                        "status": "success",
                        "outcome": "Sequence clicked successfully: Tapped at [540, 720]; Tapped at [946, 864]",
                    },
                },
            }
        ],
        "extra_metadata": {"width": 1080, "height": 2400},
    }

    normalized = normalize_step_actions(step_dict)
    tool = normalized["generic_tools"][0]
    args = tool["payload"]["args"]

    # Coordinates from first_act must NOT be injected into click_sequence
    assert "coordinates" not in args or args.get("coordinates") is None
    assert "normalized_coordinates" not in args or args.get("normalized_coordinates") is None

    # Sequence must be parsed into a list and normalized
    assert args["sequence"] == [[500, 300], [876, 360]]
    assert args["normalized_sequence"] == [[500, 300], [876, 360]]


def test_normalize_step_actions_physical_sequence():
    """Verify that physical coordinates in sequence are converted into normalized_sequence."""
    step_dict = {
        "step_id": "step-456",
        "action_taken": [],
        "generic_tools": [
            {
                "name": "click_sequence",
                "payload": {
                    "args": {
                        "sequence": [[540, 1200], [1080, 2400]],
                    }
                },
            }
        ],
        "extra_metadata": {"width": 1080, "height": 2400},
    }

    normalized = normalize_step_actions(step_dict)
    args = normalized["generic_tools"][0]["payload"]["args"]

    assert args["normalized_sequence"] == [[500, 500], [1000, 1000]]


def test_draw_action_overlay_click_sequence():
    """Verify PIL overlay drawing for click_sequence with stringified sequence."""
    img = Image.new("RGB", (1080, 2400), color="blue")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    raw_bytes = buf.getvalue()

    action_args = {
        "sequence": "[[500, 300], [876, 360]]",
    }

    annotated_bytes = draw_action_overlay_on_image(raw_bytes, "click_sequence", action_args)
    assert annotated_bytes is not None
    assert len(annotated_bytes) > 0

    res_img = Image.open(io.BytesIO(annotated_bytes))
    assert res_img.size == (1080, 2400)
