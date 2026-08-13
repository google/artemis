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

import json
from unittest.mock import MagicMock, mock_open, patch

from artemis.agents.outputter.tools import (
    get_search_history_tool,
    get_step_details_tool,
    get_step_screenshot_tool,
)
from artemis.context import ArtemisContext
import pytest


@pytest.fixture
def mock_context():
    ctx = MagicMock(spec=ArtemisContext)
    ctx.data_engine = MagicMock()
    ctx.data_engine.global_base_dir = "/tmp/fake_traces"
    return ctx


def test_get_step_details_tool():
    steps = [
        {
            "step_number": 1,
            "relative_time": "1s",
            "summary": "Step 1",
            "action_taken": "tap",
        },
        {
            "step_number": 2,
            "relative_time": "2s",
            "summary": "Step 2",
            "action_taken": "swipe",
        },
    ]
    tool = get_step_details_tool(steps)

    # Test valid range
    result = tool.invoke({"start_step": 1, "end_step": 1})
    parsed = json.loads(result)
    assert len(parsed) == 1
    assert parsed[0]["step_number"] == 1
    assert parsed[0]["action_taken"] == "tap"

    # Test invalid range
    result_invalid = tool.invoke({"start_step": 3, "end_step": 4})
    assert "No steps found" in result_invalid


def test_get_step_screenshot_tool(mock_context):
    steps = [
        {"step_number": 1, "pre_image_name": "img1", "post_image_name": "img2"},
    ]
    tool = get_step_screenshot_tool(mock_context, steps)

    # Mock open and exists
    with (
        patch("pathlib.Path.exists", return_value=True),
        patch("builtins.open", mock_open(read_data=b"fake_image_bytes")),
    ):
        result = tool.invoke({"step_number": 1})
        assert isinstance(result, list)
        assert result[0]["type"] == "text"
        assert result[1]["type"] == "image_url"
        assert "data:image/jpeg;base64" in result[1]["image_url"]["url"]


def test_search_history_tool(mock_context):
    tool = get_search_history_tool(mock_context)

    # Mock steps returned by storage
    mock_step_1 = MagicMock()
    mock_step_1.step_number = 1
    mock_step_1.pre_image_name = "img1"
    mock_step_1.post_image_name = None

    mock_context.data_engine.storage.get_steps.return_value = [mock_step_1]

    # Mock search_ui_by_hash
    mock_context.data_engine.storage.search_ui_by_hash.return_value = [
        {"type": "ocr", "matched_text": "verification code: 1234", "score": 0.9}
    ]

    result = tool.invoke({"query": "verification"})
    parsed = json.loads(result)
    assert len(parsed) == 1
    assert parsed[0]["step_number"] == 1
    assert parsed[0]["matches"][0]["matched_text"] == "verification code: 1234"
