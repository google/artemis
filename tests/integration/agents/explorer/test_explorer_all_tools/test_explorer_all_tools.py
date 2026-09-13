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
from artemis.agents.explorer.explorer import Explorer
from tests.integration.agents.explorer.test_explorer_all_tools.helpers import (
    create_mock_context,
    create_mock_state,
    get_or_create_test_screenshot,
)
import pytest


def _verify_all_tools_responses(contents: list, logger: logging.Logger):
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
                # The image byte block corresponds to the preceding function response
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
    mock_fc_ocr = MagicMock()
    mock_fc_ocr.name = "get_ocr_list"
    mock_fc_ocr.args = {}
    mock_resp_ocr = MagicMock()
    mock_resp_ocr.function_calls = [mock_fc_ocr]

    # Turn 2: call ask_perception_tool
    mock_fc_search = MagicMock()
    mock_fc_search.name = "ask_perception_tool"
    mock_fc_search.args = {
        "search_query": "Settings",
        "nx": 500,
        "ny": 500,
        "detect_queries": ["gear icon"],
    }
    mock_resp_search = MagicMock()
    mock_resp_search.function_calls = [mock_fc_search]

    # Turn 3: call detect_objects
    mock_fc_detect = MagicMock()
    mock_fc_detect.name = "detect_objects"
    mock_fc_detect.args = {"queries": ["gear icon"], "target_image_id": "img_0"}
    mock_resp_detect = MagicMock()
    mock_resp_detect.function_calls = [mock_fc_detect]

    # Turn 4: call inspect_region
    mock_fc_coords = MagicMock()
    mock_fc_coords.name = "inspect_region"
    mock_fc_coords.args = {
        "x_min": 100,
        "y_min": 100,
        "x_max": 300,
        "y_max": 300,
        "zoom_factor": 2.0,
    }
    mock_resp_coords = MagicMock()
    mock_resp_coords.function_calls = [mock_fc_coords]

    # Turn 5: call submit_answer to complete loop
    mock_fc_submit = MagicMock()
    mock_fc_submit.name = "submit_answer"
    mock_fc_submit.args = {
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
    }
    mock_resp_submit = MagicMock()
    mock_resp_submit.function_calls = [mock_fc_submit]

    mock_client = MagicMock()
    mock_client.aio.models.generate_content = AsyncMock(
        side_effect=[
            mock_resp_ocr,
            mock_resp_search,
            mock_resp_detect,
            mock_resp_coords,
            mock_resp_submit,
        ]
    )

    mock_ctx._genai_client = mock_client
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
            "artemis.agents.explorer.explorer.genai.Client",
            return_value=mock_client,
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
            enable_caching=False,
        )

        # Verify ReAct loop went through all 5 turns
        assert mock_client.aio.models.generate_content.call_count == 5
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
        call_args_list = mock_client.aio.models.generate_content.call_args_list
        # The fifth call receives the accumulated tool history
        contents = call_args_list[4].kwargs.get("contents") or call_args_list[4].args[0]

        tool_responses = _verify_all_tools_responses(contents, logger)

        # ASSERTIONS FOR EACH TOOL INDIVIDUALLY

        # Verify each tool execution record in explorer.trace_history
        assert len(explorer.trace_history) >= 4
        # 1. Verify get_ocr_list
        ocr_trace = explorer.trace_history[0]["tool_calls"][0]
        assert ocr_trace["name"] == "get_ocr_list"
        assert "[O" in ocr_trace["response"]["text"]
        assert "[462,208]" in ocr_trace["response"]["text"]
        assert ocr_trace["response"]["image_path"] is not None

        # 2. Verify ask_perception_tool
        search_trace = explorer.trace_history[1]["tool_calls"][0]
        assert search_trace["name"] == "ask_perception_tool"
        assert "Text Search Results are" in search_trace["response"]["text"]
        assert len(search_trace["response"]["image_paths"]) > 0

        # 3. Verify detect_objects
        detect_trace = explorer.trace_history[2]["tool_calls"][0]
        assert detect_trace["name"] == "detect_objects"
        assert "[D" in detect_trace["response"]["text"]
        assert detect_trace["response"]["image_path"] is not None

        # 4. Verify inspect_region
        coords_trace = explorer.trace_history[3]["tool_calls"][0]
        assert coords_trace["name"] == "inspect_region"
        assert coords_trace["response"]["image_path"] is not None
        # The coordinate audit at normalized [500, 500] (pixel [540, 1200]) is
        # answered from the in-memory index: the UI-tree button covers that point.
        perception_trace = explorer.trace_history[1]["tool_calls"][0]
        assert perception_trace["name"] == "ask_perception_tool"
        assert "Settings Button" in perception_trace["response"]["text"]

        logger.info(
            "All assertions passed! The entire suite of explorer tools is fully functional."
        )

    # Clean up logging handlers
    agent_logger.removeHandler(handler)
    logger.removeHandler(handler)
    handler.close()


if __name__ == "__main__":
    asyncio.run(test_explorer_all_tools_sequential_mocked())
