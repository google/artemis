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

"""Hermetic tests for the CLI/SDK/native-action boundaries of iOS support."""

import asyncio
import io
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image
from typer.testing import CliRunner

from artemis.context import ArtemisContext, DeviceContext, DevicePlatform
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.drivers.base import ScreenData
from artemis.drivers.factory import create_driver
from artemis.graph.state import State
from artemis.interfaces.cli.commands import run as run_module
from artemis.interfaces.cli.main import app
from artemis.mcp.action_session import get_action_session
from artemis.mcp.action_types import ActionCode
from artemis.mcp.actuators.ios import IosActuator
from artemis.sdk.agent import Agent
from artemis.sdk.builders.agent_config_builder import AgentConfigBuilder
from artemis.tools.mobile.launch_app import find_package, launch_app
from artemis.drivers.ios.bridge import XcodeApprovalRequiredError
from artemis.runtime import DeviceBusyError
from third_party.mobile_use.sdk.agent import AgentBase
from third_party.mobile_use.sdk.types.exceptions import AgentError, AgentNotInitializedError


@pytest.fixture
def native_driver(monkeypatch):
    driver = MagicMock()
    driver.device_id = "00000000-0000-0000-0000-000000000001"
    driver.screen_size = (1170, 2532)
    for method in (
        "connect",
        "resolve_device",
        "disconnect",
        "input_text",
        "press_key",
        "open_url",
        "tap",
        "swipe",
        "long_press",
        "launch_app",
        "stop_app",
        "install_app",
        "execute_shell",
    ):
        setattr(driver, method, AsyncMock(return_value=True))
    driver.list_apps = AsyncMock(return_value={"com.apple.Preferences": "Settings"})
    driver.get_screen_data = AsyncMock(
        return_value=ScreenData(
            screenshot_bytes=b"png",
            screenshot_base64="cG5n",
            platform="ios",
            width=1170,
            height=2532,
            ui_hierarchy_xml="<hierarchy />",
        )
    )
    constructor = MagicMock(return_value=driver)
    monkeypatch.setitem(
        sys.modules,
        "artemis.drivers.ios.xcode_driver",
        SimpleNamespace(XcodeSimulatorDriver=constructor),
    )
    monkeypatch.delenv("ARTEMIS_CLOUD_MODE", raising=False)
    monkeypatch.delenv("ARTEMIS_MOCK_DRIVER", raising=False)
    driver.constructor = constructor
    return driver


def ios_context(driver=None):
    context = ArtemisContext(
        device=DeviceContext(
            mobile_platform=DevicePlatform.IOS,
            device_id="booted",
            device_width=1170,
            device_height=2532,
        )
    )
    context._active_driver = driver
    return context


def ios_config():
    return AgentConfigBuilder().for_ios_simulator().build(validate_profiles=False)


def test_factory_selects_ios_without_creating_adb(native_driver, monkeypatch):
    adb = MagicMock(side_effect=AssertionError("must not create ADB client"))
    monkeypatch.setattr("artemis.drivers.factory.AdbClient", adb)
    context = ios_context()
    assert create_driver(context) is native_driver
    native_driver.constructor.assert_called_once_with(
        device_id="booted", width=1170, height=2532, workspace_path=None
    )
    assert context.adb_client is None


def test_factory_forwards_configured_ios_workspace(native_driver, tmp_path):
    project = tmp_path / "My App.xcodeproj"
    project.mkdir()
    context = ios_context()
    context.agent_config = (
        AgentConfigBuilder()
        .for_ios_simulator()
        .with_ios_workspace(project)
        .build(validate_profiles=False)
    )
    assert create_driver(context) is native_driver
    native_driver.constructor.assert_called_once_with(
        device_id="booted", width=1170, height=2532, workspace_path=project
    )


def test_factory_tolerates_missing_agent_config(native_driver):
    context = ios_context()
    assert create_driver(context) is native_driver
    native_driver.constructor.assert_called_once_with(
        device_id="booted", width=1170, height=2532, workspace_path=None
    )


def test_macos_host_keeps_android_default(monkeypatch):
    context = ArtemisContext(device=DeviceContext(host_platform="DARWIN", device_id="android-1"))
    android = MagicMock()
    adb = MagicMock()
    monkeypatch.setattr("artemis.drivers.factory.AndroidAdbDriver", android)
    monkeypatch.setattr("artemis.drivers.factory.AdbClient", adb)
    assert create_driver(context) is android.return_value
    adb.assert_called_once()
    assert context.device.mobile_platform == DevicePlatform.ANDROID


def test_factory_rejects_cloud_ios_before_importing_gateway(native_driver, monkeypatch):
    monkeypatch.setenv("ARTEMIS_CLOUD_MODE", "1")
    with pytest.raises(ValueError, match="local only"):
        create_driver(ios_context())
    native_driver.constructor.assert_not_called()


@pytest.mark.asyncio
async def test_sdk_ios_initialization_bypasses_android_and_cleans(native_driver, monkeypatch):
    monkeypatch.setattr(
        AgentBase, "_init_internal", AsyncMock(side_effect=AssertionError("ADB init"))
    )
    agent = Agent(config=ios_config())
    configured_video = agent._config.video_recording_tools_enabled
    agent._prewarm_llm_connections = AsyncMock()
    assert await agent.init() is True
    await asyncio.sleep(0)
    native_driver.resolve_device.assert_awaited_once()
    native_driver.connect.assert_not_awaited()
    assert agent._device_context.device_id == native_driver.device_id
    assert (agent._device_context.device_width, agent._device_context.device_height) == (1170, 2532)
    assert agent._adb_client is None and agent._ui_adb_client is None
    assert agent._config.video_recording_tools_enabled == configured_video
    assert agent._config.disable_device_probes is True
    await agent.clean()
    native_driver.disconnect.assert_awaited_once()
    assert agent._initialized is False


@pytest.mark.asyncio
async def test_sdk_init_forwards_ios_workspace(native_driver, tmp_path):
    project = tmp_path / "Example.xcodeproj"
    project.mkdir()
    config = (
        AgentConfigBuilder()
        .for_ios_simulator(workspace_path=project)
        .build(validate_profiles=False)
    )
    agent = Agent(config=config)
    agent._prewarm_llm_connections = AsyncMock()
    assert await agent.init() is True
    native_driver.constructor.assert_called_once_with(device_id="booted", workspace_path=project)
    await agent.clean()


@pytest.mark.asyncio
async def test_sdk_failed_ios_init_closes_partial_transport(native_driver):
    native_driver.resolve_device.side_effect = RuntimeError("bridge failed")
    agent = Agent(config=ios_config())
    with pytest.raises(RuntimeError, match="bridge failed"):
        await agent.init()
    native_driver.disconnect.assert_awaited_once()
    assert agent._initialized is False


@pytest.mark.asyncio
async def test_sdk_device_overrides_preserve_ios(native_driver):
    agent = Agent(config=ios_config(), device_serial="simulator-2")
    assert agent._config.device_platform == DevicePlatform.IOS
    agent._init_internal = AsyncMock(return_value=True)
    await agent.init(device_serial="simulator-3")
    assert agent._config.device_id == "simulator-3"
    assert agent._config.device_platform == DevicePlatform.IOS


@pytest.mark.asyncio
async def test_sdk_sequential_run_reconnects_driver_without_android_unlock(native_driver):
    agent = Agent(config=ios_config())
    agent._ios_driver = native_driver
    agent._device_context = ios_context().device
    await agent._ensure_device_unlocked()
    await ios_context(native_driver).disconnect_driver()
    await agent._ensure_device_unlocked()
    assert native_driver.connect.await_count == 2
    native_driver.execute_shell.assert_not_awaited()


@pytest.mark.asyncio
async def test_context_closes_session_before_driver_and_only_once(native_driver):
    order = []
    native_driver.disconnect.side_effect = lambda: order.append("driver")
    context = ios_context(native_driver)
    context.action_session = SimpleNamespace(
        aclose=AsyncMock(side_effect=lambda: order.append("session"))
    )
    with pytest.raises(RuntimeError, match="task failed"):
        async with context:
            raise RuntimeError("task failed")
    await context.disconnect_driver()
    assert order == ["session", "driver"]
    assert context._active_driver is None


@pytest.mark.asyncio
async def test_action_session_selects_native_actuator_and_normalizes_coordinates(native_driver):
    context = ios_context(native_driver)
    async with context:
        session = await get_action_session(context)
        clicked = await session.call("click", {"target": [500, 500]})
        typed = await session.call("input_text", {"text": "Hello 한글", "clear_exist": False})
        assert clicked.ok and typed.ok
    native_driver.tap.assert_awaited_once_with(585, 1266, duration_ms=100, times=1, delay_ms=100)
    native_driver.input_text.assert_awaited_once_with("Hello 한글", clear_existing=False)
    native_driver.execute_shell.assert_not_awaited()


@pytest.mark.asyncio
async def test_ios_key_failure_is_not_reported_as_success(native_driver):
    native_driver.press_key.return_value = False
    result = await IosActuator(ios_context(native_driver)).press_key("back")
    assert result.ok is False and result.code == ActionCode.UNSUPPORTED


@pytest.mark.asyncio
async def test_ios_clear_refusal_happens_before_focus_mutation(native_driver):
    result = await IosActuator(ios_context(native_driver)).input_text(
        "hello", target=(500, 500), clear_exist=True
    )
    assert result.code == ActionCode.UNSUPPORTED
    native_driver.tap.assert_not_awaited()
    native_driver.input_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_ios_coordinates_follow_latest_orientation(native_driver):
    native_driver.screen_size = (2532, 1170)
    assert (await IosActuator(ios_context(native_driver)).click(500, 500)).ok
    native_driver.tap.assert_awaited_once_with(1266, 585, duration_ms=100, times=1, delay_ms=100)


@pytest.mark.asyncio
async def test_ios_urls_and_clear_use_native_driver(native_driver):
    controller = UnifiedMobileController(ios_context(native_driver))
    assert await controller.open_url("https://example.com") is True
    native_driver.open_url.assert_awaited_once_with("https://example.com")
    native_driver.input_text.return_value = False
    assert await controller.erase_text() is False
    native_driver.input_text.assert_awaited_once_with("", clear_existing=True)
    native_driver.execute_shell.assert_not_awaited()


@pytest.mark.asyncio
async def test_ios_apps_resolve_native_names_and_launch_without_android_poll(native_driver):
    context = ios_context(native_driver)
    assert await find_package(context, "settings") == "com.apple.Preferences"
    assert await find_package(context, "com.apple.Preferences") == "com.apple.Preferences"
    result = await launch_app.execute(ctx=context, app_name="Settings")
    assert result == "Launched app 'Settings' (com.apple.Preferences)."
    native_driver.launch_app.assert_awaited_once_with("com.apple.Preferences")
    native_driver.execute_shell.assert_not_awaited()


@pytest.mark.asyncio
async def test_sdk_rejects_unverifiable_ios_app_lock(native_driver):
    agent = Agent(config=ios_config())
    task = SimpleNamespace(request=SimpleNamespace(locked_app_package="com.apple.Preferences"))
    with pytest.raises(AgentError, match="foreground ownership"):
        await agent._prepare_app_lock(task, ios_context(native_driver))


@pytest.mark.asyncio
async def test_execute_task_ios_ignores_android_selection(monkeypatch):
    builder = MagicMock()
    automation = AsyncMock()
    monkeypatch.setattr(run_module, "new_default_config_builder", lambda: builder)
    monkeypatch.setattr(run_module, "run_automation", automation)
    monkeypatch.setenv("ADB_DEVICE_SERIAL", "android-env-device")
    monkeypatch.setattr(
        "artemis.runtime.device_pool.select_device",
        MagicMock(side_effect=AssertionError("ADB pool")),
    )
    await run_module.execute_task("Open Settings", platform=DevicePlatform.IOS)
    builder.for_device.assert_called_once_with(DevicePlatform.IOS, "booted")
    builder.with_video_recording_tools.assert_not_called()
    automation.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_task_ios_forwards_workspace(native_driver, monkeypatch, tmp_path):
    project = tmp_path / "My App.xcodeproj"
    project.mkdir()
    builder = MagicMock()
    automation = AsyncMock()
    monkeypatch.setattr(run_module, "new_default_config_builder", lambda: builder)
    monkeypatch.setattr(run_module, "run_automation", automation)
    await run_module.execute_task(
        "Open Settings", platform=DevicePlatform.IOS, ios_workspace_path=project
    )
    builder.with_ios_workspace.assert_called_once_with(project)
    automation.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_task_rejects_workspace_without_ios(native_driver, tmp_path):
    project = tmp_path / "App.xcodeproj"
    project.mkdir()
    with pytest.raises(ValueError, match="--platform ios"):
        await run_module.execute_task(
            "Open Settings", platform=DevicePlatform.ANDROID, ios_workspace_path=project
        )


def test_cli_ios_runs_locally_without_android_status_or_daemon(monkeypatch):
    execute = AsyncMock()
    monkeypatch.setattr(run_module, "execute_task", execute)
    monkeypatch.setattr(
        run_module,
        "display_local_device_status",
        MagicMock(side_effect=AssertionError("ADB status")),
    )
    monkeypatch.setattr(
        "artemis.runtime.ensure_daemon_running", MagicMock(side_effect=AssertionError("daemon"))
    )
    monkeypatch.delenv("ARTEMIS_TASK_WORKER", raising=False)
    monkeypatch.delenv("ARTEMIS_DEVICE_QUEUE_TICKET", raising=False)
    result = CliRunner().invoke(
        app,
        [
            "run",
            "--platform",
            "ios",
            "--standalone",
            "--device-serial",
            "booted",
            "Open Settings",
        ],
    )
    assert result.exit_code == 0, result.output
    assert execute.call_args.kwargs["platform"] == DevicePlatform.IOS
    assert execute.call_args.kwargs["device_serial"] == "booted"


def test_cli_forwards_ios_workspace(monkeypatch, tmp_path):
    project = tmp_path / "My App.xcodeproj"
    project.mkdir()
    execute = AsyncMock()
    monkeypatch.setattr(run_module, "execute_task", execute)
    result = CliRunner().invoke(
        app,
        [
            "run",
            "--platform",
            "ios",
            "--standalone",
            "--ios-workspace",
            str(project),
            "Open Settings",
        ],
    )
    assert result.exit_code == 0, result.output
    assert execute.call_args.kwargs["ios_workspace_path"] == project


def test_cli_rejects_ios_workspace_for_android(monkeypatch, tmp_path):
    project = tmp_path / "App.xcodeproj"
    project.mkdir()
    execute = AsyncMock()
    monkeypatch.setattr(run_module, "execute_task", execute)
    monkeypatch.setattr(
        "artemis.runtime.ensure_daemon_running", MagicMock(side_effect=AssertionError("daemon"))
    )
    result = CliRunner().invoke(app, ["run", "--ios-workspace", str(project), "Open Settings"])
    assert result.exit_code != 0
    execute.assert_not_called()


def test_cli_approval_error_exits_2_with_guidance_panel(monkeypatch):
    monkeypatch.setattr(
        run_module,
        "execute_task",
        AsyncMock(
            side_effect=XcodeApprovalRequiredError(
                "DeviceInteractionStartSession",
                "This agent isn't approved to use Xcode's tools yet.",
            )
        ),
    )
    result = CliRunner().invoke(app, ["run", "--platform", "ios", "--standalone", "Open Settings"])
    assert result.exit_code == 2, result.output
    assert "Xcode Approval Required" in result.output
    assert "Always Allow" in result.output
    assert "isn't approved" in result.output
    assert "Missing API Key" not in result.output
    assert "GEMINI" not in result.output


def test_cli_rejects_ios_cloud_mode_before_run(monkeypatch):
    execute = AsyncMock()
    monkeypatch.setattr(run_module, "execute_task", execute)
    monkeypatch.setenv("ARTEMIS_CLOUD_MODE", "1")
    result = CliRunner().invoke(app, ["run", "--platform", "ios", "Open Settings"])
    assert result.exit_code != 0
    execute.assert_not_called()


def test_cli_allows_ios_queue_worker(monkeypatch):
    """iOS daemon workers are supported; only cloud mode is rejected."""
    execute = AsyncMock()
    monkeypatch.setattr(run_module, "execute_task", execute)
    monkeypatch.setenv("ARTEMIS_TASK_WORKER", "1")
    monkeypatch.setenv("ARTEMIS_DEVICE_QUEUE_TICKET", "ticket-1")
    result = CliRunner().invoke(app, ["run", "--platform", "ios", "Open Settings"])
    assert result.exit_code == 0, result.output
    execute.assert_awaited_once()
    assert execute.call_args.kwargs["platform"] == DevicePlatform.IOS


def test_ios_probe_and_pro_tool_gates(native_driver, monkeypatch):
    from artemis.agents.checker.checker import probes_enabled
    from artemis.tools import index

    context = ios_context(native_driver)
    assert probes_enabled(context) is False
    tools = [
        SimpleNamespace(name=n)
        for n in ("run_adb_command", "manage_task", "save_note", "ask_explorer")
    ]
    monkeypatch.setattr(index, "build_tools_from_wrappers", lambda *args, **kwargs: tools)
    assert [t.name for t in index.get_tools_from_wrappers(context, [])] == [
        "save_note",
        "ask_explorer",
    ]


@pytest.mark.asyncio
async def test_ios_hierarchy_tool_returns_accessibility_json(native_driver):
    import json
    from artemis.tools.mobile.read_hierarchy import get_ui_hierarchy, get_ui_hierarchy_tool

    data = native_driver.get_screen_data.return_value
    data.ui_hierarchy_xml = None
    data.ui_elements = [{"text": "설정", "bounds": "[0,0][100,100]"}]
    context = ios_context(native_driver)
    result = await get_ui_hierarchy.execute(driver=native_driver, ctx=context)
    assert json.loads(result) == data.ui_elements
    assert "iOS accessibility" in get_ui_hierarchy_tool(context).description


def test_flash_and_pro_prompts_teach_ios_constraints(native_driver):
    from artemis.agents.flash.runner import FlashRunner
    from artemis.agents.operator.prompts import (
        PLAN_HISTORY_TEMPLATE_SECTION,
        render_transcript_static_system,
        resolve_operator_prompt_tools,
    )

    context = ios_context(native_driver)
    context.actuator = IosActuator(context)
    runner = object.__new__(FlashRunner)
    runner.ctx = context
    runner.goal = "Open Settings"
    flash = runner._render_system_prompt([])
    prompts = {"main_template": PLAN_HISTORY_TEMPLATE_SECTION + "\n# CURRENT OBSERVATION"}
    pro = render_transcript_static_system(prompts, context, State.initial("Open Settings"))
    for prompt in (flash, pro):
        assert "Target platform: iOS." in prompt
        assert "clear_exist=false" in prompt
        assert (
            "press_key supports enter, home, "
            "power, volume_up, volume_down, and app_switch" in prompt
        )
        assert "Back/Enter/Delete" not in prompt
    assert "run_adb_command" not in resolve_operator_prompt_tools(context)
    assert "manage_task" not in resolve_operator_prompt_tools(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["flash", "pro"])
async def test_sdk_profiles_route_repeated_tasks_through_native_actions(
    native_driver, monkeypatch, tmp_path, profile
):
    import third_party.mobile_use.sdk.agent as sdk_base

    agent = Agent(config=ios_config())
    agent._tmp_traces_dir = tmp_path
    agent._prewarm_llm_connections = AsyncMock()
    agent._prepare_trace_paths = MagicMock(return_value="native-test")
    agent._prepare_output_files = MagicMock()
    agent._finalize_tracing_safely = AsyncMock()
    agent._extract_output = AsyncMock(return_value="completed")
    engine = MagicMock()
    engine.shutdown = AsyncMock()
    monkeypatch.setattr("artemis.sdk.agent.DataEngine", lambda **kwargs: engine)
    lease = MagicMock()
    lease.get_active_owner.return_value = None
    monkeypatch.setattr(sdk_base, "DeviceExecutionLock", lease)

    class NativeFlash:
        def __init__(self, context, **kwargs):
            self.context = context

        async def run(self, state):
            assert isinstance(self.context.actuator, IosActuator)
            session = await get_action_session(self.context)
            assert (await session.call("click", {"target": [500, 500]})).ok
            return {"status": "completed"}

    async def native_graph(context):
        assert isinstance(context.actuator, IosActuator)

        async def stream(**kwargs):
            session = await get_action_session(context)
            assert (await session.call("click", {"target": [500, 500]})).ok
            yield "values", State.initial("Open Settings").model_dump()

        return SimpleNamespace(astream=stream)

    helper_lease = MagicMock()
    monkeypatch.setattr("artemis.sdk.agent.DeviceExecutionLock", helper_lease)
    monkeypatch.setattr(sdk_base, "FlashRunner", NativeFlash)
    monkeypatch.setattr(sdk_base, "get_graph", native_graph)
    app_dir = tmp_path / "Demo.app"
    app_dir.mkdir()
    native_driver.install_app.return_value = "com.example.demo"
    await agent.init()
    for _ in range(2):
        await agent.run_task(goal="Open Settings", profile=profile, app_path=app_dir)
    await asyncio.sleep(0)
    assert native_driver.tap.await_count == 2
    # Native app installation runs under the task's own lease and session, so
    # no additional helper lease is constructed and no extra session is opened.
    helper_lease.assert_not_called()
    assert native_driver.install_app.await_count == 2
    native_driver.install_app.assert_awaited_with(app_dir)
    assert native_driver.connect.await_count == 2
    assert native_driver.disconnect.await_count == 2
    native_driver.execute_shell.assert_not_awaited()
    assert all(task.status == "completed" for task in agent._tasks)


def _png_bytes(width=1170, height=2532, color=(18, 52, 86)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _ordered_lease(order: list[str]):
    class FakeLease:
        def __init__(self, *args, **kwargs):
            pass

        def acquire(self, *, cancel_event=None, **kwargs):
            order.append("acquire")

        def release(self):
            order.append("release")

    return FakeLease


async def _init_ios_agent(native_driver):
    agent = Agent(config=ios_config())
    agent._prewarm_llm_connections = AsyncMock()
    assert await agent.init() is True
    await asyncio.sleep(0)
    return agent


@pytest.mark.asyncio
async def test_ios_public_screenshot_owns_a_leased_session(native_driver, monkeypatch):
    order: list[str] = []
    monkeypatch.setattr("artemis.sdk.agent.DeviceExecutionLock", _ordered_lease(order))
    native_driver.connect.side_effect = lambda *a, **k: order.append("connect")
    native_driver.disconnect.side_effect = lambda *a, **k: order.append("disconnect")
    png = _png_bytes()

    async def capture(*args, **kwargs):
        order.append("capture")
        return ScreenData(
            screenshot_bytes=png,
            screenshot_base64="cG5n",
            platform="ios",
            width=1170,
            height=2532,
            ui_hierarchy_xml="<hierarchy />",
        )

    native_driver.get_screen_data.side_effect = capture
    agent = await _init_ios_agent(native_driver)

    image = await agent.get_screenshot()
    assert isinstance(image, Image.Image)
    assert image.size == (1170, 2532)
    assert image.getpixel((0, 0)) == (18, 52, 86)
    assert order == ["acquire", "connect", "capture", "disconnect", "release"]

    # A second public call opens a fresh lease and native session.
    order.clear()
    again = await agent.get_screenshot()
    assert again.size == (1170, 2532)
    assert order == ["acquire", "connect", "capture", "disconnect", "release"]
    assert native_driver.connect.await_count == 2
    native_driver.execute_shell.assert_not_awaited()


@pytest.mark.asyncio
async def test_ios_public_install_app_owns_a_leased_session(native_driver, monkeypatch, tmp_path):
    order: list[str] = []
    monkeypatch.setattr("artemis.sdk.agent.DeviceExecutionLock", _ordered_lease(order))
    native_driver.connect.side_effect = lambda *a, **k: order.append("connect")
    native_driver.disconnect.side_effect = lambda *a, **k: order.append("disconnect")

    async def install(path):
        order.append("install")
        return "com.example.demo"

    native_driver.install_app.side_effect = install
    app_dir = tmp_path / "Demo.app"
    app_dir.mkdir()
    (app_dir / "Info.plist").write_bytes(b"placeholder")
    agent = await _init_ios_agent(native_driver)

    bundle = await agent.install_app(app_dir)
    assert bundle == "com.example.demo"
    native_driver.install_app.assert_awaited_once_with(app_dir)
    assert order == ["acquire", "connect", "install", "disconnect", "release"]


@pytest.mark.asyncio
async def test_ios_public_helpers_require_initialization(native_driver, monkeypatch, tmp_path):
    lease = MagicMock()
    monkeypatch.setattr("artemis.sdk.agent.DeviceExecutionLock", lease)
    agent = Agent(config=ios_config())
    with pytest.raises(AgentNotInitializedError):
        await agent.get_screenshot()
    with pytest.raises(AgentNotInitializedError):
        await agent.install_app(tmp_path / "Demo.app")
    lease.assert_not_called()
    native_driver.constructor.assert_not_called()
    native_driver.connect.assert_not_awaited()

    # Android selection still delegates to the base implementation unchanged.
    android = Agent(config=AgentConfigBuilder().build(validate_profiles=False))
    with pytest.raises(AgentNotInitializedError):
        await android.get_screenshot()
    with pytest.raises(FileNotFoundError):
        await android.install_app(tmp_path / "missing.apk")


@pytest.mark.asyncio
async def test_ios_helper_failures_release_session_and_lease(native_driver, monkeypatch, tmp_path):
    order: list[str] = []
    monkeypatch.setattr("artemis.sdk.agent.DeviceExecutionLock", _ordered_lease(order))
    native_driver.disconnect.side_effect = lambda *a, **k: order.append("disconnect")
    agent = await _init_ios_agent(native_driver)

    native_driver.connect.side_effect = RuntimeError("connect failed")
    with pytest.raises(RuntimeError, match="connect failed"):
        await agent.get_screenshot()
    assert order == ["acquire", "disconnect", "release"]

    order.clear()
    native_driver.connect.side_effect = lambda *a, **k: order.append("connect")
    native_driver.get_screen_data.side_effect = RuntimeError("capture failed")
    with pytest.raises(RuntimeError, match="capture failed"):
        await agent.get_screenshot()
    assert order == ["acquire", "connect", "disconnect", "release"]

    order.clear()
    native_driver.install_app.side_effect = RuntimeError("install failed")
    app_dir = tmp_path / "Demo.app"
    app_dir.mkdir()
    with pytest.raises(RuntimeError, match="install failed"):
        await agent.install_app(app_dir)
    assert order == ["acquire", "connect", "disconnect", "release"]


@pytest.mark.asyncio
async def test_ios_operation_cancellation_drains_queued_acquire(native_driver, monkeypatch):
    started = threading.Event()
    finished = threading.Event()
    released = MagicMock()

    class FakeLease:
        def __init__(self, *args, **kwargs):
            pass

        def acquire(self, *, cancel_event=None, **kwargs):
            started.set()
            try:
                cancel_event.wait(timeout=30)
            finally:
                finished.set()
            raise DeviceBusyError("queue wait cancelled")

        def release(self):
            released()

    monkeypatch.setattr("artemis.sdk.agent.DeviceExecutionLock", FakeLease)
    agent = await _init_ios_agent(native_driver)

    helper = asyncio.ensure_future(agent.get_screenshot())
    assert await asyncio.to_thread(started.wait, 10) is True
    helper.cancel()
    with pytest.raises(asyncio.CancelledError):
        await helper
    assert finished.is_set()
    released.assert_called_once()
    native_driver.connect.assert_not_awaited()
    native_driver.get_screen_data.assert_not_awaited()


@pytest.mark.asyncio
async def test_context_disconnect_driver_swallows_expected_cleanup_errors(native_driver):
    native_driver.disconnect.side_effect = OSError("transport already closed")
    context = ios_context(native_driver)
    await context.disconnect_driver()
    assert context._active_driver is None

    native_driver.disconnect.side_effect = KeyError("unexpected")
    context = ios_context(native_driver)
    with pytest.raises(KeyError):
        await context.disconnect_driver()


@pytest.mark.parametrize(
    "flag", ["--with-video-recording-tools", "--without-video-recording-tools"]
)
def test_cli_ios_accepts_video_flag_in_standalone(monkeypatch, flag):
    execute = AsyncMock()
    monkeypatch.setattr(run_module, "execute_task", execute)
    result = CliRunner().invoke(
        app, ["run", "--platform", "ios", "--standalone", flag, "Open Settings"]
    )
    assert result.exit_code == 0, result.output
    execute.assert_awaited_once()


def _unconfigured_agent_cfg(monkeypatch, builder_module):
    """Neutralize any configured video_analyzer.enabled so detection runs."""
    real_load = builder_module.load_agent_config

    def load():
        cfg = real_load()
        cfg.video_analyzer.enabled = None
        return cfg

    monkeypatch.setattr(builder_module, "load_agent_config", load)


def test_builder_auto_detects_video_tools_per_platform(monkeypatch):
    from artemis.sdk.builders import agent_config_builder as builder_module

    _unconfigured_agent_cfg(monkeypatch, builder_module)
    observed: list[str] = []
    monkeypatch.setattr(
        builder_module,
        "detect_video_tools_enabled",
        lambda platform="android": observed.append(platform) or platform == "ios",
    )

    ios_config = AgentConfigBuilder().for_ios_simulator().build(validate_profiles=False)
    assert ios_config.video_recording_tools_enabled is True
    assert observed[-1] == "ios"

    android_config = AgentConfigBuilder().build(validate_profiles=False)
    assert android_config.video_recording_tools_enabled is False
    assert observed[-1] == "android"


def test_builder_explicit_video_flag_wins_regardless_of_order(monkeypatch):
    from artemis.sdk.builders import agent_config_builder as builder_module

    monkeypatch.setattr(
        builder_module,
        "detect_video_tools_enabled",
        lambda platform="android": True,
    )

    before = (
        AgentConfigBuilder()
        .with_video_recording_tools(enabled=False)
        .for_ios_simulator()
        .build(validate_profiles=False)
    )
    after = (
        AgentConfigBuilder()
        .for_ios_simulator()
        .with_video_recording_tools(enabled=False)
        .build(validate_profiles=False)
    )
    enabled = (
        AgentConfigBuilder()
        .for_ios_simulator()
        .with_video_recording_tools(enabled=True)
        .build(validate_profiles=False)
    )
    assert before.video_recording_tools_enabled is False
    assert after.video_recording_tools_enabled is False
    assert enabled.video_recording_tools_enabled is True


def test_agent_config_default_factory_detects_platform_from_data(monkeypatch):
    from artemis.sdk.types import agent as agent_types

    monkeypatch.setattr(
        agent_types,
        "detect_video_tools_enabled",
        lambda platform="android": platform == "ios",
    )
    from artemis.sdk.types.agent import AgentConfig

    data = AgentConfigBuilder().build(validate_profiles=False).model_dump()
    data.pop("video_recording_tools_enabled")
    data["device_platform"] = DevicePlatform.IOS
    ios = AgentConfig.model_validate(data)
    data["device_platform"] = DevicePlatform.ANDROID
    android = AgentConfig.model_validate(data)
    explicit = AgentConfig.model_validate(
        {**data, "device_platform": DevicePlatform.IOS, "video_recording_tools_enabled": False}
    )
    assert ios.video_recording_tools_enabled is True
    assert android.video_recording_tools_enabled is False
    assert explicit.video_recording_tools_enabled is False
