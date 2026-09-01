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

"""Unit tests for Universal Tool Protocol and Actions."""

import pytest
from artemis.drivers.mock.mock_driver import MockDeviceDriver
from artemis.tools.base import ToolRegistry
import artemis.tools.mobile.exec_tools  # noqa: F401 - Register action tools


@pytest.mark.asyncio
async def test_tool_registry_registration():
    """Verify standard device actions are registered in ToolRegistry."""
    click_tool = ToolRegistry.get("click")
    assert click_tool is not None
    assert click_tool.name == "click"
    assert click_tool.category == "action"

    swipe_tool = ToolRegistry.get("swipe")
    assert swipe_tool is not None
    assert swipe_tool.name == "swipe"


@pytest.mark.asyncio
async def test_tool_execution_with_driver():
    """Verify tool execution correctly dispatches to MockDeviceDriver."""
    driver = MockDeviceDriver(width=1000, height=2000)

    # Click normalized [500, 500] -> pixel (500, 1000)
    result = await ToolRegistry.execute("click", {"target": [500, 500]}, driver=driver, ctx=None)
    assert "Click" in result or "Clicked" in result
    assert driver.action_history[-1]["action"] == "tap"
    assert driver.action_history[-1]["x"] == 500
    assert driver.action_history[-1]["y"] == 1000

    # Swipe cardinal "up"
    result = await ToolRegistry.execute("swipe", {"action": "up"}, driver=driver, ctx=None)
    assert "Swipe" in result or "Swiped" in result
    assert driver.action_history[-1]["action"] == "swipe_direction"


def test_tool_to_genai_declaration():
    """Verify tools export to Google GenAI FunctionDeclaration properly."""
    declarations = ToolRegistry.get_genai_declarations(["click", "swipe", "input_text"])
    assert len(declarations) == 3

    decl_names = [d.name for d in declarations]
    assert "click" in decl_names
    assert "swipe" in decl_names
    assert "input_text" in decl_names


def test_tool_to_langchain_tool():
    """Verify tools export to LangChain BaseTool."""
    lc_tools = ToolRegistry.get_langchain_tools(ctx=None, names=["click", "press_key"])
    assert len(lc_tools) == 2
    assert lc_tools[0].name == "click"
    assert lc_tools[1].name == "press_key"
