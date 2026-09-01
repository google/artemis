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

from unittest.mock import AsyncMock, MagicMock, patch

from artemis.context import ArtemisContext
from artemis.drivers.base import BaseDeviceDriver
from artemis.graph.state import State
from artemis.tools.base import ArtemisTool, ToolRegistry
from artemis.tools.mobile.exec_tools import (
    Click,
    ClickArgs,
    ClickTool,
    click,
    click_wrapper,
    get_click_tool,
)
from langchain_core.messages import ToolMessage
import pytest


@pytest.fixture
def mock_ctx():
    ctx = MagicMock(spec=ArtemisContext)
    ctx.device = MagicMock()
    ctx.device.device_width = 1080
    ctx.device.device_height = 2400
    return ctx


@pytest.fixture
def mock_driver():
    driver = MagicMock(spec=BaseDeviceDriver)
    driver.screen_size = (1080, 2400)
    driver.tap = AsyncMock(return_value=True)
    return driver


@pytest.fixture
def mock_state():
    return MagicMock(spec=State)


def test_click_tool_subclass_and_registry():
    """Verify ClickTool is a subclass of ArtemisTool and properly registered."""
    assert issubclass(ClickTool, ArtemisTool)
    assert issubclass(Click, ArtemisTool)
    assert isinstance(click, ArtemisTool)
    assert isinstance(click, ClickTool)

    assert click.name == "click"
    assert click.category == "action"
    assert click.args_schema == ClickArgs

    # Registry lookup
    reg_tool = ToolRegistry.get("click")
    assert reg_tool is not None
    assert isinstance(reg_tool, ClickTool)

    # GenAI FunctionDeclaration export
    declaration = click.to_genai_declaration()
    assert declaration.name == "click"
    assert "target" in declaration.parameters.properties

    # Wrapper check
    assert click_wrapper is not None
    assert click_wrapper.tool_fn_getter == get_click_tool


@pytest.mark.asyncio
async def test_click_direct_execution_with_driver(mock_driver):
    """Verify direct execution of ClickTool with BaseDeviceDriver."""
    result = await click.execute(driver=mock_driver, target=[500, 500])
    mock_driver.tap.assert_called_once_with(540, 1200)
    assert "Clicked at [500, 500] (normalized) successfully." in result


@pytest.mark.asyncio
async def test_click_direct_execution_with_ctx(mock_ctx):
    """Verify direct execution of ClickTool with ArtemisContext."""
    with patch("artemis.tools.mobile.exec_tools.UnifiedMobileController") as mock_controller_cls:
        mock_controller_inst = MagicMock()
        mock_controller_inst.tap_at = AsyncMock(return_value=MagicMock(error=None))
        mock_controller_cls.return_value = mock_controller_inst

        result = await click.execute(ctx=mock_ctx, target=[250, 750])
        mock_controller_inst.tap_at.assert_called_once_with(270, 1800)
        assert "Clicked at [250, 750] (normalized) successfully." in result


@pytest.mark.asyncio
async def test_click_with_state_command(mock_ctx, mock_state):
    """Verify ClickTool returns ToolMessage when state is provided."""
    with patch("artemis.tools.mobile.exec_tools.UnifiedMobileController") as mock_controller_cls:
        mock_controller_inst = MagicMock()
        mock_controller_inst.tap_at = AsyncMock(return_value=MagicMock(error=None))
        mock_controller_cls.return_value = mock_controller_inst

        cmd = await click.execute(
            ctx=mock_ctx,
            target=[100, 200],
            tool_call_id="call_click_1",
            state=mock_state,
        )
        assert isinstance(cmd, ToolMessage)
        assert cmd.tool_call_id == "call_click_1"
        assert cmd.status == "success"
        assert "Clicked at [100, 200]" in cmd.content


@pytest.mark.asyncio
async def test_click_callable_execution(mock_driver):
    """Verify invoking click directly as a callable."""
    result = await click(driver=mock_driver, target=[100, 100])
    assert "Clicked at [100, 100] (normalized) successfully." in result


@pytest.mark.asyncio
async def test_click_invalid_target(mock_ctx):
    """Verify error handling on invalid target coordinates."""
    result = await click.execute(ctx=mock_ctx, target=[])
    assert "Error during click: Invalid target coordinates" in result


@pytest.mark.asyncio
async def test_get_click_tool_langchain_ainvoke(mock_ctx):
    """Verify get_click_tool exports a LangChain tool that works with ainvoke."""
    click_tool = get_click_tool(mock_ctx)
    assert click_tool.name == "click"

    with patch("artemis.tools.mobile.exec_tools.UnifiedMobileController") as mock_controller_cls:
        mock_controller_inst = MagicMock()
        mock_controller_inst.tap_at = AsyncMock(return_value=MagicMock(error=None))
        mock_controller_cls.return_value = mock_controller_inst

        result = await click_tool.ainvoke({"target": [500, 500]})
        assert "Clicked at [500, 500] (normalized) successfully." in result
