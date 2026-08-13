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

import asyncio
import json
import logging
from pathlib import Path
import sys
from unittest.mock import AsyncMock, MagicMock, patch
from artemis.agents.explorer.explorer import Explorer
from tests.integration.agents.explorer.test_explorer_all_tools.helpers import (
    create_mock_context,
    create_mock_state,
    get_or_create_test_screenshot,
)
from PIL import Image
import pytest


def _verify_tool_responses(contents: list, logger: logging.Logger):
    """Parses the multi-turn conversational history payload and maps tool outputs."""
    logger.info("=== VERIFYING TOOL RESPONSES IN CONVERSATION HISTORY ===")
    tool_responses = {}
    for content in contents:
        for part in content.parts:
            if part.function_response:
                name = part.function_response.name
                response_data = part.function_response.response

                # Extract result text
                tool_text = None
                if hasattr(response_data, "get"):
                    tool_text = response_data.get("result")
                elif hasattr(response_data, "fields"):
                    tool_text = response_data.fields.get("result")
                else:
                    tool_text = getattr(response_data, "result", None)

                if name not in tool_responses:
                    tool_responses[name] = []
                tool_responses[name].append({"text": tool_text, "has_image": False})
                logger.info(f"Found tool response for '{name}':\n{tool_text}")

            if part.inline_data and part.inline_data.mime_type == "image/jpeg":
                if tool_responses:
                    last_tool = list(tool_responses.keys())[-1]
                    tool_responses[last_tool][-1]["has_image"] = True
                    tool_responses[last_tool][-1]["img_bytes"] = part.inline_data.data
                    logger.info(
                        f"  -> Attached {len(part.inline_data.data)} bytes of"
                        f" JPEG inline visual content to '{last_tool}'"
                    )

    return tool_responses


@pytest.mark.asyncio
async def test_explorer_ask_image_processor_tool():
    """Verify that the Explorer ReAct loop correctly dispatches, runs, processes, and

    handles the output image/transform details for the 'ask_image_processor'
    tool.
    The test ends immediately after the Vision Coder responds (in the second
    turn).
    """
    test_name = sys._getframe().f_code.co_name

    # Set up standard log file saved on the same level as test file
    log_file = Path(__file__).parent / f"log_{test_name}.log"
    logger = logging.getLogger(test_name)
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    logger.addHandler(handler)

    # Attach the main agent logger to capture internal step logs
    agent_logger = logging.getLogger("artemis.agents.explorer.explorer")
    agent_logger.setLevel(logging.INFO)
    agent_logger.addHandler(handler)

    logger.info(f"Starting test: {test_name}")

    # Define outputs directory and redirect settings to ensure only the latest run is saved
    outputs_dir = Path(__file__).resolve().parent / "outputs"
    if not outputs_dir.exists():
        outputs_dir.mkdir(parents=True, exist_ok=True)

    from artemis.config import settings

    settings.TRACES_PATH = outputs_dir

    mock_ctx = create_mock_context()
    mock_state = create_mock_state()

    logger.info(
        f"Mock context: device={mock_ctx.device.device_width}x{mock_ctx.device.device_height}"
    )

    # Load and verify screenshot
    input_screenshot = get_or_create_test_screenshot()
    logger.info(f"Visual screenshot loaded: {input_screenshot}")

    # =========================================================================
    # 1. Mock LLM Multi-Turn Responses (ask_image_processor -> submit_answer)
    # =========================================================================

    # Turn 1: call ask_image_processor
    mock_fc_coder = MagicMock()
    mock_fc_coder.name = "ask_image_processor"
    mock_fc_coder.args = {
        "instruction": (
            "Crop the top-right area, convert to HSV, resize, then convert to grayscale."
        ),
        "target_image_id": "img_0",
    }
    mock_resp_coder = MagicMock()
    mock_resp_coder.function_calls = [mock_fc_coder]

    # Turn 2: call submit_answer to complete loop immediately after vision coder responds
    mock_fc_submit = MagicMock()
    mock_fc_submit.name = "submit_answer"
    mock_fc_submit.args = {
        "candidates": [],
        "fallback_message": ("Successfully processed the image via Vision Coder."),
    }
    mock_resp_submit = MagicMock()
    mock_resp_submit.function_calls = [mock_fc_submit]

    mock_client = MagicMock()
    mock_client.aio.models.generate_content = AsyncMock(
        side_effect=[mock_resp_coder, mock_resp_submit]
    )

    mock_ctx._genai_client = mock_client
    mock_ctx.llm_config.explorer = None

    # =========================================================================
    # 2. Mock ImageProcessor agent
    # =========================================================================

    # Create the mock output image path
    mock_new_image_path = outputs_dir / f"mock_image_processor_output_{test_name}.jpg"
    # Create a small dummy image of size 100x100 using PIL so that cv2.imread inside explorer reads these dimensions
    img = Image.new("RGB", (100, 100), color="red")
    img.save(mock_new_image_path)

    # Also write a transform JSON file for the new image
    transform_path = Path(str(mock_new_image_path) + ".transform.json")
    mock_transform_data = {
        "offset_x": 108.0,
        "offset_y": 240.0,
        "scale_x": 0.5,
        "scale_y": 0.5,
    }
    with open(transform_path, "w") as f:
        json.dump(mock_transform_data, f)

    # We mock ImageProcessor class itself
    mock_image_processor_instance = MagicMock()
    mock_image_processor_instance.run = AsyncMock(
        return_value={
            "outputs": [
                {
                    "image_id": "img_1",
                    "path": str(mock_new_image_path),
                    "transform": {
                        "offset_x": 108.0,
                        "offset_y": 240.0,
                        "scale_x": 0.5,
                        "scale_y": 0.5,
                    },
                    "annotations": {"V1": [50, 100], "V2": [180, 240]},
                }
            ],
            "summary": "Cropped and highlighted the red button",
        }
    )

    logger.info("Starting Explorer.run execution loop with ask_image_processor...")

    with (
        patch(
            "artemis.agents.explorer.explorer.genai.Client",
            return_value=mock_client,
        ),
        patch(
            "artemis.agents.image_processor.image_processor.ImageProcessor",
            return_value=mock_image_processor_instance,
        ),
    ):
        explorer = Explorer(mock_ctx)

        result = await explorer.run(
            query=("Click on the red button in the top-right corner of the screen."),
            context_feedback="",
            screenshot_path=str(input_screenshot),
            state=mock_state,
            version="ultra",
        )

        # Verify initial image pool has img_0
        assert "img_0" in explorer.image_pool
        assert explorer.image_pool["img_0"]["path"] == str(input_screenshot)

        # Verify ReAct loop went through exactly 2 turns
        assert mock_client.aio.models.generate_content.call_count == 2
        logger.info("Successfully completed both ReAct turns.")

        # Verify ImageProcessor run was called with correct parameters
        mock_image_processor_instance.run.assert_called_once_with(
            "Crop the top-right area, convert to HSV, resize, then convert to grayscale.",
            str(input_screenshot),
        )
        logger.info("Verified ImageProcessor was invoked with correct parameters.")

        # Verify new image is registered in explorer's image_pool
        assert "img_1" in explorer.image_pool
        img_1_info = explorer.image_pool["img_1"]
        assert img_1_info["path"] == str(mock_new_image_path)

        # Verify transform calculations:
        # Since parent (img_0) transform is {"offset_x": 0.0, "offset_y": 0.0, "scale_x": 1.0, "scale_y": 1.0}
        # and child transform is {"offset_x": 108.0, "offset_y": 240.0, "scale_x": 0.5, "scale_y": 0.5}
        assert img_1_info["transform"]["scale_x"] == 0.5
        assert img_1_info["transform"]["scale_y"] == 0.5
        assert img_1_info["transform"]["offset_x"] == 108.0
        assert img_1_info["transform"]["offset_y"] == 240.0
        assert "Cropped and highlighted the red button" in img_1_info["description"]
        logger.info("Verified image_pool registrations and coordinate transforms.")

        # Retrieve final submission outcome
        outcome_data = json.loads(result)
        assert (
            outcome_data["fallback_message"] == "Successfully processed the image via Vision Coder."
        )
        logger.info("Submission response verified successfully.")

        # Extract all tool outputs from history to assert correctness
        call_args_list = mock_client.aio.models.generate_content.call_args_list
        # The second call receives the accumulated tool history
        contents = call_args_list[1].kwargs.get("contents") or call_args_list[1].args[0]

        tool_responses = _verify_tool_responses(contents, logger)

        # Assertions for ask_image_processor tool response (Turn 1 output)
        assert "ask_image_processor" in tool_responses
        coder_resp = tool_responses["ask_image_processor"][0]
        assert "Image Processor completed successfully" in coder_resp["text"]
        assert "img_1" in coder_resp["text"]
        assert "Annotations (wrt original screenshot):" in coder_resp["text"]
        assert "img_1:" in coder_resp["text"]
        assert "- [V1]: [208, 440]" in coder_resp["text"]
        assert "- [V2]: [468, 720]" in coder_resp["text"]
        assert coder_resp["has_image"]
        assert len(coder_resp["img_bytes"]) > 0
        logger.info(
            "Verified tool response contains the response text, annotations"
            " list, and the attached image bytes."
        )

    # Clean up dummy files (Commented out temporarily so you can see the images!)
    # if mock_new_image_path.exists():
    #     mock_new_image_path.unlink()
    # if transform_path.exists():
    #     transform_path.unlink()
    # if input_screenshot.exists():
    #     input_screenshot.unlink()

    # Clean up logging handlers
    agent_logger.removeHandler(handler)
    logger.removeHandler(handler)
    handler.close()


if __name__ == "__main__":
    asyncio.run(test_explorer_ask_image_processor_tool())


def test_cv_canvas_annotations(tmp_path):
    import numpy as np
    import cv2
    from artemis.utils.cv_canvas import ImageCanvas

    # Create a dummy image
    img_dir = tmp_path / "images"
    img_dir.mkdir()
    img_path = img_dir / "img_0.jpg"
    img_data = np.zeros((100, 100, 3), dtype=np.uint8)
    cv2.imwrite(str(img_path), img_data)

    # Initialize transforms registry
    registry_path = img_dir / "intermediate_transforms.json"
    registry_data = {
        "img_0": {
            "image_id": "img_0",
            "path": str(img_path.resolve()),
            "transform": {
                "offset_x": 0.0,
                "offset_y": 0.0,
                "scale_x": 1.0,
                "scale_y": 1.0,
            },
            "annotations": {},
        }
    }
    with open(registry_path, "w") as f:
        json.dump(registry_data, f)

    canvas = ImageCanvas("img_0", str(img_dir))
    assert canvas.annotations == {}

    # Test _get_resolution and initial width/height
    assert canvas._get_resolution() == (100, 100)
    assert canvas.width == 100
    assert canvas.height == 100

    # Test normalized_to_pixel_coords
    assert canvas.normalized_to_pixel_coords(500, 500) == (50, 50)
    assert canvas.normalized_to_pixel_coords(0, 0) == (0, 0)
    assert canvas.normalized_to_pixel_coords(1000, 1000) == (99, 99)

    # Draw dot
    canvas.draw_dot(10, 20, 1)
    assert canvas.annotations == {"V1": [10, 20]}

    # Crop
    canvas.crop(5, 5, 20, 20)
    assert canvas.width == 20
    assert canvas.height == 20
    assert canvas._get_resolution() == (20, 20)
    assert canvas.normalized_to_pixel_coords(500, 500) == (10, 10)
    assert canvas.annotations == {"V1": [5, 15]}

    # Out of bounds test
    canvas.draw_dot(2, 2, 2)
    assert canvas.annotations == {"V1": [5, 15], "V2": [2, 2]}

    # Crop again to discard V1
    canvas.crop(1, 1, 5, 5)
    assert canvas.width == 5
    assert canvas.height == 5
    assert canvas.normalized_to_pixel_coords(500, 500) == (2, 2)
    # V1 was at [5, 15], after crop(1, 1), it becomes [4, 14], which is out of bounds of 5x5.
    # V2 was at [2, 2], after crop(1, 1), it becomes [1, 1], which is inside 5x5.
    assert canvas.annotations == {"V2": [1, 1]}

    # Resize by factor
    canvas.resize_by_factor(2.0)
    assert canvas.width == 10
    assert canvas.height == 10
    assert canvas._get_resolution() == (10, 10)
    assert canvas.normalized_to_pixel_coords(500, 500) == (5, 5)
    assert canvas.annotations == {"V2": [2, 2]}

    # Save and verify registry
    canvas.save(final=True)
    with open(registry_path) as f:
        saved_registry = json.load(f)

    assert "img_1" in saved_registry
    assert saved_registry["img_1"]["annotations"] == {"V2": [2, 2]}
    assert saved_registry["img_1"]["is_output"] is True
