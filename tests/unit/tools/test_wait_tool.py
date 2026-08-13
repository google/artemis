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

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

from artemis.context import ArtemisContext
from artemis.tools.base import ArtemisTool, ToolRegistry
from artemis.tools.wait_tool import (
    Wait,
    WaitArgs,
    WaitTool,
    get_wait_tool,
    wait,
    wait_wrapper,
)
import pytest


@pytest.fixture
def mock_ctx():
    ctx = MagicMock(spec=ArtemisContext)
    return ctx


def test_wait_tool_subclass_and_registry():
    """Verify WaitTool is a subclass of ArtemisTool and properly registered."""
    assert issubclass(WaitTool, ArtemisTool)
    assert issubclass(Wait, ArtemisTool)
    assert isinstance(wait, ArtemisTool)
    assert isinstance(wait, WaitTool)

    assert wait.name == "wait"
    assert wait.category == "system"
    assert wait.args_schema == WaitArgs

    # Registry lookup
    reg_tool = ToolRegistry.get("wait")
    assert reg_tool is not None
    assert isinstance(reg_tool, WaitTool)

    # GenAI FunctionDeclaration export
    declaration = wait.to_genai_declaration()
    assert declaration.name == "wait"
    assert "seconds" in declaration.parameters.properties

    # Wrapper check
    assert wait_wrapper is not None
    assert wait_wrapper.tool_fn_getter == get_wait_tool


@pytest.mark.asyncio
async def test_wait_direct_execution(mock_ctx):
    """Verify direct execution of WaitTool.execute."""
    with pytest.MonkeyPatch.context() as mp:
        mock_sleep = AsyncMock()
        mp.setattr(asyncio, "sleep", mock_sleep)

        result = await wait.execute(ctx=mock_ctx, seconds=5)
        mock_sleep.assert_called_once_with(5)
        assert result == "Successfully waited for 5 seconds."


@pytest.mark.asyncio
async def test_wait_callable_execution(mock_ctx):
    """Verify invoking wait directly as a callable."""
    with pytest.MonkeyPatch.context() as mp:
        mock_sleep = AsyncMock()
        mp.setattr(asyncio, "sleep", mock_sleep)

        result = await wait(ctx=mock_ctx, seconds=3)
        mock_sleep.assert_called_once_with(3)
        assert result == "Successfully waited for 3 seconds."


@pytest.mark.asyncio
async def test_wait_tool(mock_ctx):
    wait_tool = get_wait_tool(mock_ctx)

    start_time = time.time()
    result = await wait_tool.ainvoke({"seconds": 2})
    end_time = time.time()

    duration = end_time - start_time
    assert duration >= 1.9  # Allow slight timing variations
    assert "Successfully waited for 2 seconds" in result


@pytest.mark.asyncio
async def test_wait_tool_bounds(mock_ctx):
    wait_tool = get_wait_tool(mock_ctx)

    with pytest.MonkeyPatch.context() as mp:
        mock_sleep = AsyncMock()
        mp.setattr(asyncio, "sleep", mock_sleep)

        await wait_tool.ainvoke({"seconds": 120})
        mock_sleep.assert_called_once_with(60)

        mock_sleep.reset_mock()
        await wait_tool.ainvoke({"seconds": -5})
        mock_sleep.assert_called_once_with(1)
