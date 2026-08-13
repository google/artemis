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

from artemis.tools.image_processor_tool import get_ask_image_processor_tool
import pytest


@pytest.mark.asyncio
async def test_ask_image_processor_tool(artemis_context, mock_state):
    """Tests the ask_image_processor tool as a blackbox.

    Allows actual side effects and full lifecycle execution (including real LLM
    API calls). Uses shared pytest fixtures for realistic context.
    """
    tool = get_ask_image_processor_tool(artemis_context)

    # Passing a simple visual reasoning instruction.
    # The tool will use target_image_id="img_0" corresponding to the artifact in mock_state.
    result = await tool.ainvoke(
        {
            "instruction": "Identify the primary colors present in this image.",
            "target_image_id": "img_0",
            "state": mock_state,
        }
    )

    # Assert execution without errors and produces expected output structure
    assert result is not None
    assert isinstance(result, dict)
    assert "outputs" in result
    assert "summary" in result
    assert isinstance(result["outputs"], list)
