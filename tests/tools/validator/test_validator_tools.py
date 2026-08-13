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

import base64
from pathlib import Path

from artemis.agents.validator.failure_analyzer import ValidationErrorCategory
from artemis.agents.validator.validator import ValidatorNode
import pytest


@pytest.mark.asyncio
async def test_safety_net_validation(artemis_context, mock_state):
    """Test the safety_net_validation tool."""
    node = ValidatorNode(artemis_context)

    action_item = {
        "action": "tap",
        "coordinates": [100, 200],
        "target_text": "Submit",
    }

    # We pass session=None so it relies on fallback or fails gracefully
    # We assert it runs without unhandled exceptions.
    try:
        passed, category, reason = await node._validate_action_precondition(
            session=None, action_item=action_item, state=mock_state
        )
    except Exception as e:
        pytest.fail(f"Tool raised an exception: {e}")

    assert isinstance(passed, bool)
    assert isinstance(category, ValidationErrorCategory)
    assert isinstance(reason, str)


@pytest.mark.asyncio
async def test_safety_net_pixel_validation(artemis_context, mock_state):
    """Test the safety_net_pixel_validation tool."""
    node = ValidatorNode(artemis_context)

    action_item = {"action": "tap", "coordinates": [100, 200]}

    # Read the real screenshot from the mock_state to provide as pre_screenshot_b64
    screenshot_path = mock_state.latest_screenshot
    pre_screenshot_b64 = "dummy_base64"
    if screenshot_path and Path(screenshot_path).exists():
        with open(screenshot_path, "rb") as f:
            pre_screenshot_b64 = base64.b64encode(f.read()).decode("utf-8")

    try:
        passed, category, reason = await node._validate_action_precondition_pixel(
            session=None,
            action_item=action_item,
            pre_screenshot_b64=pre_screenshot_b64,
            original_coords=[100, 200],
            state=mock_state,
        )
    except Exception as e:
        pytest.fail(f"Tool raised an exception: {e}")

    assert isinstance(passed, bool)
    assert isinstance(category, ValidationErrorCategory)
    assert isinstance(reason, str)
