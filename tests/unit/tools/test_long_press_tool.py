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
    LongPress,
    LongPressArgs,
    LongPressTool,
    get_long_press_tool,
    long_press,
    long_press_wrapper,
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
    driver.long_press = AsyncMock(return_value=True)
    return driver


@pytest.fixture
def mock_state():
    return MagicMock(spec=State)


def test_long_press_tool_subclass_and_registry():
    """Verify LongPressTool is a subclass of ArtemisTool and properly registered."""
    assert issubclass(LongPressTool, ArtemisTool)
    assert issubclass(LongPress, ArtemisTool)
    assert isinstance(long_press, ArtemisTool)
    assert isinstance(long_press, LongPressTool)

    assert long_press.name == "long_press"
    assert long_press.category == "action"
    assert long_press.args_schema == LongPressArgs

    # Registry lookup
    reg_tool = ToolRegistry.get("long_press")
    assert reg_tool is not None
    assert isinstance(reg_tool, LongPressTool)

    # GenAI FunctionDeclaration export
    declaration = long_press.to_genai_declaration()
    assert declaration.name == "long_press"
    assert "target" in declaration.parameters.properties
    assert "duration" in declaration.parameters.properties

    # Wrapper check
    assert long_press_wrapper is not None
    assert long_press_wrapper.tool_fn_getter == get_long_press_tool


@pytest.mark.asyncio
async def test_long_press_direct_execution_with_driver(mock_driver):
    """Verify direct execution of LongPressTool with BaseDeviceDriver."""
    result = await long_press.execute(driver=mock_driver, target=[500, 500], duration=1500)
    mock_driver.long_press.assert_called_once_with(540, 1200, duration_ms=1500)
    assert "Long pressed at [500, 500] (normalized) successfully." in result


@pytest.mark.asyncio
async def test_long_press_direct_execution_with_ctx(mock_ctx):
    """Verify direct execution of LongPressTool with ArtemisContext."""
    with patch("artemis.tools.mobile.exec_tools.UnifiedMobileController") as mock_controller_cls:
        mock_controller_inst = MagicMock()
        mock_controller_inst.tap_at = AsyncMock(return_value=MagicMock(error=None))
        mock_controller_cls.return_value = mock_controller_inst

        result = await long_press.execute(ctx=mock_ctx, target=[250, 750], duration=2000)
        mock_controller_inst.tap_at.assert_called_once_with(
            270, 1800, long_press=True, long_press_duration=2000
        )
        assert "Long pressed at [250, 750] (normalized) successfully." in result


@pytest.mark.asyncio
async def test_long_press_with_state_command(mock_ctx, mock_state):
    """Verify LongPressTool returns ToolMessage when state is provided."""
    with patch("artemis.tools.mobile.exec_tools.UnifiedMobileController") as mock_controller_cls:
        mock_controller_inst = MagicMock()
        mock_controller_inst.tap_at = AsyncMock(return_value=MagicMock(error=None))
        mock_controller_cls.return_value = mock_controller_inst

        cmd = await long_press.execute(
            ctx=mock_ctx,
            target=[100, 200],
            duration=1200,
            tool_call_id="call_lp_1",
            state=mock_state,
        )
        assert isinstance(cmd, ToolMessage)
        assert cmd.tool_call_id == "call_lp_1"
        assert cmd.status == "success"
        assert "Long pressed at [100, 200]" in cmd.content


@pytest.mark.asyncio
async def test_long_press_callable_execution(mock_driver):
    """Verify invoking long_press directly as a callable."""
    result = await long_press(driver=mock_driver, target=[100, 100])
    assert "Long pressed at [100, 100] (normalized) successfully." in result


@pytest.mark.asyncio
async def test_long_press_invalid_target(mock_ctx):
    """Verify error handling on invalid target coordinates."""
    result = await long_press.execute(ctx=mock_ctx, target=[])
    assert "Error during long press: Invalid target coordinates" in result


@pytest.mark.asyncio
async def test_get_long_press_tool_langchain_ainvoke(mock_ctx):
    """Verify get_long_press_tool exports a LangChain tool that works with ainvoke."""
    lp_tool = get_long_press_tool(mock_ctx)
    assert lp_tool.name == "long_press"

    with patch("artemis.tools.mobile.exec_tools.UnifiedMobileController") as mock_controller_cls:
        mock_controller_inst = MagicMock()
        mock_controller_inst.tap_at = AsyncMock(return_value=MagicMock(error=None))
        mock_controller_cls.return_value = mock_controller_inst

        result = await lp_tool.ainvoke({"target": [500, 500], "duration": 1500})
        assert "Long pressed at [500, 500] (normalized) successfully." in result
