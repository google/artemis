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
from unittest.mock import AsyncMock, MagicMock, patch

from artemis.agents.explorer.explorer import Explorer
from tests.integration.agents.explorer.test_explorer_all_tools.helpers import (
    create_mock_context,
    create_mock_state,
)
from tests.integration.agents.explorer.test_explorer_all_tools.seed_mock_image import (
    seed_database,
)

# Set up logging to stdout
logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("live_tools_test")


def extract_and_print_tool_results(contents, set_idx, logger):
    logger.info("\n======================================================================")
    logger.info(f"  RESULTS FOR INPUT TEST SET {set_idx}")
    logger.info("======================================================================")

    for content in contents:
        if content.role != "tool":
            continue
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

                logger.info(f"\n🛠️ Tool: '{name}'")
                logger.info("--------------------------------------------------")
                logger.info(f"Output Result:\n{tool_text}")

            if part.inline_data and part.inline_data.mime_type == "image/jpeg":
                logger.info(
                    f"📷 Visual Data: Attached {len(part.inline_data.data)}"
                    " bytes of annotated JPEG screenshot"
                )


async def run_single_set(set_idx, query, search_query, labels, coords_nx, coords_ny, candidates):
    import os

    os.environ["GOOGLE_API_KEY"] = "dummy_api_key"

    mock_ctx = create_mock_context()
    mock_ctx.llm_config.utils = MagicMock()
    mock_ctx.llm_config.utils.object_detector = MagicMock()
    mock_ctx.llm_config.utils.object_detector.model = "gemini-3.5-flash"
    mock_ctx.llm_config.utils.object_detector.timeout = 10.0

    mock_state = create_mock_state()

    screenshot_path = (
        Path(__file__).resolve().parent
        / "input_screenshot_test_explorer_all_tools_sequential_mocked.jpg"
    )

    # Multi-turn sequential mock client
    # Turn 1: get_ocr_list
    mock_fc_ocr = MagicMock()
    mock_fc_ocr.name = "get_ocr_list"
    mock_fc_ocr.args = {}
    mock_resp_ocr = MagicMock()
    mock_resp_ocr.function_calls = [mock_fc_ocr]

    # Turn 2: search_xml_ocr with query
    mock_fc_search = MagicMock()
    mock_fc_search.name = "search_xml_ocr"
    mock_fc_search.args = {"query": search_query}
    mock_resp_search = MagicMock()
    mock_resp_search.function_calls = [mock_fc_search]

    # Turn 3: detect_objects
    mock_fc_detect = MagicMock()
    mock_fc_detect.name = "detect_objects"
    mock_fc_detect.args = {"labels": labels, "target_image_id": "img_0"}
    mock_resp_detect = MagicMock()
    mock_resp_detect.function_calls = [mock_fc_detect]

    # Turn 4: search_xml_ocr with coordinates
    mock_fc_coords = MagicMock()
    mock_fc_coords.name = "search_xml_ocr"
    mock_fc_coords.args = {"nx": coords_nx, "ny": coords_ny}
    mock_resp_coords = MagicMock()
    mock_resp_coords.function_calls = [mock_fc_coords]

    # Turn 5: submit_answer
    mock_fc_submit = MagicMock()
    mock_fc_submit.name = "submit_answer"
    mock_fc_submit.args = {
        "candidates": candidates,
        "fallback_message": f"Successfully verified test set {set_idx}",
    }
    mock_resp_submit = MagicMock()
    mock_resp_submit.function_calls = [mock_fc_submit]

    mock_client = MagicMock()

    # Mock client.aio.models.generate_content side effects for ReAct turns
    mock_client.aio.models.generate_content = AsyncMock(
        side_effect=[
            mock_resp_ocr,
            mock_resp_search,
            mock_resp_detect,
            mock_resp_coords,
            mock_resp_submit,
        ]
    )

    # Mock the internal model call for _detect_single_label inside detect_objects
    mock_detect_response = MagicMock()
    y_norm, x_norm = 0, 0
    if "gear icon" in labels:
        y_norm, x_norm = 300, 200
    elif "dashboard icon" in labels:
        y_norm, x_norm = 500, 500
    elif "magnifying glass icon" in labels:
        y_norm, x_norm = 140, 875
    elif "avatar icon" in labels:
        y_norm, x_norm = 825, 375
    elif "arrow back icon" in labels:
        y_norm, x_norm = 75, 100

    mock_detect_response.text = json.dumps([{"point": [y_norm, x_norm]}])

    mock_detector_client = MagicMock()
    mock_detector_client.aio.models.generate_content = AsyncMock(return_value=mock_detect_response)

    with (
        patch(
            "google.genai.Client",
            side_effect=[mock_client, mock_detector_client],
        ),
    ):
        explorer = Explorer(mock_ctx)
        await explorer.run(
            query=query,
            context_feedback="",
            screenshot_path=str(screenshot_path),
            state=mock_state,
        )

        call_args_list = mock_client.aio.models.generate_content.call_args_list
        contents = call_args_list[4].kwargs.get("contents") or call_args_list[4].args[0]
        extract_and_print_tool_results(contents, set_idx, logger)


async def main():
    import shutil
    from artemis.config import settings

    # Define outputs directory
    outputs_dir = Path(__file__).resolve().parent / "outputs"
    if outputs_dir.exists():
        shutil.rmtree(outputs_dir)
    outputs_dir.mkdir(parents=True, exist_ok=True)

    # Redirect traces output to outputs/ and only keep the latest run
    settings.TRACES_PATH = outputs_dir

    # 1. Seed the SQLite DB record first
    seed_database()

    # 2. Define the 5 groups of input tests
    test_sets = [
        {
            "query": "Verify Settings button",
            "search_query": "Settings",
            "labels": ["gear icon"],
            "coords_nx": (185),  # Matches pixel center [200, 225] on [1080, 2400] scale
            "coords_ny": 93,
            "candidates": [
                {
                    "label": "S1",
                    "coords": [185, 93],
                    "description": "Settings Button",
                }
            ],
        },
        {
            "query": "Verify Dashboard button",
            "search_query": "Dashboard",
            "labels": ["dashboard icon"],
            "coords_nx": (462),  # Matches pixel center [500, 500] on [1080, 2400] scale
            "coords_ny": 208,
            "candidates": [
                {
                    "label": "S1",
                    "coords": [462, 208],
                    "description": "Dashboard Button",
                }
            ],
        },
        {
            "query": "Verify Search bar",
            "search_query": "Search",
            "labels": ["magnifying glass icon"],
            "coords_nx": (810),  # Matches pixel center [875, 140] on [1080, 2400] scale
            "coords_ny": 58,
            "candidates": [
                {
                    "label": "S1",
                    "coords": [810, 58],
                    "description": "Search bar",
                }
            ],
        },
        {
            "query": "Verify Profile icon",
            "search_query": "Profile",
            "labels": ["avatar icon"],
            "coords_nx": (347),  # Matches pixel center [375, 825] on [1080, 2400] scale
            "coords_ny": 343,
            "candidates": [
                {
                    "label": "S1",
                    "coords": [347, 343],
                    "description": "Profile icon",
                }
            ],
        },
        {
            "query": "Verify Back button",
            "search_query": "Back",
            "labels": ["arrow back icon"],
            "coords_nx": (92),  # Matches pixel center [100, 75] on [1080, 2400] scale
            "coords_ny": 31,
            "candidates": [
                {
                    "label": "S1",
                    "coords": [92, 31],
                    "description": "Back button",
                }
            ],
        },
    ]

    # 3. Run all 5 sets sequentially
    for i, test in enumerate(test_sets, 1):
        await run_single_set(
            set_idx=i,
            query=test["query"],
            search_query=test["search_query"],
            labels=test["labels"],
            coords_nx=test["coords_nx"],
            coords_ny=test["coords_ny"],
            candidates=test["candidates"],
        )


if __name__ == "__main__":
    asyncio.run(main())
