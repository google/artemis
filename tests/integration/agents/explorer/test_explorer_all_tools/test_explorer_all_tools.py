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
import shutil
import sys
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from artemis.agents.explorer.explorer import Explorer
from tests.integration.agents.explorer.test_explorer_all_tools.helpers import (
    create_mock_context,
    create_mock_state,
    get_or_create_test_screenshot,
)
import pytest


def _verify_all_tools_responses(messages: list, logger: logging.Logger):
    """Collects every tool result the loop fed back, plus the images it attached."""
    logger.info("=== VERIFYING TOOL RESPONSES IN CONVERSATION HISTORY ===")
    tool_responses = {}
    for message in messages:
        if isinstance(message, ToolMessage):
            tool_responses.setdefault(message.name, []).append({"text": message.content})
            logger.info(f"Found tool response for '{message.name}':\n{message.content}")
        if isinstance(message, HumanMessage) and isinstance(message.content, list):
            blocks = [b for b in message.content if b.get("type") == "image_url"]
            if blocks and tool_responses:
                last_tool = list(tool_responses.keys())[-1]
                tool_responses[last_tool][-1]["has_image"] = True
                tool_responses[last_tool][-1]["img_bytes"] = blocks[0]["image_url"]["url"]
                logger.info(f"  -> Attached {len(blocks)} image block(s) to '{last_tool}'")

    return tool_responses


@pytest.mark.asyncio
async def test_explorer_all_tools_sequential_mocked():
    """Verify that the Explorer ReAct loop correctly sequences, dispatches, processes, and

    annotates screenshot outputs for all available visual tools:
    1. get_ocr_list
    2. search_xml_ocr (query)
    3. detect_objects
    4. search_xml_ocr (coordinates)
    5. submit_answer
    """
    test_name = sys._getframe().f_code.co_name

    # Set up standard log file saved on the same level as test file
    log_file = Path(__file__).parent / f"log_{test_name}.log"
    logger = logging.getLogger(test_name)
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    logger.addHandler(handler)

    # Attach the main agent logger to capture internal step logs
    agent_logger = logging.getLogger("artemis.agents.explorer")
    agent_logger.setLevel(logging.INFO)
    agent_logger.addHandler(handler)

    logger.info(f"Starting comprehensive test: {test_name}")

    # Define outputs directory and redirect settings to ensure only the latest run is saved
    outputs_dir = Path(__file__).resolve().parent / "outputs"
    if outputs_dir.exists():
        shutil.rmtree(outputs_dir)
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
    # 1. Mock LLM Multi-Turn Responses (Sequentially calling each tool)
    # =========================================================================

    # Turn 1: call get_ocr_list
    turn_ocr = AIMessage(
        content="", tool_calls=[{"name": "get_ocr_list", "args": {}, "id": "call_ocr"}]
    )

    # Turn 2: call ask_perception_tool
    turn_search = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "ask_perception_tool",
                "args": {
                    "search_query": "Settings",
                    "nx": 500,
                    "ny": 500,
                    "detect_queries": ["gear icon"],
                },
                "id": "call_search",
            }
        ],
    )

    # Turn 3: call detect_objects
    turn_detect = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "detect_objects",
                "args": {"queries": ["gear icon"], "target_image_id": "img_0"},
                "id": "call_detect",
            }
        ],
    )

    # Turn 4: call inspect_region
    turn_coords = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "inspect_region",
                "args": {
                    "x_min": 100,
                    "y_min": 100,
                    "x_max": 300,
                    "y_max": 300,
                    "zoom_factor": 2.0,
                },
                "id": "call_coords",
            }
        ],
    )

    # Turn 5: call submit_answer to complete loop
    turn_submit = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "submit_answer",
                "args": {
                    "candidates": [
                        {
                            "label": "T1",
                            "coords": [462, 208],
                            "description": "Dashboard Button",
                        },
                        {
                            "label": "S1",
                            "coords": [462, 208],
                            "description": "Settings Button",
                        },
                        {"label": "D1", "coords": [200, 300], "description": "gear icon"},
                    ],
                    "fallback_message": ("Successfully verified all visual controls sequentially."),
                },
                "id": "call_submit",
            }
        ],
    )

    llm = MagicMock()
    llm.bind_tools.return_value.ainvoke = AsyncMock(
        side_effect=[turn_ocr, turn_search, turn_detect, turn_coords, turn_submit]
    )

    mock_ctx.llm_config.explorer = None

    # =========================================================================
    # 2. Mock Downstream Tool Logic Dependencies
    # =========================================================================

    # Mock SQLite StorageManager for get_ocr_list
    mock_storage = MagicMock()
    mock_record = MagicMock()
    mock_record.ocr_result = [
        {"text": "screen description text", "position": []},
        {
            "text": "Dashboard Button",
            "position": [
                {"x": 400, "y": 400},
                {"x": 600, "y": 400},
                {"x": 600, "y": 600},
                {"x": 400, "y": 600},
            ],
        },
    ]
    mock_storage.get_image.return_value = mock_record

    # The UI tree of the record feeds the in-memory ScreenIndex that backs the
    # text search and the coordinate audit (no Data Engine query per tool).
    mock_record.ui_tree = [
        {
            "text": "Settings Button",
            "bounds": "[400,400][600,600]",
            "class": "android.widget.Button",
            "clickable": "true",
        }
    ]

    # Mock downstream object detection pipeline (_run_object_detection)
    mock_detect = AsyncMock(
        return_value={"detected": [{"label": "gear icon", "point": [200, 300]}]}
    )

    logger.info("Starting Explorer.run execution loop with sequential tool mocks...")

    with (
        patch(
            "artemis.agents.explorer.universal_runner.get_llm",
            return_value=llm,
        ),
        patch(
            "artemis.agents.explorer.run_setup.StorageManager",
            return_value=mock_storage,
        ),
        # get_ocr_list is only exposed when an OCR backend is configured.
        patch("artemis.agents.explorer.explorer.is_ocr_configured", return_value=True),
        patch("artemis.agents.explorer.run_setup.is_ocr_configured", return_value=True),
        patch("artemis.agents.explorer.perception_tools._run_object_detection", mock_detect),
    ):
        explorer = Explorer(mock_ctx)
        result = await explorer.run(
            query="Test all tools",
            context_feedback="",
            screenshot_path=str(input_screenshot),
            state=mock_state,
            version="ultra",
        )

        # Verify the loop went through all 5 turns
        assert llm.bind_tools.return_value.ainvoke.await_count == 5
        logger.info("Successfully completed all 5 ReAct turns.")

        # Retrieve final submission outcome
        outcome_data = json.loads(result)
        assert "candidates" in outcome_data
        assert (
            outcome_data["fallback_message"]
            == "Successfully verified all visual controls sequentially."
        )
        logger.info("Submission response verified successfully.")

        # Extract all tool outputs from history to assert correctness
        call_args_list = llm.bind_tools.return_value.ainvoke.call_args_list
        # The fifth call receives the accumulated tool history
        tool_responses = _verify_all_tools_responses(call_args_list[4].args[0], logger)

        # ASSERTIONS FOR EACH TOOL INDIVIDUALLY

        # 1. Verify get_ocr_list
        assert "[O" in tool_responses["get_ocr_list"][0]["text"]
        assert "[462,208]" in tool_responses["get_ocr_list"][0]["text"]
        assert tool_responses["get_ocr_list"][0]["has_image"]

        # 2. Verify ask_perception_tool
        assert "Text Search Results are" in tool_responses["ask_perception_tool"][0]["text"]
        assert tool_responses["ask_perception_tool"][0]["has_image"]

        # 3. Verify detect_objects
        assert "[D" in tool_responses["detect_objects"][0]["text"]
        assert tool_responses["detect_objects"][0]["has_image"]

        # 4. Verify inspect_region
        assert tool_responses["inspect_region"][0]["has_image"]
        # The coordinate audit at normalized [500, 500] (pixel [540, 1200]) is
        # answered from the in-memory index: the UI-tree button covers that point.
        assert "Settings Button" in tool_responses["ask_perception_tool"][0]["text"]

        logger.info(
            "All assertions passed! The entire suite of explorer tools is fully functional."
        )

    # Clean up logging handlers
    agent_logger.removeHandler(handler)
    logger.removeHandler(handler)
    handler.close()


if __name__ == "__main__":
    asyncio.run(test_explorer_all_tools_sequential_mocked())
