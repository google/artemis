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

from artemis.agents.explorer.explorer import Explorer
import pytest


@pytest.fixture
def explorer_instance(artemis_context, mock_state):
    explorer = Explorer(artemis_context)
    explorer.image_name = "test_img"
    explorer.screenshot_path = mock_state.latest_screenshot

    # Initialize the image pool with img_0 required by processing tools
    explorer.image_pool = {
        "img_0": {
            "path": mock_state.latest_screenshot,
            "transform": {
                "offset_x": 0.0,
                "offset_y": 0.0,
                "scale_x": 1.0,
                "scale_y": 1.0,
            },
        }
    }
    return explorer


@pytest.mark.asyncio
async def test_exec_detect_objects(explorer_instance):
    result = await explorer_instance.exec_detect_objects(
        ["button", "text"], target_image_id="img_0"
    )
    assert isinstance(result, dict)
    assert "text" in result
    assert "image_path" in result


@pytest.mark.asyncio
async def test_exec_ask_perception_tool(explorer_instance):
    result = await explorer_instance.exec_ask_perception_tool("login", 500, 500, ["button"])
    assert isinstance(result, dict)
    assert "text" in result
    assert "image_paths" in result


@pytest.mark.asyncio
async def test_exec_get_ocr_list(explorer_instance):
    result = await explorer_instance.exec_get_ocr_list()
    assert isinstance(result, dict)
    assert "text" in result
    assert "image_path" in result


@pytest.mark.asyncio
async def test_exec_inspect_region(explorer_instance):
    # Depending on cv2 logic, it might return an error string if coordinates are out of bounds or image is missing
    # But it shouldn't raise unhandled exceptions
    result = await explorer_instance.exec_inspect_region(100, 100, 400, 400, 2.0)
    assert isinstance(result, dict)
    assert "text" in result
    assert "image_path" in result


@pytest.mark.asyncio
async def test_exec_ask_image_processor(explorer_instance):
    result = await explorer_instance.exec_ask_image_processor(
        "Invert colors", target_image_id="img_0"
    )
    assert isinstance(result, dict)
    assert "text" in result
    assert "image_paths" in result
