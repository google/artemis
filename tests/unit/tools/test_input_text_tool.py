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

from artemis.constants import VALIDATOR_MESSAGES_KEY
from artemis.context import ArtemisContext
from artemis.drivers.base import BaseDeviceDriver
from artemis.graph.state import State
from artemis.tools.base import ArtemisTool, ToolRegistry
from artemis.tools.mobile.exec_tools import (
    InputText,
    InputTextArgs,
    InputTextTool,
    get_input_text_tool,
    input_text,
    input_text_wrapper,
)
from langgraph.types import Command
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
    driver.input_text = AsyncMock(return_value=True)
    return driver


@pytest.fixture
def mock_state():
    state = MagicMock(spec=State)

    async def _mock_asanitize_update(ctx, update, agent):
        return update

    state.asanitize_update = AsyncMock(side_effect=_mock_asanitize_update)
    return state


def test_input_text_tool_subclass_and_registry():
    """Verify InputTextTool is a subclass of ArtemisTool and properly registered."""
    assert issubclass(InputTextTool, ArtemisTool)
    assert issubclass(InputText, ArtemisTool)
    assert isinstance(input_text, ArtemisTool)
    assert isinstance(input_text, InputTextTool)

    assert input_text.name == "input_text"
    assert input_text.category == "action"
    assert input_text.args_schema == InputTextArgs

    # Registry lookup
    reg_tool = ToolRegistry.get("input_text")
    assert reg_tool is not None
    assert isinstance(reg_tool, InputTextTool)

    # GenAI FunctionDeclaration export
    declaration = input_text.to_genai_declaration()
    assert declaration.name == "input_text"
    assert "text" in declaration.parameters.properties
    assert "target" in declaration.parameters.properties
    assert "clear_exist" in declaration.parameters.properties

    # Wrapper check
    assert input_text_wrapper is not None
    assert input_text_wrapper.tool_fn_getter == get_input_text_tool


@pytest.mark.asyncio
async def test_input_text_direct_execution_with_driver(mock_driver):
    """Verify direct execution of InputTextTool with BaseDeviceDriver."""
    result = await input_text.execute(
        driver=mock_driver, text="hello world", target=[500, 500], clear_exist=True
    )
    mock_driver.tap.assert_called_once_with(540, 1200)
    mock_driver.input_text.assert_called_once_with("hello world", clear_existing=True)
    assert "Typed 'hello world' successfully." in result


@pytest.mark.asyncio
async def test_input_text_direct_execution_with_ctx(mock_ctx):
    """Verify direct execution of InputTextTool with ArtemisContext."""
    with patch("artemis.tools.mobile.exec_tools.UnifiedMobileController") as mock_controller_cls:
        mock_controller_inst = MagicMock()
        mock_controller_inst.tap_at = AsyncMock(return_value=MagicMock(error=None))
        mock_controller_inst.erase_text = AsyncMock(return_value=True)
        mock_controller_inst.type_text = AsyncMock(return_value=True)
        mock_controller_cls.return_value = mock_controller_inst

        result = await input_text.execute(
            ctx=mock_ctx, text="test input", target=[250, 750], clear_exist=True
        )
        mock_controller_inst.tap_at.assert_called_once_with(270, 1800)
        mock_controller_inst.erase_text.assert_called_once()
        mock_controller_inst.type_text.assert_called_once_with("test input", clear_existing=False)
        assert "Typed 'test input' successfully." in result


@pytest.mark.asyncio
async def test_input_text_with_state_command(mock_ctx, mock_state):
    """Verify InputTextTool returns Command when state is provided."""
    with patch("artemis.tools.mobile.exec_tools.UnifiedMobileController") as mock_controller_cls:
        mock_controller_inst = MagicMock()
        mock_controller_inst.tap_at = AsyncMock(return_value=MagicMock(error=None))
        mock_controller_inst.erase_text = AsyncMock(return_value=True)
        mock_controller_inst.type_text = AsyncMock(return_value=True)
        mock_controller_cls.return_value = mock_controller_inst

        cmd = await input_text.execute(
            ctx=mock_ctx,
            text="query",
            target=[100, 200],
            tool_call_id="call_it_1",
            state=mock_state,
        )
        assert isinstance(cmd, Command)
        assert VALIDATOR_MESSAGES_KEY in cmd.update
        messages = cmd.update[VALIDATOR_MESSAGES_KEY]
        assert len(messages) == 1
        assert messages[0].tool_call_id == "call_it_1"
        assert messages[0].status == "success"
        assert "Typed 'query'" in messages[0].content


@pytest.mark.asyncio
async def test_input_text_callable_execution(mock_driver):
    """Verify invoking input_text directly as a callable."""
    result = await input_text(driver=mock_driver, text="call text", target=[100, 100])
    assert "Typed 'call text' successfully." in result


@pytest.mark.asyncio
async def test_input_text_invalid_target(mock_ctx):
    """Verify error handling on invalid target coordinates."""
    result = await input_text.execute(ctx=mock_ctx, text="bad", target=[])
    assert "Error during input text: Invalid target coordinates" in result


@pytest.mark.asyncio
async def test_get_input_text_tool_langchain_ainvoke(mock_ctx):
    """Verify get_input_text_tool exports a LangChain tool that works with ainvoke."""
    it_tool = get_input_text_tool(mock_ctx)
    assert it_tool.name == "input_text"

    with patch("artemis.tools.mobile.exec_tools.UnifiedMobileController") as mock_controller_cls:
        mock_controller_inst = MagicMock()
        mock_controller_inst.tap_at = AsyncMock(return_value=MagicMock(error=None))
        mock_controller_inst.press_key = AsyncMock(return_value=True)
        mock_controller_inst.type_text = AsyncMock(return_value=True)
        mock_controller_cls.return_value = mock_controller_inst

        result = await it_tool.ainvoke(
            {
                "text": "abc",
                "target": [500, 500],
                "clear_exist": False,
            }
        )
        mock_controller_inst.press_key.assert_called_once_with("123")
        assert "Typed 'abc' successfully." in result
