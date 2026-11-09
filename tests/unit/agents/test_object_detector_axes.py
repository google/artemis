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

"""Point axis handling of the object detector: [x, y] in, [x, y] out."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from artemis.agents.explorer.perception_tools import load_detector_templates
from artemis.agents.object_detector.object_detector import _run_object_detection


async def _detect_with(provider: str | None) -> list[int]:
    llm = MagicMock()
    llm.endpoint = SimpleNamespace(provider=provider) if provider else None
    with (
        patch(
            "artemis.agents.object_detector.object_detector._detect_single_label",
            new_callable=AsyncMock,
            return_value=[{"label": "settings icon", "point": [800, 200]}],
        ),
        patch("artemis.agents.object_detector.object_detector.get_llm", return_value=llm),
    ):
        result = await _run_object_detection(
            ctx=AsyncMock(),
            image_bytes=b"fake_image_bytes",
            queries=["settings icon"],
            templates=["find the {label}"],
        )
    return result["detected"][0]["point"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["google", "openai", None])
async def test_point_is_returned_as_reported(provider):
    assert await _detect_with(provider) == [800, 200]


def test_detector_prompt_asks_for_xy_order():
    templates, _ = load_detector_templates()
    for template in templates:
        assert "[x, y]" in template
        assert "[y, x]" not in template
