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

from unittest.mock import AsyncMock, patch
from artemis.agents.object_detector.object_detector import _run_object_detection
import pytest


@pytest.mark.asyncio
async def test_coordinate_normalization_swap():
    """Verify that _run_object_detection correctly swaps coordinates from [y, x] to [x, y]."""

    # 1. Define the mock output returned by the VLM task (VLM native format: [y_norm, x_norm])
    mock_vlm_output = [
        {
            "label": "settings icon",
            "point": [800, 200],  # [y, x] -> y = 800, x = 200
        }
    ]

    # 2. Patch dependencies to run in a pure offline sandbox
    with (
        patch(
            "artemis.agents.object_detector.object_detector._detect_single_label",
            new_callable=AsyncMock,
            return_value=mock_vlm_output,
        ),
        patch("artemis.agents.object_detector.object_detector.get_llm"),
    ):
        # Mock context setup
        mock_ctx = AsyncMock()
        mock_ctx.llm_config.utils.object_detector = None

        # 3. Execute the function under test
        json_output = await _run_object_detection(
            ctx=mock_ctx,
            image_bytes=b"fake_image_bytes",
            queries=["settings icon"],
            templates=["find the {label}"],
        )

        # 4. Verify that the coordinates were successfully swapped
        result = json_output
        detected_items = result["detected"]

        assert len(detected_items) == 1
        assert detected_items[0]["label"] == "settings icon"

        # CRITICAL ASSERTION:
        # The coordinate MUST be swapped from [800, 200] (y, x) to [200, 800] (x, y)
        assert detected_items[0]["point"] == [200, 800]
