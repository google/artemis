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

from artemis.agents.outputter.tools import (
    get_search_history_tool,
    get_step_details_tool,
    get_step_screenshot_tool,
)
from artemis.context import ArtemisContext
import pytest


@pytest.fixture
def history_steps(artemis_context: ArtemisContext):
    if artemis_context.data_engine:
        return artemis_context.data_engine.get_agent_friendly_steps()
    return []


def test_get_step_details_tool(history_steps):
    """Test get_step_details_tool to ensure it runs without errors."""
    tool = get_step_details_tool(history_steps)
    result = tool.invoke({"start_step": 1, "end_step": 5})

    assert isinstance(result, str)
    assert len(result) > 0


def test_get_step_screenshot_tool(artemis_context: ArtemisContext, history_steps):
    """Test get_step_screenshot_tool to ensure it handles requests gracefully."""
    tool = get_step_screenshot_tool(artemis_context, history_steps)

    result = tool.invoke({"step_number": 1})

    if isinstance(result, list):
        assert len(result) == 2
        assert result[0]["type"] == "text"
        assert result[1]["type"] == "image_url"
    else:
        assert isinstance(result, str)
        assert len(result) > 0


def test_get_search_history_tool(artemis_context: ArtemisContext):
    """Test get_search_history_tool to ensure it can search through history."""
    tool = get_search_history_tool(artemis_context)
    result = tool.invoke({"query": "test"})

    assert isinstance(result, str)
    assert len(result) > 0
