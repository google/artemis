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
from artemis.tools.base import ArtemisTool
from artemis.tools.mobile.launch_app import (
    LaunchApp,
    LaunchAppArgs,
    LaunchAppTool,
    get_launch_app_tool,
    launch_app,
    launch_app_wrapper,
)
from langchain_core.messages import ToolMessage
import pytest


@pytest.fixture
def mock_ctx():
    ctx = MagicMock(spec=ArtemisContext)
    ctx.package_cache = {}
    return ctx


@pytest.fixture
def mock_driver():
    driver = MagicMock(spec=BaseDeviceDriver)
    driver.launch_app = AsyncMock(return_value=True)
    return driver


@pytest.fixture
def mock_state():
    return MagicMock(spec=State)


def test_launch_app_tool_subclass():
    """Verify LaunchAppTool is a subclass of ArtemisTool."""
    assert issubclass(LaunchAppTool, ArtemisTool)
    assert issubclass(LaunchApp, ArtemisTool)
    assert isinstance(launch_app, ArtemisTool)
    assert isinstance(launch_app, LaunchAppTool)

    assert launch_app.name == "launch_app"
    assert launch_app.category == "action"
    assert launch_app.args_schema == LaunchAppArgs

    # GenAI FunctionDeclaration export
    declaration = launch_app.to_genai_declaration()
    assert declaration.name == "launch_app"
    assert "app_name" in declaration.parameters.properties

    # Wrapper check
    assert launch_app_wrapper is not None
    assert launch_app_wrapper.tool_fn_getter == get_launch_app_tool


@pytest.mark.asyncio
async def test_launch_app_direct_execution_with_driver(mock_driver):
    """Verify direct execution with BaseDeviceDriver."""
    result = await launch_app.execute(driver=mock_driver, app_name="com.android.settings")
    mock_driver.launch_app.assert_called_once_with("com.android.settings")
    assert "App 'com.android.settings' launched successfully." in result


@pytest.mark.asyncio
async def test_launch_app_direct_execution_with_ctx(mock_ctx):
    """Verify direct execution with ArtemisContext."""
    with (
        patch(
            "artemis.tools.mobile.launch_app.find_package",
            new_callable=AsyncMock,
            return_value="com.example.myapp",
        ) as mock_find,
        patch(
            "artemis.tools.mobile.launch_app.launch_app_with_retries",
            new_callable=AsyncMock,
            return_value=(True, None),
        ) as mock_launch,
    ):
        result = await launch_app.execute(ctx=mock_ctx, app_name="My App")
        mock_find.assert_called_once_with(ctx=mock_ctx, app_name="My App")
        mock_launch.assert_called_once_with(ctx=mock_ctx, app_package="com.example.myapp")
        assert "App 'My App' launched successfully." in result


@pytest.mark.asyncio
async def test_launch_app_package_not_found(mock_ctx):
    """Verify outcome when package is not found."""
    with patch(
        "artemis.tools.mobile.launch_app.find_package",
        new_callable=AsyncMock,
        return_value=None,
    ):
        result = await launch_app.execute(ctx=mock_ctx, app_name="Nonexistent App")
        assert "Failed to launch app 'Nonexistent App': Package not found." in result


@pytest.mark.asyncio
async def test_launch_app_execution_failure(mock_ctx):
    """Verify outcome when launch_app_with_retries fails."""
    with (
        patch(
            "artemis.tools.mobile.launch_app.find_package",
            new_callable=AsyncMock,
            return_value="com.example.failapp",
        ),
        patch(
            "artemis.tools.mobile.launch_app.launch_app_with_retries",
            new_callable=AsyncMock,
            return_value=(False, "App crashed on startup"),
        ),
    ):
        result = await launch_app.execute(ctx=mock_ctx, app_name="Crash App")
        assert "Failed to launch app 'Crash App': App crashed on startup" in result


@pytest.mark.asyncio
async def test_launch_app_with_state_command(mock_ctx, mock_state):
    """Verify LaunchAppTool returns ToolMessage when state is provided."""
    with (
        patch(
            "artemis.tools.mobile.launch_app.find_package",
            new_callable=AsyncMock,
            return_value="com.example.stateapp",
        ),
        patch(
            "artemis.tools.mobile.launch_app.launch_app_with_retries",
            new_callable=AsyncMock,
            return_value=(True, None),
        ),
    ):
        cmd = await launch_app.execute(
            ctx=mock_ctx,
            app_name="State App",
            tool_call_id="call_la_1",
            state=mock_state,
        )
        assert isinstance(cmd, ToolMessage)
        assert cmd.tool_call_id == "call_la_1"
        assert cmd.status == "success"
        assert "App 'State App' launched successfully." in cmd.content


@pytest.mark.asyncio
async def test_launch_app_callable_execution(mock_driver):
    """Verify invoking launch_app directly as a callable."""
    result = await launch_app(driver=mock_driver, app_name="com.android.chrome")
    assert "App 'com.android.chrome' launched successfully." in result


@pytest.mark.asyncio
async def test_launch_app_missing_name(mock_ctx):
    """Verify error handling when app_name is empty."""
    result = await launch_app.execute(ctx=mock_ctx, app_name="")
    assert "Error during launch app: app_name parameter is required." in result


@pytest.mark.asyncio
async def test_get_launch_app_tool_langchain_ainvoke(mock_ctx):
    """Verify get_launch_app_tool exports a LangChain tool that works with ainvoke."""
    la_tool = get_launch_app_tool(mock_ctx)
    assert la_tool.name == "launch_app"

    with (
        patch(
            "artemis.tools.mobile.launch_app.find_package",
            new_callable=AsyncMock,
            return_value="com.example.ainvoke",
        ),
        patch(
            "artemis.tools.mobile.launch_app.launch_app_with_retries",
            new_callable=AsyncMock,
            return_value=(True, None),
        ),
    ):
        result = await la_tool.ainvoke({"app_name": "Invoke App"})
        assert "App 'Invoke App' launched successfully." in result
