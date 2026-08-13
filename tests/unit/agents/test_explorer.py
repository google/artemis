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

import os

os.environ["ARTEMIS_USE_FILE_API"] = "false"
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import json
from google.genai import types

from artemis.agents.explorer.explorer import Explorer
from artemis.context import ArtemisContext
from artemis.graph.state import State

MOCK_PROMPT_JSON = """{
  "IDENTITY": "You are the agentic UI Explorer designed to identify objects, scan text, and extract UI attributes on a phone screen.",
  "OPERATING PRINCIPLES": [
    "Confidence-Driven Cost-Benefit Strategy: Maximize localization accuracy...",
    "Tool Capabilities: You have three classes of tools...",
    "Parallel vs Sequential Invocations: You are fully supported..."
  ]
}"""


@pytest.fixture(autouse=True)
def disable_file_api(monkeypatch):
    monkeypatch.setenv("ARTEMIS_USE_FILE_API", "false")


@pytest.mark.asyncio
async def test_explorer_run():
    # Mock context and state
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.device = MagicMock()
    mock_ctx.device.device_width = 1080
    mock_ctx.device.device_height = 2400
    mock_ctx.data_engine = MagicMock()
    mock_ctx.data_engine.base_dir = "/tmp/test_session"
    mock_ctx.llm_config = MagicMock()
    mock_ctx.llm_config.explorer = MagicMock()
    mock_ctx.llm_config.explorer.model = "gemini-3.6-flash"

    mock_state = MagicMock(spec=State)
    mock_state.latest_screenshot = "/tmp/test_screenshot.jpg"

    # Mock Gemini Client and Response
    mock_client = MagicMock()
    mock_client.aio = MagicMock()
    mock_client.aio.models = MagicMock()
    mock_response = MagicMock()
    mock_response.text = ""
    mock_func_call = MagicMock()
    mock_func_call.name = "submit_answer"
    mock_func_call.args = {
        "candidates": [
            {
                "label": "S1",
                "coords": [500, 500],
                "description": "Target button",
            }
        ],
        "fallback_message": "",
    }
    mock_response.function_calls = [mock_func_call]

    mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)

    # Mock storage manager
    mock_storage = MagicMock()
    mock_storage.get_image.return_value = "dummy_hash"

    # Mock file reading for prompt
    mock_prompt = MOCK_PROMPT_JSON

    # Custom mock open to handle both binary and text reads
    def custom_open(file, mode="r", *args, **kwargs):
        mock_file = MagicMock()
        if "b" in mode:
            # Provide a sequence of bytes for iter(lambda: f.read(4096), b"") and b64 encode
            mock_file.read.side_effect = [
                b"fake_image_data",
                b"",
                b"fake_image_data",
            ]
        else:
            mock_file.read.return_value = '{"templates": [], "instructions": ""}'

        mock_file.__enter__.return_value = mock_file
        return mock_file

    with (
        patch(
            "artemis.agents.explorer.explorer.genai.Client",
            return_value=mock_client,
        ),
        patch(
            "artemis.agents.explorer.explorer.StorageManager",
            return_value=mock_storage,
        ),
        patch("pathlib.Path.exists", return_value=True),
        patch("pathlib.Path.read_text", return_value=mock_prompt),
        patch("builtins.open", custom_open),
    ):
        explorer = Explorer(mock_ctx)
        result = await explorer.run(
            query="Find button",
            context_feedback="",
            screenshot_path="/tmp/test_screenshot.jpg",
            minimal_list="",
            state=mock_state,
        )

        expected_outcome = json.dumps(mock_func_call.args, ensure_ascii=False)
        assert result == expected_outcome
        assert mock_client.aio.models.generate_content.called


@pytest.mark.asyncio
async def test_explorer_submit_answer():
    # Mock context and state
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.device = MagicMock()
    mock_ctx.device.device_width = 1080
    mock_ctx.device.device_height = 2400
    mock_ctx.data_engine = MagicMock()
    mock_ctx.data_engine.base_dir = "/tmp/test_session"
    mock_ctx.llm_config = MagicMock()
    mock_ctx.llm_config.explorer = MagicMock()
    mock_ctx.llm_config.explorer.model = "gemini-3.6-flash"

    mock_state = MagicMock(spec=State)
    mock_state.latest_screenshot = "/tmp/test_screenshot.jpg"

    # Mock Gemini Client and Function Call for submit_answer
    mock_client = MagicMock()
    mock_client.aio = MagicMock()
    mock_client.aio.models = MagicMock()
    mock_response = MagicMock()
    mock_response.text = ""

    mock_func_call = MagicMock()
    mock_func_call.name = "submit_answer"
    mock_func_call.args = {
        "candidates": [
            {
                "label": "S1",
                "coords": [500, 500],
                "description": "Target button",
            }
        ],
        "fallback_message": "",
    }
    mock_response.function_calls = [mock_func_call]

    mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)

    # Mock storage manager
    mock_storage = MagicMock()
    mock_storage.get_image.return_value = "dummy_hash"

    # Mock file reading for prompt
    mock_prompt = MOCK_PROMPT_JSON

    def custom_open(file, mode="r", *args, **kwargs):
        mock_file = MagicMock()
        if "b" in mode:
            mock_file.read.side_effect = [
                b"fake_image_data",
                b"",
                b"fake_image_data",
            ]
        else:
            mock_file.read.return_value = '{"templates": [], "instructions": ""}'

        mock_file.__enter__.return_value = mock_file
        return mock_file

    with (
        patch(
            "artemis.agents.explorer.explorer.genai.Client",
            return_value=mock_client,
        ),
        patch(
            "artemis.agents.explorer.explorer.StorageManager",
            return_value=mock_storage,
        ),
        patch("pathlib.Path.exists", return_value=True),
        patch("pathlib.Path.read_text", return_value=mock_prompt),
        patch("builtins.open", custom_open),
    ):
        explorer = Explorer(mock_ctx)
        result = await explorer.run(
            query="Find button",
            context_feedback="",
            screenshot_path="/tmp/test_screenshot.jpg",
            minimal_list="",
            state=mock_state,
        )

        # Ensure it returned the structured JSON outcome
        expected_outcome = json.dumps(mock_func_call.args, ensure_ascii=False)
        assert result == expected_outcome
        assert mock_client.aio.models.generate_content.called


@pytest.mark.asyncio
async def test_explorer_submit_answer_self_correction():
    # Mock context and state
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.device = MagicMock()
    mock_ctx.device.device_width = 1080
    mock_ctx.device.device_height = 2400
    mock_ctx.data_engine = MagicMock()
    mock_ctx.data_engine.base_dir = "/tmp/test_session"
    mock_ctx.llm_config = MagicMock()
    mock_ctx.llm_config.explorer = MagicMock()
    mock_ctx.llm_config.explorer.model = "gemini-3.6-flash"

    mock_state = MagicMock(spec=State)
    mock_state.latest_screenshot = "/tmp/test_screenshot.jpg"

    # Response 1: Returns invalid coords [1500, -200] (out of bounds)
    mock_response1 = MagicMock()
    mock_response1.text = ""

    mock_func_call1 = MagicMock()
    mock_func_call1.name = "submit_answer"
    mock_func_call1.args = {
        "candidates": [
            {
                "label": "S1",
                "coords": [1500, -200],
                "description": "Ambiguous button",
            }
        ]
    }

    mock_response1.candidates = [
        MagicMock()
    ]  # Provide dummy candidate content for loop history tracking
    mock_response1.candidates[0].content = types.Content(
        role="model",
        parts=[
            types.Part.from_function_call(
                name="submit_answer",
                args={
                    "candidates": [
                        {
                            "label": "S1",
                            "coords": [1500, -200],
                            "description": "Ambiguous button",
                        }
                    ]
                },
            )
        ],
    )
    mock_response1.function_calls = [mock_func_call1]

    # Response 2: Returns corrected valid coords [500, 500]
    mock_response2 = MagicMock()
    mock_response2.text = ""

    mock_func_call2 = MagicMock()
    mock_func_call2.name = "submit_answer"
    mock_func_call2.args = {
        "candidates": [
            {
                "label": "S1",
                "coords": [500, 500],
                "description": "Corrected button",
            }
        ]
    }

    mock_response2.candidates = [MagicMock()]
    mock_response2.candidates[0].content = types.Content(
        role="model",
        parts=[
            types.Part.from_function_call(
                name="submit_answer",
                args={
                    "candidates": [
                        {
                            "label": "S1",
                            "coords": [500, 500],
                            "description": "Corrected button",
                        }
                    ]
                },
            )
        ],
    )
    mock_response2.function_calls = [mock_func_call2]

    mock_client = MagicMock()
    mock_client.aio = MagicMock()
    mock_client.aio.models = MagicMock()
    mock_client.aio.models.generate_content = AsyncMock(
        side_effect=[mock_response1, mock_response2]
    )

    # Mock storage manager
    mock_storage = MagicMock()
    mock_storage.get_image.return_value = "dummy_hash"

    # Mock file reading for prompt
    mock_prompt = MOCK_PROMPT_JSON

    def custom_open(file, mode="r", *args, **kwargs):
        mock_file = MagicMock()
        if "b" in mode:
            mock_file.read.side_effect = [
                b"fake_image_data",
                b"",
                b"fake_image_data",
            ]
        else:
            mock_file.read.return_value = '{"templates": [], "instructions": ""}'

        mock_file.__enter__.return_value = mock_file
        return mock_file

    with (
        patch(
            "artemis.agents.explorer.explorer.genai.Client",
            return_value=mock_client,
        ),
        patch(
            "artemis.agents.explorer.explorer.StorageManager",
            return_value=mock_storage,
        ),
        patch("pathlib.Path.exists", return_value=True),
        patch("pathlib.Path.read_text", return_value=mock_prompt),
        patch("builtins.open", custom_open),
    ):
        explorer = Explorer(mock_ctx)
        result = await explorer.run(
            query="Find button",
            context_feedback="",
            screenshot_path="/tmp/test_screenshot.jpg",
            minimal_list="",
            state=mock_state,
        )

        # Assert loops successfully completed on the second corrected turn
        expected_args = {
            "candidates": [
                {
                    "label": "S1",
                    "coords": [500, 500],
                    "description": "Corrected button",
                }
            ]
        }
        assert result == json.dumps(expected_args, ensure_ascii=False)
        assert mock_client.aio.models.generate_content.call_count == 2


@pytest.mark.asyncio
async def test_explorer_initial_visual_marking():
    # Mock context and state with latest_ui_hierarchy
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.device = MagicMock()
    mock_ctx.device.device_width = 1080
    mock_ctx.device.device_height = 2400
    mock_ctx.data_engine = MagicMock()
    mock_ctx.data_engine.base_dir = "/tmp/test_session"
    mock_ctx.llm_config = MagicMock()
    mock_ctx.llm_config.explorer = MagicMock()
    mock_ctx.llm_config.explorer.model = "gemini-3.6-flash"

    mock_state = MagicMock(spec=State)
    mock_state.latest_screenshot = "/tmp/test_screenshot.jpg"
    mock_state.latest_ui_hierarchy = [{"text": "Clickable Button", "bounds": "[100,100][200,200]"}]

    # Mock Gemini Client and response
    mock_client = MagicMock()
    mock_client.aio = MagicMock()
    mock_client.aio.models = MagicMock()
    mock_response = MagicMock()
    mock_response.text = ""

    mock_func_call = MagicMock()
    mock_func_call.name = "submit_answer"
    mock_func_call.args = {
        "candidates": [{"label": "1", "coords": [500, 500], "description": "Visual target"}]
    }
    mock_response.function_calls = [mock_func_call]
    mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)

    # Mock storage manager
    mock_storage = MagicMock()
    mock_storage.get_image.return_value = "dummy_hash"

    # Mock file reading for prompt
    mock_prompt = MOCK_PROMPT_JSON

    def custom_open(file, mode="r", *args, **kwargs):
        mock_file = MagicMock()
        if "b" in mode:
            mock_file.read.side_effect = [
                b"fake_image_marked_data",
                b"",
                b"fake_image_marked_data",
            ]
        else:
            mock_file.read.return_value = '{"templates": [], "instructions": ""}'
        mock_file.__enter__.return_value = mock_file
        return mock_file

    with (
        patch(
            "artemis.agents.explorer.explorer.genai.Client",
            return_value=mock_client,
        ),
        patch(
            "artemis.agents.explorer.explorer.StorageManager",
            return_value=mock_storage,
        ),
        patch("artemis.agents.explorer.explorer.draw_dots") as mock_draw_dots,
        patch("pathlib.Path.exists", return_value=True),
        patch("pathlib.Path.read_text", return_value=mock_prompt),
        patch("builtins.open", custom_open),
    ):
        explorer = Explorer(mock_ctx)
        await explorer.run(
            query="Find button",
            context_feedback="",
            screenshot_path="/tmp/test_screenshot.jpg",
            state=mock_state,
            minimal_list="",
        )

        # Ensure draw_dots was successfully called with calculated center coordinates of bounds [100,100][200,200] -> [150, 150]
        mock_draw_dots.assert_called_once()
        points_arg = mock_draw_dots.call_args[0][1]
        assert points_arg == [[150, 150]]

        labels_arg = mock_draw_dots.call_args[0][2]
        assert labels_arg == ["1"]


@pytest.mark.asyncio
async def test_explorer_initial_visual_marking_previous_screenshot():
    from artemis.data_engine.models import ImageRecord

    # Mock context and state
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.device = MagicMock()
    mock_ctx.device.device_width = 1080
    mock_ctx.device.device_height = 2400
    mock_ctx.data_engine = MagicMock()
    mock_ctx.data_engine.base_dir = "/tmp/test_session"
    mock_ctx.llm_config = MagicMock()
    mock_ctx.llm_config.explorer = MagicMock()
    mock_ctx.llm_config.explorer.model = "gemini-3.6-flash"

    mock_state = MagicMock(spec=State)
    # current screenshot is different
    mock_state.latest_screenshot = "/tmp/current_screenshot.jpg"
    mock_state.latest_ui_hierarchy = [{"text": "Current Button", "bounds": "[300,300][400,400]"}]

    # Mock Gemini Client and response
    mock_client = MagicMock()
    mock_client.aio = MagicMock()
    mock_client.aio.models = MagicMock()
    mock_response = MagicMock()
    mock_response.text = ""

    mock_func_call = MagicMock()
    mock_func_call.name = "submit_answer"
    mock_func_call.args = {
        "candidates": [{"label": "1", "coords": [500, 500], "description": "Visual target"}]
    }
    mock_response.function_calls = [mock_func_call]
    mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)

    # Mock storage manager to return correct previous image record from DB
    mock_storage = MagicMock()

    # Mock ImageRecord with the previous screenshot's UI hierarchy
    mock_record = MagicMock(spec=ImageRecord)
    mock_record.ui_tree = [{"text": "Previous Button", "bounds": "[100,100][200,200]"}]
    mock_storage.get_image.return_value = mock_record

    # Mock file reading for prompt
    mock_prompt = MOCK_PROMPT_JSON

    def custom_open(file, mode="r", *args, **kwargs):
        mock_file = MagicMock()
        if "b" in mode:
            mock_file.read.side_effect = [
                b"fake_image_marked_data",
                b"",
                b"fake_image_marked_data",
            ]
        else:
            mock_file.read.return_value = '{"templates": [], "instructions": ""}'
        mock_file.__enter__.return_value = mock_file
        return mock_file

    with (
        patch(
            "artemis.agents.explorer.explorer.genai.Client",
            return_value=mock_client,
        ),
        patch(
            "artemis.agents.explorer.explorer.StorageManager",
            return_value=mock_storage,
        ),
        patch("artemis.agents.explorer.explorer.draw_dots") as mock_draw_dots,
        patch("pathlib.Path.exists", return_value=True),
        patch("pathlib.Path.read_text", return_value=mock_prompt),
        patch("builtins.open", custom_open),
    ):
        explorer = Explorer(mock_ctx)
        await explorer.run(
            query="Find button",
            context_feedback="",
            # Pass previous screenshot path (different from state.latest_screenshot)
            screenshot_path="/tmp/previous_screenshot.jpg",
            state=mock_state,
            minimal_list="",
        )

        # Ensure draw_dots was called with previous bounds [100,100][200,200] -> [150, 150]
        # instead of current bounds [300,300][400,400] -> [350, 350]
        mock_draw_dots.assert_called_once()
        points_arg = mock_draw_dots.call_args[0][1]
        assert points_arg == [[150, 150]]  # Uses previous screenshot bounds!
        assert points_arg != [[350, 350]]  # Does not use current screenshot bounds!

        labels_arg = mock_draw_dots.call_args[0][2]
        assert labels_arg == ["1"]


@pytest.mark.asyncio
async def test_explorer_initial_visual_marking_previous_screenshot_no_ui_tree():

    # Mock context and state
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.device = MagicMock()
    mock_ctx.device.device_width = 1080
    mock_ctx.device.device_height = 2400
    mock_ctx.data_engine = MagicMock()
    mock_ctx.data_engine.base_dir = "/tmp/test_session"
    mock_ctx.llm_config = MagicMock()
    mock_ctx.llm_config.explorer = MagicMock()
    mock_ctx.llm_config.explorer.model = "gemini-3.6-flash"

    mock_state = MagicMock(spec=State)
    mock_state.latest_screenshot = "/tmp/current_screenshot.jpg"
    mock_state.latest_ui_hierarchy = [{"text": "Current Button", "bounds": "[300,300][400,400]"}]

    # Mock Gemini Client and response
    mock_client = MagicMock()
    mock_client.aio = MagicMock()
    mock_client.aio.models = MagicMock()
    mock_response = MagicMock()
    mock_response.text = ""

    mock_func_call = MagicMock()
    mock_func_call.name = "submit_answer"
    mock_func_call.args = {
        "candidates": [{"label": "1", "coords": [500, 500], "description": "Visual target"}]
    }
    mock_response.function_calls = [mock_func_call]
    mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)

    # Mock storage manager to return None (meaning not found in DB)
    mock_storage = MagicMock()
    mock_storage.get_image.return_value = None

    # Mock file reading for prompt
    mock_prompt = MOCK_PROMPT_JSON

    def custom_open(file, mode="r", *args, **kwargs):
        mock_file = MagicMock()
        if "b" in mode:
            mock_file.read.side_effect = [
                b"fake_image_raw_data",
                b"",
                b"fake_image_raw_data",
            ]
        else:
            mock_file.read.return_value = '{"templates": [], "instructions": ""}'
        mock_file.__enter__.return_value = mock_file
        return mock_file

    with (
        patch(
            "artemis.agents.explorer.explorer.genai.Client",
            return_value=mock_client,
        ),
        patch(
            "artemis.agents.explorer.explorer.StorageManager",
            return_value=mock_storage,
        ),
        patch("artemis.agents.explorer.explorer.draw_dots") as mock_draw_dots,
        patch("pathlib.Path.exists", return_value=True),
        patch("pathlib.Path.read_text", return_value=mock_prompt),
        patch("builtins.open", custom_open),
    ):
        explorer = Explorer(mock_ctx)
        await explorer.run(
            query="Find button",
            context_feedback="",
            # Pass previous screenshot path (different from state.latest_screenshot)
            screenshot_path="/tmp/previous_screenshot.jpg",
            state=mock_state,
            minimal_list="",
        )

        # Ensure draw_dots was NEVER called since no ui_tree was available for previous screenshot
        mock_draw_dots.assert_not_called()


@pytest.mark.asyncio
async def test_explorer_initial_visual_marking_previous_screenshot_ocr_fusion():
    from artemis.data_engine.models import ImageRecord

    # Mock context and state
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.device = MagicMock()
    mock_ctx.device.device_width = 1080
    mock_ctx.device.device_height = 2400
    mock_ctx.data_engine = MagicMock()
    mock_ctx.data_engine.base_dir = "/tmp/test_session"
    mock_ctx.llm_config = MagicMock()
    mock_ctx.llm_config.explorer = MagicMock()
    mock_ctx.llm_config.explorer.model = "gemini-3.6-flash"

    mock_state = MagicMock(spec=State)
    mock_state.latest_screenshot = "/tmp/current_screenshot.jpg"

    # Mock Gemini Client and response
    mock_client = MagicMock()
    mock_client.aio = MagicMock()
    mock_client.aio.models = MagicMock()
    mock_response = MagicMock()
    mock_response.text = ""

    mock_func_call = MagicMock()
    mock_func_call.name = "submit_answer"
    mock_func_call.args = {
        "candidates": [{"label": "1", "coords": [500, 500], "description": "Visual target"}]
    }
    mock_response.function_calls = [mock_func_call]
    mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)

    # Mock storage manager
    mock_storage = MagicMock()

    # Mock ImageRecord with previous raw XML and raw OCR
    mock_record = MagicMock(spec=ImageRecord)
    mock_record.ui_tree = [{"text": "", "bounds": "[100,100][200,200]"}]
    # First ocr_result is full screen text and ignored; second overlaps with bounds
    mock_record.ocr_result = [
        {"text": "full screen", "position": [{"x": 0, "y": 0}]},
        {
            "text": "OCR Text",
            "position": [
                {"x": 110, "y": 110},
                {"x": 190, "y": 110},
                {"x": 190, "y": 190},
                {"x": 110, "y": 190},
            ],
        },
    ]
    mock_storage.get_image.return_value = mock_record

    # Mock file reading for prompt
    mock_prompt = MOCK_PROMPT_JSON

    def custom_open(file, mode="r", *args, **kwargs):
        mock_file = MagicMock()
        if "b" in mode:
            mock_file.read.side_effect = [
                b"fake_image_marked_data",
                b"",
                b"fake_image_marked_data",
            ]
        else:
            mock_file.read.return_value = '{"templates": [], "instructions": ""}'
        mock_file.__enter__.return_value = mock_file
        return mock_file

    with (
        patch(
            "artemis.agents.explorer.explorer.genai.Client",
            return_value=mock_client,
        ),
        patch(
            "artemis.agents.explorer.explorer.StorageManager",
            return_value=mock_storage,
        ),
        patch("artemis.agents.explorer.explorer.draw_dots") as mock_draw_dots,
        patch("pathlib.Path.exists", return_value=True),
        patch("pathlib.Path.read_text", return_value=mock_prompt),
        patch("builtins.open", custom_open),
    ):
        explorer = Explorer(mock_ctx)
        await explorer.run(
            query="Find button",
            context_feedback="",
            screenshot_path="/tmp/previous_screenshot.jpg",
            state=mock_state,
            minimal_list="",
        )

        # Ensure draw_dots was called with center coordinates of the fused OCR element [110,110][190,190] -> [150, 150]
        mock_draw_dots.assert_called_once()
        points_arg = mock_draw_dots.call_args[0][1]
        assert points_arg == [[150, 150]]  # Successfully fused and drawn!

        labels_arg = mock_draw_dots.call_args[0][2]
        assert labels_arg == ["1"]


@pytest.mark.asyncio
async def test_explorer_initial_visual_marking_previous_screenshot_on_the_fly_ocr():
    from artemis.data_engine.models import ImageRecord

    # Mock context and state
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.device = MagicMock()
    mock_ctx.device.device_width = 1080
    mock_ctx.device.device_height = 2400
    mock_ctx.data_engine = MagicMock()
    mock_ctx.data_engine.base_dir = "/tmp/test_session"
    mock_ctx.llm_config = MagicMock()
    mock_ctx.llm_config.explorer = MagicMock()
    mock_ctx.llm_config.explorer.model = "gemini-3.6-flash"

    mock_state = MagicMock(spec=State)
    mock_state.latest_screenshot = "/tmp/current_screenshot.jpg"

    # Mock Gemini Client and response
    mock_client = MagicMock()
    mock_client.aio = MagicMock()
    mock_client.aio.models = MagicMock()
    mock_response = MagicMock()
    mock_response.text = ""

    mock_func_call = MagicMock()
    mock_func_call.name = "submit_answer"
    mock_func_call.args = {
        "candidates": [{"label": "1", "coords": [500, 500], "description": "Visual target"}]
    }
    mock_response.function_calls = [mock_func_call]
    mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)

    # Mock storage manager
    mock_storage = MagicMock()

    # Mock ImageRecord containing ui_tree but ocr_result is None
    mock_record = MagicMock(spec=ImageRecord)
    mock_record.ui_tree = [{"text": "", "bounds": "[100,100][200,200]"}]
    mock_record.ocr_result = None
    mock_storage.get_image.return_value = mock_record

    # Mock file reading for prompt
    mock_prompt = MOCK_PROMPT_JSON

    # Mock dynamic OCR result
    mock_ocr_result = [
        {"text": "full screen", "position": [{"x": 0, "y": 0}]},
        {
            "text": "On-the-fly OCR Text",
            "position": [
                {"x": 110, "y": 110},
                {"x": 190, "y": 110},
                {"x": 190, "y": 190},
                {"x": 110, "y": 190},
            ],
        },
    ]

    def custom_open(file, mode="r", *args, **kwargs):
        mock_file = MagicMock()
        if "b" in mode:
            mock_file.read.side_effect = [
                b"fake_image_marked_data",
                b"",
                b"fake_image_marked_data",
            ]
        else:
            mock_file.read.return_value = '{"templates": [], "instructions": ""}'
        mock_file.__enter__.return_value = mock_file
        return mock_file

    with (
        patch(
            "artemis.agents.explorer.explorer.genai.Client",
            return_value=mock_client,
        ),
        patch(
            "artemis.agents.explorer.explorer.StorageManager",
            return_value=mock_storage,
        ),
        patch(
            "artemis.agents.explorer.explorer.perform_ocr",
            new_callable=AsyncMock,
            return_value=mock_ocr_result,
        ) as mock_perform_ocr,
        patch("artemis.agents.explorer.explorer.draw_dots") as mock_draw_dots,
        patch("pathlib.Path.exists", return_value=True),
        patch("pathlib.Path.read_text", return_value=mock_prompt),
        patch("builtins.open", custom_open),
    ):
        explorer = Explorer(mock_ctx)
        await explorer.run(
            query="Find button",
            context_feedback="",
            screenshot_path="/tmp/previous_screenshot.jpg",
            state=mock_state,
            minimal_list="",
        )

        # Verify perform_ocr was triggered on-the-fly
        mock_perform_ocr.assert_called_once()

        # Ensure draw_dots was called with center coordinates of the fused OCR element [110,110][190,190] -> [150, 150]
        mock_draw_dots.assert_called_once()
        points_arg = mock_draw_dots.call_args[0][1]
        assert points_arg == [[150, 150]]

        labels_arg = mock_draw_dots.call_args[0][2]
        assert labels_arg == ["1"]


@pytest.mark.asyncio
async def test_explorer_blacklisted_tool():
    # Mock context and state
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.device = MagicMock()
    mock_ctx.device.device_width = 1080
    mock_ctx.device.device_height = 2400
    mock_ctx.data_engine = MagicMock()
    mock_ctx.data_engine.base_dir = "/tmp/test_session"
    mock_ctx.llm_config = MagicMock()
    mock_ctx.llm_config.explorer = MagicMock()
    mock_ctx.llm_config.explorer.model = "gemini-3.6-flash"
    mock_ctx.agent_config = MagicMock()
    mock_ctx.agent_config.blacklisted_tools = {"explorer": ["search_xml_ocr"]}

    mock_state = MagicMock(spec=State)
    mock_state.latest_screenshot = "/tmp/test_screenshot.jpg"

    # Response 1: Model calls search_xml_ocr (which is blacklisted)
    mock_response1 = MagicMock()
    mock_response1.text = ""
    mock_func_call1 = MagicMock()
    mock_func_call1.name = "search_xml_ocr"
    mock_func_call1.args = {"use_coords": False, "search_value": "test"}
    mock_response1.function_calls = [mock_func_call1]
    mock_response1.candidates = [MagicMock()]
    mock_response1.candidates[0].content = types.Content(
        role="model",
        parts=[types.Part.from_function_call(name="search_xml_ocr", args=mock_func_call1.args)],
    )

    # Response 2: Model calls submit_answer
    mock_response2 = MagicMock()
    mock_response2.text = ""
    mock_func_call2 = MagicMock()
    mock_func_call2.name = "submit_answer"
    mock_func_call2.args = {
        "candidates": [
            {
                "label": "S1",
                "coords": [500, 500],
                "description": "Corrected button",
            }
        ],
        "fallback_message": "",
    }
    mock_response2.function_calls = [mock_func_call2]
    mock_response2.candidates = [MagicMock()]
    mock_response2.candidates[0].content = types.Content(
        role="model",
        parts=[types.Part.from_function_call(name="submit_answer", args=mock_func_call2.args)],
    )

    mock_client = MagicMock()
    mock_client.aio = MagicMock()
    mock_client.aio.models = MagicMock()
    mock_client.aio.models.generate_content = AsyncMock(
        side_effect=[mock_response1, mock_response2]
    )

    # Mock storage manager
    mock_storage = MagicMock()
    mock_storage.get_image.return_value = "dummy_hash"

    # Mock file reading for prompt
    mock_prompt = MOCK_PROMPT_JSON

    def custom_open(file, mode="r", *args, **kwargs):
        mock_file = MagicMock()
        if "b" in mode:
            mock_file.read.side_effect = [
                b"fake_image_data",
                b"",
                b"fake_image_data",
            ]
        else:
            mock_file.read.return_value = '{"templates": [], "instructions": ""}'

        mock_file.__enter__.return_value = mock_file
        return mock_file

    with (
        patch(
            "artemis.agents.explorer.explorer.genai.Client",
            return_value=mock_client,
        ),
        patch(
            "artemis.agents.explorer.explorer.StorageManager",
            return_value=mock_storage,
        ),
        patch("pathlib.Path.exists", return_value=True),
        patch("pathlib.Path.read_text", return_value=mock_prompt),
        patch("builtins.open", custom_open),
    ):
        explorer = Explorer(mock_ctx)

        # Verify get_exposed_tools doesn't expose search_xml_ocr
        exposed_tools = explorer.get_exposed_tools()
        exposed_tool_names = [tool.name for tool in exposed_tools]
        assert "search_xml_ocr" not in exposed_tool_names

        result = await explorer.run(
            query="Find button",
            context_feedback="",
            screenshot_path="/tmp/test_screenshot.jpg",
            minimal_list="",
            state=mock_state,
        )

        expected_outcome = json.dumps(mock_func_call2.args, ensure_ascii=False)
        assert result == expected_outcome

        # Ensure we called generate_content twice
        assert mock_client.aio.models.generate_content.call_count == 2

        # Verify the contents passed to the second call contain the error response
        # indicating that the tool is blacklisted and unavailable.
        call_args = mock_client.aio.models.generate_content.call_args_list[1]
        contents = call_args[1]["contents"]

        # Find the tool response in contents
        tool_content = next(
            (
                c
                for c in contents
                if c.role in ("tool", "user") and any(p.function_response for p in c.parts)
            ),
            None,
        )
        assert tool_content is not None

        # Verify that the response part contains the expected error message
        found_error = False
        for part in tool_content.parts:
            func_resp = part.function_response
            if func_resp and func_resp.name == "search_xml_ocr":
                assert "blacklisted and unavailable" in func_resp.response["error"]
                found_error = True

        assert found_error


@pytest.mark.asyncio
async def test_explorer_inspect_region():
    import pathlib
    import numpy as np

    # Mock context
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.device = MagicMock()
    mock_ctx.device.device_width = 1080
    mock_ctx.device.device_height = 2400
    mock_ctx.data_engine = MagicMock()

    explorer = Explorer(mock_ctx)
    explorer.width = 1080
    explorer.height = 2400
    explorer.screenshot_path = "/tmp/test_screenshot.jpg"
    explorer.image_name = "test_image"

    dummy_img = np.zeros((2400, 1080, 3), dtype=np.uint8)

    with (
        patch("cv2.imread", return_value=dummy_img) as mock_imread,
        patch("cv2.resize", return_value=dummy_img) as mock_resize,
        patch("cv2.imwrite", return_value=True) as mock_imwrite,
        patch("pathlib.Path.mkdir") as mock_mkdir,
        patch("glob.glob", return_value=[]),
        patch("artemis.agents.explorer.explorer.settings") as mock_settings,
    ):
        mock_settings.TRACES_PATH = pathlib.Path("/tmp/traces")
        res = await explorer.exec_inspect_region(
            x_min=100, y_min=200, x_max=300, y_max=400, zoom_factor=2.0
        )
        assert res["image_path"] is not None
        assert "Inspected region coordinates" in res["text"]
        mock_imread.assert_called_once_with("/tmp/test_screenshot.jpg")

        # Verify resize calculations:
        # px_start = 100 * 1080 / 1000 = 108
        # py_start = 200 * 2400 / 1000 = 480
        # px_end = 300 * 1080 / 1000 = 324
        # py_end = 400 * 2400 / 1000 = 960
        # width = 324 - 108 = 216
        # height = 960 - 480 = 480
        # new_width = 216 * 2.0 = 432
        # new_height = 480 * 2.0 = 960
        mock_resize.assert_called_once()
        resize_args = mock_resize.call_args[0]
        assert resize_args[1] == (432, 960)
        mock_imwrite.assert_called_once()


@pytest.mark.asyncio
async def test_explorer_final_turn_tool_stripping():
    """Test that on the final iteration (turn 10), Explorer strips other tools and injects warning."""
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.device = MagicMock()
    mock_ctx.device.device_width = 1080
    mock_ctx.device.device_height = 2400
    mock_ctx.data_engine = MagicMock()
    mock_ctx.data_engine.base_dir = "/tmp/test_session"
    explorer = Explorer(mock_ctx)

    mock_ctx.llm_config = MagicMock()
    mock_ctx.llm_config.explorer = MagicMock()
    mock_ctx.llm_config.explorer.model = "gemini-3.6-flash"

    mock_state = MagicMock(spec=State)
    mock_state.latest_screenshot = "/tmp/test_screenshot.jpg"

    # Set up generate_content mock to capture call arguments
    mock_client = MagicMock()
    mock_ctx._genai_client = mock_client

    # Turn 1: model returns search_xml_ocr
    fc_search = MagicMock()
    fc_search.name = "search_xml_ocr"
    fc_search.args = {"use_coords": False, "search_value": "test"}
    mock_response = MagicMock()
    mock_response.function_calls = [fc_search]
    mock_response.candidates = [MagicMock()]
    mock_response.candidates[0].content = types.Content(
        role="model",
        parts=[types.Part.from_function_call(name="search_xml_ocr", args=fc_search.args)],
    )

    # Always return the same mock response to force it to run out of turns
    mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)

    from unittest.mock import mock_open

    with (
        patch("builtins.open", mock_open(read_data=b"")),
        patch("hashlib.sha256") as mock_sha,
        patch("pathlib.Path.exists", return_value=True),
        patch("pathlib.Path.read_text", return_value="{}"),
    ):
        mock_sha.return_value.hexdigest.return_value = "dummy_hash"

        await explorer.run(
            query="test",
            context_feedback="",
            screenshot_path="/tmp/test_screenshot.jpg",
            state=mock_state,
            minimal_list="test list",
        )

        # Total number of iterations is max_iterations = 3 under "pro" mode default
        assert mock_client.aio.models.generate_content.call_count == 3

        # Check the final turn call (index 2) keyword args
        call_args_3 = mock_client.aio.models.generate_content.call_args_list[2]
        config = call_args_3.kwargs["config"]

        # Tools in the 10th call should only contain "submit_answer"
        tools = config.tools[0].function_declarations
        assert len(tools) == 1
        assert tools[0].name == "submit_answer"

        # Warning should be injected in contents
        contents = call_args_3.kwargs["contents"]
        # Find warning in contents parts
        warning_found = False
        for content in contents:
            if content.role == "user":
                for part in content.parts:
                    if part.text and "[WARNING] This is your final iteration" in part.text:
                        warning_found = True
        assert warning_found, "Warning message was not found in contents"


@pytest.mark.asyncio
async def test_explorer_ask_perception_tool():
    import numpy as np
    from unittest.mock import mock_open

    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = MagicMock()
    mock_ctx.data_engine.base_dir = "/tmp/test_session"
    explorer = Explorer(mock_ctx)
    explorer.image_name = "dummy_hash"
    explorer.screenshot_path = "/tmp/test_screenshot.jpg"
    explorer.width = 1080
    explorer.height = 2400
    explorer.global_label_idx = 1
    explorer.image_pool = {
        "img_0": {
            "path": "/tmp/test_screenshot.jpg",
            "transform": {
                "offset_x": 0.0,
                "offset_y": 0.0,
                "scale_x": 1.0,
                "scale_y": 1.0,
            },
            "description": "Original complete screenshot",
        }
    }

    # Mock search_ui_func
    mock_search_ui_res = {
        "matches": [
            {
                "matched_text": "Settings",
                "bounds": [540, 480, 540, 480],
                "type": "xml",
            },
            {
                "matched_text": "Sign In",
                "bounds": [100, 100, 200, 200],
                "type": "ocr",
            },
        ]
    }

    # Mock storage record
    mock_record = MagicMock()
    mock_record.ui_tree = [
        {
            "bounds": "[500,400][600,560]",
            "text": "Settings Button",
            "resource-id": "com.android.settings:id/btn_settings",
            "class": "android.widget.Button",
        }
    ]
    mock_record.ocr_result = []

    mock_storage = MagicMock()
    mock_storage.get_image.return_value = mock_record

    # Mock _run_object_detection
    mock_detector_res = {
        "detected": [{"label": "profile icon", "point": [900, 100]}],
        "failed": [],
    }

    # Mock draw_dots
    mock_draw_dots = MagicMock()

    with (
        patch(
            "artemis.agents.explorer.explorer.search_ui_func",
            return_value=mock_search_ui_res,
        ),
        patch(
            "artemis.agents.explorer.explorer.StorageManager",
            return_value=mock_storage,
        ),
        patch(
            "artemis.mcp.xml_search_server.StorageManager",
            return_value=mock_storage,
        ),
        patch(
            "artemis.agents.explorer.explorer._run_object_detection",
            return_value=mock_detector_res,
        ),
        patch("artemis.agents.explorer.explorer.draw_dots", mock_draw_dots),
        patch("artemis.utils.visualization.draw_dots", mock_draw_dots),
        patch("pathlib.Path.mkdir"),
        patch("glob.glob", return_value=[]),
        patch("builtins.open", mock_open(read_data=b'{"templates": []}')),
        patch("cv2.imread", return_value=np.zeros((2400, 1080, 3), dtype=np.uint8)),
    ):
        result = await explorer.exec_ask_perception_tool(
            search_query="Settings",
            nx=550,
            ny=200,
            detect_queries=["profile icon"],
        )

        assert "XML/OCR Text Search Results are:" in result["text"]
        assert "Coordinate Search Results are:" in result["text"]
        assert "Object Detection Results are:" in result["text"]

        # XML Text search should return label X1
        assert "[X1]" in result["text"]
        # OCR Text search should return label O2
        assert "[O2]" in result["text"]
        # Coordinate search should return label X3
        assert "[X3]" in result["text"]
        # Object detection should return label D4 (since global label idx is shared/contiguous)
        assert "[D4]" in result["text"]

        assert len(result["image_paths"]) == 3

        # Verify call arguments of draw_dots
        assert mock_draw_dots.call_count == 3
        # First call (XML/OCR Text search): green color
        first_call = mock_draw_dots.call_args_list[0]
        assert first_call.kwargs.get("color") == "green"
        assert first_call.args[2] == ["X1", "O2"]

        # Second call (XML Coordinate audit): blue color
        second_call = mock_draw_dots.call_args_list[1]
        assert second_call.kwargs.get("color") == "blue"
        assert second_call.args[2] == ["X3"]

        # Third call (Object detection): red color
        third_call = mock_draw_dots.call_args_list[2]
        assert third_call.kwargs.get("color") == "red"
        assert third_call.args[2] == ["D4"]


def test_explorer_prune_historical_images():
    from artemis.agents.explorer.explorer import Explorer
    from google.genai import types

    mock_ctx = MagicMock()
    explorer = Explorer(mock_ctx)

    part1 = types.Part(inline_data=types.Blob(data=b"img1", mime_type="image/jpeg"))
    part2 = types.Part(inline_data=types.Blob(data=b"img2", mime_type="image/jpeg"))
    part3 = types.Part.from_text(text="Some text")

    contents = [
        types.Content(role="user", parts=[part1, part3]),
        types.Content(role="user", parts=[part2]),
    ]

    explorer._prune_historical_images(contents, keep_last=1)

    # First image part should be pruned to text part
    assert contents[0].parts[0].text == "[Image pruned to maintain visual focus on latest state]"
    assert contents[0].parts[1].text == "Some text"
    # Second (last) image part should be kept intact
    assert contents[1].parts[0] is part2
