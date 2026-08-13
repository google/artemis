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

from unittest.mock import AsyncMock, MagicMock

from artemis.context import ArtemisContext
from artemis.drivers.base import BaseDeviceDriver
from artemis.tools.base import ArtemisTool, ToolRegistry
from artemis.tools.mobile.read_hierarchy import (
    GetUIHierarchy,
    GetUiHierarchy,
    GetUiHierarchyArgs,
    GetUiHierarchyTool,
    get_ui_hierarchy,
    get_ui_hierarchy_tool,
    ui_hierarchy_wrapper,
)
import pytest


@pytest.fixture
def mock_ctx():
    ctx = MagicMock(spec=ArtemisContext)
    ctx.ui_adb_client = MagicMock()
    ctx.ui_adb_client.get_hierarchy.return_value = "<hierarchy><node text='Home'/></hierarchy>"
    return ctx


@pytest.fixture
def mock_driver():
    driver = MagicMock(spec=BaseDeviceDriver)
    screen_data = MagicMock()
    screen_data.ui_hierarchy_xml = "<hierarchy><node text='Settings'/></hierarchy>"
    driver.get_screen_data = AsyncMock(return_value=screen_data)
    return driver


def test_get_ui_hierarchy_tool_subclass_and_registry():
    """Verify GetUiHierarchyTool is a subclass of ArtemisTool and properly registered."""
    assert issubclass(GetUiHierarchyTool, ArtemisTool)
    assert issubclass(GetUiHierarchy, ArtemisTool)
    assert issubclass(GetUIHierarchy, ArtemisTool)
    assert isinstance(get_ui_hierarchy, ArtemisTool)
    assert isinstance(get_ui_hierarchy, GetUiHierarchyTool)

    assert get_ui_hierarchy.name == "get_ui_hierarchy"
    assert get_ui_hierarchy.category == "diagnostic"
    assert get_ui_hierarchy.args_schema == GetUiHierarchyArgs

    # Registry lookup
    reg_tool = ToolRegistry.get("get_ui_hierarchy")
    assert reg_tool is not None
    assert isinstance(reg_tool, GetUiHierarchyTool)

    # GenAI FunctionDeclaration export
    declaration = get_ui_hierarchy.to_genai_declaration()
    assert declaration.name == "get_ui_hierarchy"

    # Wrapper check
    assert ui_hierarchy_wrapper is not None
    assert ui_hierarchy_wrapper.tool_fn_getter == get_ui_hierarchy_tool


@pytest.mark.asyncio
async def test_get_ui_hierarchy_direct_execution_with_ctx(mock_ctx):
    """Verify direct execution with ArtemisContext."""
    result = await get_ui_hierarchy.execute(ctx=mock_ctx)
    mock_ctx.ui_adb_client.get_hierarchy.assert_called_once()
    assert "<hierarchy><node text='Home'/></hierarchy>" in result


@pytest.mark.asyncio
async def test_get_ui_hierarchy_direct_execution_with_driver_screen_data(mock_driver):
    """Verify direct execution with driver's get_screen_data."""
    result = await get_ui_hierarchy.execute(driver=mock_driver)
    mock_driver.get_screen_data.assert_called_once()
    assert "<hierarchy><node text='Settings'/></hierarchy>" in result


@pytest.mark.asyncio
async def test_get_ui_hierarchy_direct_execution_with_driver_get_hierarchy():
    """Verify direct execution with driver's get_ui_hierarchy."""
    driver = MagicMock(spec=BaseDeviceDriver)
    del driver.get_screen_data
    driver.get_ui_hierarchy = AsyncMock(
        return_value="<hierarchy><node text='DirectDriver'/></hierarchy>"
    )
    result = await get_ui_hierarchy.execute(driver=driver)
    driver.get_ui_hierarchy.assert_called_once()
    assert "<hierarchy><node text='DirectDriver'/></hierarchy>" in result


@pytest.mark.asyncio
async def test_get_ui_hierarchy_with_state(mock_ctx):
    """Verify GetUiHierarchyTool returns string when state is provided."""
    state = MagicMock()
    result = await get_ui_hierarchy.execute(
        ctx=mock_ctx,
        tool_call_id="call_hier_1",
        state=state,
    )
    assert "<hierarchy><node text='Home'/></hierarchy>" in result


@pytest.mark.asyncio
async def test_get_ui_hierarchy_no_client_or_driver():
    """Verify error handling when no client or driver is provided."""
    result = await get_ui_hierarchy.execute()
    assert "Error retrieving UI hierarchy: No UI Automator client or driver provided." in result


@pytest.mark.asyncio
async def test_get_ui_hierarchy_exception_handling(mock_ctx):
    """Verify error handling when ui_adb_client raises an exception."""
    mock_ctx.ui_adb_client.get_hierarchy.side_effect = RuntimeError("ADB server died")
    result = await get_ui_hierarchy.execute(ctx=mock_ctx)
    assert "Error retrieving UI hierarchy: ADB server died" in result


@pytest.mark.asyncio
async def test_get_ui_hierarchy_callable_execution(mock_ctx):
    """Verify invoking get_ui_hierarchy directly as a callable."""
    result = await get_ui_hierarchy(ctx=mock_ctx)
    assert "<node text='Home'/>" in result


@pytest.mark.asyncio
async def test_get_ui_hierarchy_tool_langchain_ainvoke(mock_ctx):
    """Verify get_ui_hierarchy_tool exports a LangChain tool that works with ainvoke."""
    tool = get_ui_hierarchy_tool(mock_ctx)
    assert tool.name == "get_ui_hierarchy"

    result = await tool.ainvoke({})
    assert "<node text='Home'/>" in result
