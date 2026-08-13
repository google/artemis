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
    Swipe,
    SwipeArgs,
    SwipeTool,
    get_swipe_tool,
    swipe,
    swipe_wrapper,
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
    driver.swipe_direction = AsyncMock(return_value=True)
    driver.swipe = AsyncMock(return_value=True)
    return driver


@pytest.fixture
def mock_state():
    state = MagicMock(spec=State)

    async def _mock_asanitize_update(ctx, update, agent):
        return update

    state.asanitize_update = AsyncMock(side_effect=_mock_asanitize_update)
    return state


def test_swipe_tool_subclass_and_registry():
    """Verify SwipeTool is a subclass of ArtemisTool and properly registered."""
    assert issubclass(SwipeTool, ArtemisTool)
    assert issubclass(Swipe, ArtemisTool)
    assert isinstance(swipe, ArtemisTool)
    assert isinstance(swipe, SwipeTool)

    assert swipe.name == "swipe"
    assert swipe.category == "action"
    assert swipe.args_schema == SwipeArgs

    # Registry lookup
    reg_tool = ToolRegistry.get("swipe")
    assert reg_tool is not None
    assert isinstance(reg_tool, ArtemisTool)

    # GenAI FunctionDeclaration export
    declaration = swipe.to_genai_declaration()
    assert declaration.name == "swipe"
    assert "action" in declaration.parameters.properties
    assert "duration" in declaration.parameters.properties

    # Wrapper check
    assert swipe_wrapper is not None
    assert swipe_wrapper.tool_fn_getter == get_swipe_tool


@pytest.mark.asyncio
async def test_swipe_direct_execution_with_driver_direction(mock_driver):
    """Verify direct execution with direction on BaseDeviceDriver."""
    result = await swipe.execute(driver=mock_driver, action="up", duration=500)
    mock_driver.swipe_direction.assert_called_once_with("up", duration_ms=500)
    assert "Swipe completed successfully." in result


@pytest.mark.asyncio
async def test_swipe_direct_execution_with_driver_coords(mock_driver):
    """Verify direct execution with coordinates on BaseDeviceDriver."""
    result = await swipe.execute(driver=mock_driver, action=[100, 200, 300, 400], duration=600)
    mock_driver.swipe.assert_called_once_with(108, 480, 324, 960, duration_ms=600)
    assert "Swipe completed successfully." in result


@pytest.mark.asyncio
async def test_swipe_direct_execution_with_ctx_direction(mock_ctx):
    """Verify direct execution with direction on ArtemisContext."""
    with patch("artemis.tools.mobile.exec_tools.UnifiedMobileController") as mock_controller_cls:
        mock_controller_inst = MagicMock()
        mock_controller_inst.swipe_coords = AsyncMock(return_value=None)
        mock_controller_cls.return_value = mock_controller_inst

        result = await swipe.execute(ctx=mock_ctx, action="down", duration=400)
        mock_controller_inst.swipe_coords.assert_called_once_with(540, 480, 540, 1920, 400)
        assert "Swipe completed successfully." in result


@pytest.mark.asyncio
async def test_swipe_direct_execution_with_ctx_coords(mock_ctx):
    """Verify direct execution with coordinates on ArtemisContext."""
    with patch("artemis.tools.mobile.exec_tools.UnifiedMobileController") as mock_controller_cls:
        mock_controller_inst = MagicMock()
        mock_controller_inst.swipe_coords = AsyncMock(return_value=None)
        mock_controller_cls.return_value = mock_controller_inst

        result = await swipe.execute(ctx=mock_ctx, action=[200, 300, 800, 900], duration=800)
        mock_controller_inst.swipe_coords.assert_called_once_with(216, 720, 864, 2160, 800)
        assert "Swipe completed successfully." in result


@pytest.mark.asyncio
async def test_swipe_with_state_command(mock_ctx, mock_state):
    """Verify SwipeTool returns Command when state is provided."""
    with patch("artemis.tools.mobile.exec_tools.UnifiedMobileController") as mock_controller_cls:
        mock_controller_inst = MagicMock()
        mock_controller_inst.swipe_coords = AsyncMock(return_value=None)
        mock_controller_cls.return_value = mock_controller_inst

        cmd = await swipe.execute(
            ctx=mock_ctx,
            action="left",
            tool_call_id="call_sw_1",
            state=mock_state,
        )
        assert isinstance(cmd, Command)
        assert VALIDATOR_MESSAGES_KEY in cmd.update
        messages = cmd.update[VALIDATOR_MESSAGES_KEY]
        assert len(messages) == 1
        assert messages[0].tool_call_id == "call_sw_1"
        assert messages[0].status == "success"
        assert "Swipe completed successfully." in messages[0].content


@pytest.mark.asyncio
async def test_swipe_callable_execution(mock_driver):
    """Verify invoking swipe directly as a callable."""
    result = await swipe(driver=mock_driver, action="right")
    assert "Swipe completed successfully." in result


@pytest.mark.asyncio
async def test_swipe_invalid_action(mock_ctx):
    """Verify error handling on invalid action."""
    with patch("artemis.tools.mobile.exec_tools.UnifiedMobileController"):
        result = await swipe.execute(ctx=mock_ctx, action="diagonal")
        assert "Error during swipe: Invalid" in result


@pytest.mark.asyncio
async def test_get_swipe_tool_langchain_ainvoke(mock_ctx):
    """Verify get_swipe_tool exports a LangChain tool that works with ainvoke."""
    sw_tool = get_swipe_tool(mock_ctx)
    assert sw_tool.name == "swipe"

    with patch("artemis.tools.mobile.exec_tools.UnifiedMobileController") as mock_controller_cls:
        mock_controller_inst = MagicMock()
        mock_controller_inst.swipe_coords = AsyncMock(return_value=None)
        mock_controller_cls.return_value = mock_controller_inst

        result = await sw_tool.ainvoke({"action": "up"})
        assert "Swipe completed successfully." in result


@pytest.mark.asyncio
async def test_swipe_with_direction_and_start_end(mock_driver):
    """Verify swipe works with explicit direction and start/end parameters."""
    # Direction parameter
    result_dir = await swipe.execute(driver=mock_driver, direction="left", duration=500)
    mock_driver.swipe_direction.assert_called_with("left", duration_ms=500)
    assert "Swipe completed successfully." in result_dir

    # Start and End parameters
    result_drag = await swipe.execute(
        driver=mock_driver, start=[100, 200], end=[300, 400], duration=1500
    )
    mock_driver.swipe.assert_called_with(108, 480, 324, 960, duration_ms=1500)
    assert "Swipe completed successfully." in result_drag


@pytest.mark.asyncio
async def test_swipe_with_coordinates_alias(mock_driver):
    """Verify swipe works with coordinates alias parameter."""
    result = await swipe.execute(driver=mock_driver, coordinates=[200, 300, 400, 500], duration=800)
    mock_driver.swipe.assert_called_with(216, 720, 432, 1200, duration_ms=800)
    assert "Swipe completed successfully." in result


@pytest.mark.asyncio
async def test_swipe_with_string_coordinates(mock_driver):
    """Verify swipe works with string-formatted coordinates."""
    # List format in string
    result1 = await swipe.execute(driver=mock_driver, action="[920, 290, 920, 180]", duration=1000)
    mock_driver.swipe.assert_called_with(993, 696, 993, 432, duration_ms=1000)
    assert "Swipe completed successfully." in result1

    # Space-separated format in string
    result2 = await swipe.execute(driver=mock_driver, action="920 290 920 180", duration=1000)
    assert "Swipe completed successfully." in result2


def test_parse_swipe_parameters_direct():
    """Verify parse_swipe_parameters with various parameter formats."""
    from artemis.utils.coordinates import parse_swipe_parameters

    # List
    k, t, d = parse_swipe_parameters([100, 200, 300, 400])
    assert k == "coords"
    assert t == [100, 200, 300, 400]

    # String list
    k, t, d = parse_swipe_parameters("[920, 290, 920, 180]")
    assert k == "coords"
    assert t == [920, 290, 920, 180]

    # String space separated
    k, t, d = parse_swipe_parameters("920 290 920 180")
    assert k == "coords"
    assert t == [920, 290, 920, 180]

    # Dict with action string
    k, t, d = parse_swipe_parameters({"action": "[920, 290, 920, 180]", "duration": 1000})
    assert k == "coords"
    assert t == [920, 290, 920, 180]
    assert d == 1000

    # Dict with start and end
    k, t, d = parse_swipe_parameters({"start": [100, 200], "end": [300, 400]})
    assert k == "coords"
    assert t == [100, 200, 300, 400]

    # Direction string
    k, t, d = parse_swipe_parameters({"action": "up"})
    assert k == "direction"
    assert t == "up"
