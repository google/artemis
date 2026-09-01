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
    PressKey,
    PressKeyArgs,
    PressKeyTool,
    get_press_key_tool,
    press_key,
    press_key_wrapper,
)
from langchain_core.messages import ToolMessage
import pytest


@pytest.fixture
def mock_ctx():
    ctx = MagicMock(spec=ArtemisContext)
    ctx.device = MagicMock()
    return ctx


@pytest.fixture
def mock_driver():
    driver = MagicMock(spec=BaseDeviceDriver)
    driver.press_key = AsyncMock(return_value=True)
    return driver


@pytest.fixture
def mock_state():
    return MagicMock(spec=State)


def test_press_key_tool_subclass_and_registry():
    """Verify PressKeyTool is a subclass of ArtemisTool and properly registered."""
    assert issubclass(PressKeyTool, ArtemisTool)
    assert issubclass(PressKey, ArtemisTool)
    assert isinstance(press_key, ArtemisTool)
    assert isinstance(press_key, PressKeyTool)

    assert press_key.name == "press_key"
    assert press_key.category == "action"
    assert press_key.args_schema == PressKeyArgs

    # Registry lookup
    reg_tool = ToolRegistry.get("press_key")
    assert reg_tool is not None
    assert isinstance(reg_tool, PressKeyTool)

    # GenAI FunctionDeclaration export
    declaration = press_key.to_genai_declaration()
    assert declaration.name == "press_key"
    assert "key" in declaration.parameters.properties

    # Wrapper check
    assert press_key_wrapper is not None
    assert press_key_wrapper.tool_fn_getter == get_press_key_tool


@pytest.mark.asyncio
async def test_press_key_direct_execution_with_driver(mock_driver):
    """Verify direct execution with BaseDeviceDriver."""
    result = await press_key.execute(driver=mock_driver, key="BACK")
    mock_driver.press_key.assert_called_once_with("BACK")
    assert "Pressed key 'BACK' successfully." in result


@pytest.mark.asyncio
async def test_press_key_direct_execution_with_ctx(mock_ctx):
    """Verify direct execution with ArtemisContext for various keys."""
    with patch("artemis.tools.mobile.exec_tools.UnifiedMobileController") as mock_controller_cls:
        mock_controller_inst = MagicMock()
        mock_controller_inst.press_enter = AsyncMock(return_value=True)
        mock_controller_inst.go_back = AsyncMock(return_value=True)
        mock_controller_inst.go_home = AsyncMock(return_value=True)
        mock_controller_inst.press_key = AsyncMock(return_value=True)
        mock_controller_cls.return_value = mock_controller_inst

        # Test ENTER
        res_enter = await press_key.execute(ctx=mock_ctx, key="ENTER")
        mock_controller_inst.press_enter.assert_called_once()
        assert "Pressed key 'ENTER' successfully." in res_enter

        # Test BACK
        res_back = await press_key.execute(ctx=mock_ctx, key="BACK")
        mock_controller_inst.go_back.assert_called_once()
        assert "Pressed key 'BACK' successfully." in res_back

        # Test HOME
        res_home = await press_key.execute(ctx=mock_ctx, key="HOME")
        mock_controller_inst.go_home.assert_called_once()
        assert "Pressed key 'HOME' successfully." in res_home

        # Test APP_SWITCH
        res_app_switch = await press_key.execute(ctx=mock_ctx, key="APP_SWITCH")
        mock_controller_inst.press_key.assert_called_with("KEYCODE_APP_SWITCH")
        assert "Pressed key 'APP_SWITCH' successfully." in res_app_switch


@pytest.mark.asyncio
async def test_press_key_with_state_command(mock_ctx, mock_state):
    """Verify PressKeyTool returns ToolMessage when state is provided."""
    with patch("artemis.tools.mobile.exec_tools.UnifiedMobileController") as mock_controller_cls:
        mock_controller_inst = MagicMock()
        mock_controller_inst.go_home = AsyncMock(return_value=True)
        mock_controller_cls.return_value = mock_controller_inst

        cmd = await press_key.execute(
            ctx=mock_ctx,
            key="HOME",
            tool_call_id="call_pk_1",
            state=mock_state,
        )
        assert isinstance(cmd, ToolMessage)
        assert cmd.tool_call_id == "call_pk_1"
        assert cmd.status == "success"
        assert "Pressed key 'HOME' successfully." in cmd.content


@pytest.mark.asyncio
async def test_press_key_callable_execution(mock_driver):
    """Verify invoking press_key directly as a callable."""
    result = await press_key(driver=mock_driver, key="ENTER")
    assert "Pressed key 'ENTER' successfully." in result


@pytest.mark.asyncio
async def test_press_key_missing_key(mock_ctx):
    """Verify error handling when key is empty or missing."""
    result = await press_key.execute(ctx=mock_ctx, key="")
    assert "Error during press key: Key parameter is required." in result


@pytest.mark.asyncio
async def test_get_press_key_tool_langchain_ainvoke(mock_ctx):
    """Verify get_press_key_tool exports a LangChain tool that works with ainvoke."""
    pk_tool = get_press_key_tool(mock_ctx)
    assert pk_tool.name == "press_key"

    with patch("artemis.tools.mobile.exec_tools.UnifiedMobileController") as mock_controller_cls:
        mock_controller_inst = MagicMock()
        mock_controller_inst.go_back = AsyncMock(return_value=True)
        mock_controller_cls.return_value = mock_controller_inst

        result = await pk_tool.ainvoke({"key": "BACK"})
        assert "Pressed key 'BACK' successfully." in result
