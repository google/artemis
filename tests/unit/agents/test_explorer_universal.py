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

"""Unit tests for Universal Explorer and Multi-Model Grounding Engine."""

import json
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from langchain_core.messages import AIMessage

from artemis.agents.explorer.explorer import (
    UNIVERSAL_EXPLORER_TOOLS,
    Explorer,
)
from artemis.config import settings
from artemis.context import ArtemisContext
from artemis.graph.state import State


@pytest.fixture
def mock_context(tmp_path):
    ctx = Mock(spec=ArtemisContext)
    ctx.llm_config = Mock()
    mock_llm_cfg = Mock()
    mock_llm_cfg.model = "claude-3-7-sonnet"
    mock_llm_cfg.temperature = 0.1
    ctx.llm_config.explorer = mock_llm_cfg

    ctx.device = Mock()
    ctx.device.device_width = 1080
    ctx.device.device_height = 2400

    ctx.agent_config = Mock()
    ctx.agent_config.denylisted_tools = {}

    ctx.data_engine = Mock()
    ctx.data_engine.base_dir = tmp_path
    return ctx


def test_explorer_universal_engine_detection(mock_context):
    """Test that Explorer routes to Universal Engine when non-Gemini model is configured."""
    with patch.object(settings, "GOOGLE_API_KEY", None):
        agent = Explorer(mock_context)
        assert agent.use_native_gemini is False
        assert agent.model_name == "claude-3-7-sonnet"


def test_universal_explorer_tool_schemas():
    """Verify schema integrity of Universal Explorer tool declarations."""
    assert len(UNIVERSAL_EXPLORER_TOOLS) == 6
    names = [t["function"]["name"] for t in UNIVERSAL_EXPLORER_TOOLS]
    assert "ask_perception_tool" in names
    assert "detect_objects" in names
    assert "get_ocr_list" in names
    assert "ask_image_processor" in names
    assert "inspect_region" in names
    assert "submit_answer" in names


@pytest.mark.asyncio
async def test_run_universal_explorer_success(mock_context, tmp_path):
    """Test universal Explorer tool invocation and submit_answer coordinate parsing."""
    agent = Explorer(mock_context)
    agent.use_native_gemini = False

    fake_screenshot = tmp_path / "screenshot.jpg"
    fake_screenshot.write_bytes(b"fake_image_bytes")

    # Mock tool call response for submit_answer
    msg_response = AIMessage(
        content="Submitting grounded elements.",
        tool_calls=[
            {
                "name": "submit_answer",
                "args": {
                    "candidates": [
                        {
                            "label": "D1",
                            "coords": [500, 600],
                            "description": "Settings button",
                        }
                    ],
                    "fallback_message": "",
                },
                "id": "exp_call_1",
            }
        ],
    )

    mock_llm = MagicMock()
    mock_bound = MagicMock()
    mock_bound.ainvoke = AsyncMock(return_value=msg_response)
    mock_llm.bind_tools.return_value = mock_bound

    with patch(
        "artemis.agents.explorer.explorer.get_llm",
        return_value=mock_llm,
    ):
        mock_state = Mock(spec=State)
        res_str = await agent._run_universal(
            query="find settings icon",
            context_feedback="",
            screenshot_path=str(fake_screenshot),
            state=mock_state,
            minimal_list="",
            prompt_template="You are an explorer.",
            max_turns=3,
        )

        res = json.loads(res_str)
        assert len(res["candidates"]) == 1
        assert res["candidates"][0]["label"] == "D1"
        assert res["candidates"][0]["coords"] == [500, 600]
