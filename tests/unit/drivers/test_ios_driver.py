# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Native iOS behavior tested without Xcode, CoreSimulator, or a device."""

import asyncio
import base64
import json
from pathlib import Path
import plistlib
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image
import pytest

from artemis.drivers.base import KeyCode
from artemis.drivers.ios import xcode_driver
from artemis.drivers.ios.bridge import XcodeApprovalRequiredError
from artemis.drivers.ios.hierarchy import application_bundle, parse_hierarchy
from artemis.drivers.ios.xcode_driver import XcodeSimulatorDriver, run_xcrun
from third_party.mobile_use.controllers.types import ElementQuery


IOS_A = "DE345DD3-5792-4DAD-B863-144682629565"
IOS_B = "F81B6533-B1EC-40CE-8A21-9CD4F2994E55"
WATCH = "362C24CC-751F-457F-B8BF-9FFAC1F855A0"
NATIVE_TOOLS = {
    "DeviceInteractionStartSession",
    "DeviceInteractionSynthesize",
    "DeviceInteractionEndSession",
}
# Apple's packaged device-interaction skill uses these frame/hitPoint formats.
APPLE_HIERARCHY = """Device orientation: Portrait
------------------------
Application bundle identifier: com.example.app
Application UI orientation: Portrait
Application, pid: 123, label: 'Example'
 Window, {{0.0, 0.0}, {200.0, 400.0}}, hitPoint: {100.0, 200.0}
  Other, {{0.0, 0.0}, {200.0, 400.0}}, hitPoint: {100.0, 200.0}
   Button, {{10.0, 20.0}, {60.0, 30.0}}, identifier: 'login', label: 'Login', hitPoint: {15.0, 25.0}, activationBundleId: com.example.app
   StaticText, {{20.0, 70.0}, {100.0, 20.0}}, label: 'Welcome'
"""


def device(udid=IOS_A, state="Booted", available=True):
    return {"udid": udid, "name": "iPhone", "state": state, "isAvailable": available}


class NativeBridge:
    def __init__(self, observation):
        self.tools = NATIVE_TOOLS.copy()
        self.connected = False
        self.start = AsyncMock(side_effect=self.open)
        self.close = AsyncMock(side_effect=self.finish)
        self.call = AsyncMock(side_effect=self.respond)
        self.observation = observation
        self.hooks = {}
        self.start_result = {
            "interactionSessionKey": "test-session",
            "deviceUUID": IOS_A,
            "deviceIsSimulator": True,
        }

    async def open(self):
        self.connected = True

    async def finish(self):
        self.connected = False

    async def respond(self, name, arguments):
        if name in self.hooks:
            return await self.hooks[name](arguments)
        if name == "DeviceInteractionStartSession":
            return self.start_result
        if name == "DeviceInteractionSynthesize":
            return self.observation
        if name == "DeviceInteractionEndSession":
            return {"userMessage": "Closed"}
        if name == "XcodeOpenWorkspace":
            return {"workspaceIdentifier": "workspace-1"}
        raise AssertionError(f"Unexpected native tool {name}")


@pytest.fixture
def simulator(tmp_path, monkeypatch):
    screenshot = tmp_path / "native.png"
    Image.new("RGB", (200, 400)).save(screenshot)
    hierarchy = tmp_path / "native-hierarchy.txt"
    hierarchy.write_text(APPLE_HIERARCHY, encoding="utf-8")
    observation = {
        "screenshotPath": str(screenshot),
        "hierarchyPath": str(hierarchy),
        "thumbnailScreenshotPath": str(screenshot),
        "logsPath": str(tmp_path / "logs.txt"),
        "applicationState": "NotRun",
    }
    native = NativeBridge(observation)
    inventory = {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-27-0": [device()]}}

    async def command(*arguments, timeout=30):
        if arguments == ("xcodebuild", "-version"):
            return b"Xcode 27.0\nBuild version 27A266a\n"
        if arguments == ("simctl", "list", "devices", "--json"):
            return json.dumps(inventory).encode()
        if arguments[:2] == ("simctl", "listapps"):
            # Real simctl emits an OpenStep/ASCII plist that plistlib cannot
            # parse — the driver must route it through plutil.
            return (
                b'{ "com.example.app" = { CFBundleDisplayName = Example; '
                b'CFBundleIdentifier = "com.example.app"; }; }'
            )
        return b""

    async def fake_plutil(payload: bytes, timeout: float = 30.0) -> bytes:
        return json.dumps({"com.example.app": {"CFBundleDisplayName": "Example"}}).encode()

    commands = AsyncMock(side_effect=command)
    monkeypatch.setattr(xcode_driver.sys, "platform", "darwin")
    monkeypatch.setattr(xcode_driver, "run_xcrun", commands)
    monkeypatch.setattr(xcode_driver, "plist_to_json", fake_plutil)
    monkeypatch.setattr(xcode_driver, "XcodeBridge", lambda: native)
    return SimpleNamespace(
        driver=XcodeSimulatorDriver(),
        native=native,
        commands=commands,
        inventory=inventory,
        screenshot=screenshot,
        hierarchy=hierarchy,
    )


@pytest.mark.asyncio
async def test_booted_selection_pins_every_app_operation_to_one_ios_udid(simulator, tmp_path):
    simulator.inventory["devices"]["com.apple.CoreSimulator.SimRuntime.iOS-27-0"].append(
        device(IOS_B, state="Shutdown")
    )
    simulator.inventory["devices"]["com.apple.CoreSimulator.SimRuntime.watchOS-27-0"] = [
        device(WATCH)
    ]
    driver = simulator.driver
    await driver.connect()
    assert driver.device_id == IOS_A
    await driver.launch_app("com.example.app")
    await driver.stop_app("com.example.app")
    await driver.open_url("example://item?q=$literal")
    assert await driver.list_apps() == {"com.example.app": "Example"}
    app = tmp_path / "Example.app"
    app.mkdir()
    (app / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.example.app"}))
    assert await driver.install_app(app) == "com.example.app"

    native_start = simulator.native.call.await_args_list[0]
    assert native_start.args[0] == "DeviceInteractionStartSession"
    assert native_start.args[1]["deviceIdentifier"] == IOS_A
    assert all(
        call.args[2] == IOS_A
        for call in simulator.commands.await_args_list
        if len(call.args) >= 3
        and call.args[:2]
        in {
            ("simctl", "launch"),
            ("simctl", "terminate"),
            ("simctl", "openurl"),
            ("simctl", "listapps"),
            ("simctl", "install"),
        }
    )
    assert not any(
        call.args[:2] == ("simctl", "boot") for call in simulator.commands.await_args_list
    )
    await driver.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("booted_count", [0, 2])
async def test_booted_selector_rejects_ambiguous_or_missing_devices(simulator, booted_count):
    simulator.inventory["devices"]["com.apple.CoreSimulator.SimRuntime.iOS-27-0"] = [
        device(udid) for udid in (IOS_A, IOS_B)[:booted_count]
    ]
    with pytest.raises(ValueError, match="exactly one booted iOS simulator"):
        await simulator.driver.connect()
    simulator.native.start.assert_not_awaited()
    simulator.native.call.assert_not_awaited()


@pytest.mark.asyncio
async def test_booted_selector_tolerates_mixed_case_and_whitespace(simulator):
    """The reserved 'booted' token normalizes before comparison."""
    simulator.driver._device_id = "  BoOtEd  "
    await simulator.driver.connect()
    assert simulator.driver.device_id == IOS_A
    await simulator.driver.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("requested, available", [(WATCH, True), (IOS_A, False)])
async def test_explicit_selection_rejects_non_ios_and_unavailable_devices(
    simulator, requested, available
):
    simulator.driver._device_id = requested
    simulator.inventory["devices"]["com.apple.CoreSimulator.SimRuntime.iOS-27-0"][0][
        "isAvailable"
    ] = available
    simulator.inventory["devices"]["com.apple.CoreSimulator.SimRuntime.watchOS-27-0"] = [
        device(WATCH)
    ]
    with pytest.raises(ValueError, match="Unavailable iOS simulator"):
        await simulator.driver.connect()
    simulator.native.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_shutdown_device_is_booted_and_selected_case_insensitively(simulator):
    simulator.driver._device_id = IOS_A.lower()
    simulator.inventory["devices"]["com.apple.CoreSimulator.SimRuntime.iOS-27-0"][0]["state"] = (
        "Shutdown"
    )
    await simulator.driver.connect()
    simulator.commands.assert_any_await("simctl", "boot", IOS_A)
    simulator.commands.assert_any_await("simctl", "bootstatus", IOS_A, "-b", timeout=180.0)
    assert simulator.driver.device_id == IOS_A
    await simulator.driver.disconnect()


@pytest.mark.asyncio
async def test_repeated_connect_reuses_session(simulator):
    await simulator.driver.connect()
    await simulator.driver.connect()
    simulator.native.start.assert_awaited_once()
    starts = [
        c for c in simulator.native.call.await_args_list if c.args[0].endswith("StartSession")
    ]
    assert len(starts) == 1
    await simulator.driver.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        {"interactionSessionKey": "wrong-target", "deviceUUID": IOS_B, "deviceIsSimulator": True},
        {
            "interactionSessionKey": "physical-target",
            "deviceUUID": IOS_A,
            "deviceIsSimulator": False,
        },
    ],
)
async def test_native_wrong_target_is_closed_before_any_interaction(simulator, result):
    simulator.native.start_result = result
    with pytest.raises(RuntimeError, match="different device"):
        await simulator.driver.connect()
    simulator.native.call.assert_any_await(
        "DeviceInteractionEndSession", {"interactionSessionKey": result["interactionSessionKey"]}
    )
    assert not any(
        c.args[0] == "DeviceInteractionSynthesize" for c in simulator.native.call.await_args_list
    )
    simulator.native.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_native_tools_fail_before_session_start(simulator):
    simulator.native.tools.remove("DeviceInteractionSynthesize")
    with pytest.raises(RuntimeError, match="tools are unavailable"):
        await simulator.driver.connect()
    simulator.native.call.assert_not_awaited()
    simulator.native.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_macos_host_is_rejected_before_launching_native_tools(simulator, monkeypatch):
    monkeypatch.setattr(xcode_driver.sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="requires macOS"):
        await simulator.driver.connect()
    simulator.commands.assert_not_awaited()
    simulator.native.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_xcode_is_rejected_before_device_boot(simulator):
    simulator.commands.side_effect = None
    simulator.commands.return_value = b"Xcode 26.3\nBuild version 17C529\n"
    with pytest.raises(RuntimeError, match="requires Xcode 27"):
        await simulator.driver.connect()
    simulator.commands.assert_awaited_once_with("xcodebuild", "-version")
    simulator.native.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_session_error_closes_bridge_without_fake_session_cleanup(simulator):
    simulator.native.call.side_effect = RuntimeError("Agent approval required")
    with pytest.raises(RuntimeError, match="Agent approval required"):
        await simulator.driver.connect()
    assert [c.args[0] for c in simulator.native.call.await_args_list] == [
        "DeviceInteractionStartSession"
    ]
    simulator.native.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_session_key_is_rejected_and_bridge_closed(simulator):
    simulator.native.start_result.pop("interactionSessionKey")
    with pytest.raises(RuntimeError, match="session key"):
        await simulator.driver.connect()
    simulator.native.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_connect_preserves_device_rejection_when_cleanup_also_fails(simulator):
    simulator.native.start_result["deviceUUID"] = IOS_B

    async def respond(name, arguments):
        if name == "DeviceInteractionEndSession":
            raise RuntimeError("Cleanup unavailable")
        return await simulator.native.respond(name, arguments)

    simulator.native.call.side_effect = respond
    with pytest.raises(RuntimeError, match="different device"):
        await simulator.driver.connect()
    simulator.native.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_initial_observation_releases_native_session(simulator):
    async def fail_capture(name, arguments):
        if name == "DeviceInteractionSynthesize":
            raise RuntimeError("Accessibility service failed")
        return await simulator.native.respond(name, arguments)

    simulator.native.call.side_effect = fail_capture
    with pytest.raises(RuntimeError, match="Accessibility service failed"):
        await simulator.driver.connect()
    simulator.native.call.assert_any_await(
        "DeviceInteractionEndSession", {"interactionSessionKey": "test-session"}
    )
    simulator.native.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_disconnect_closes_bridge_even_when_session_end_fails(simulator):
    await simulator.driver.connect()
    simulator.native.call.side_effect = RuntimeError("Native session expired")
    with pytest.raises(RuntimeError, match="expired"):
        await simulator.driver.disconnect()
    simulator.native.close.assert_awaited_once()
    with pytest.raises(RuntimeError, match="Connect"):
        await simulator.driver.get_screen_data()


@pytest.mark.asyncio
async def test_cancelled_initial_capture_releases_session_and_preserves_cancellation(simulator):
    started = asyncio.Event()

    async def respond(name, arguments):
        if name == "DeviceInteractionSynthesize":
            started.set()
            await asyncio.Event().wait()
        return await simulator.native.respond(name, arguments)

    simulator.native.call.side_effect = respond
    task = asyncio.create_task(simulator.driver.connect())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    simulator.native.call.assert_any_await(
        "DeviceInteractionEndSession", {"interactionSessionKey": "test-session"}
    )
    simulator.native.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_disconnect_reopens_retired_transport_only_to_close_known_session(simulator):
    await simulator.driver.connect()
    simulator.native.connected = False
    simulator.native.call.reset_mock()
    await simulator.driver.disconnect()
    assert simulator.native.start.await_count == 2
    simulator.native.call.assert_awaited_once_with(
        "DeviceInteractionEndSession", {"interactionSessionKey": "test-session"}
    )
    simulator.native.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("scale", [1, 3])
async def test_observation_and_coordinate_actions_share_screenshot_geometry(simulator, scale):
    Image.new("RGB", (200 * scale, 400 * scale)).save(simulator.screenshot)
    await simulator.driver.connect()
    data = await simulator.driver.get_screen_data()
    assert data.platform == "ios"
    assert (data.width, data.height) == (200 * scale, 400 * scale)
    assert base64.b64decode(data.screenshot_base64) == data.screenshot_bytes
    button = next(element for element in data.ui_elements if element["resource_id"] == "login")
    assert button["hit_point"] == [15 * scale, 25 * scale]
    assert button["parsed_bounds"] == {
        "left": 10 * scale,
        "top": 20 * scale,
        "right": 70 * scale,
        "bottom": 50 * scale,
    }

    await simulator.driver.tap(15 * scale, 25 * scale, duration_ms=600)
    arguments = simulator.native.call.await_args.args[1]
    assert arguments["interactSessionKey"] == "test-session"
    command = arguments["interactionCommand"].split()
    assert command[0] == "t"
    assert list(map(float, command[1:])) == [15, 25, 0.6]

    await simulator.driver.swipe(150 * scale, 300 * scale, 50 * scale, 100 * scale, 250)
    command = simulator.native.call.await_args.args[1]["interactionCommand"].split()
    assert command[0] == "t" and command[3] == "f"
    assert list(map(float, command[1:3] + command[4:])) == [150, 300, 50, 100, 0.25]
    await simulator.driver.disconnect()


@pytest.mark.asyncio
async def test_element_action_uses_native_hit_point_and_app_activation(simulator):
    await simulator.driver.connect()
    assert await simulator.driver.tap_element(ElementQuery(resource_id="login"))
    arguments = simulator.native.call.await_args.args[1]
    assert arguments["activationBundleId"] == "com.example.app"
    assert list(map(float, arguments["interactionCommand"].split()[1:])) == [15, 25, 0.1]
    assert not await simulator.driver.tap_element(ElementQuery(text="Missing"))
    await simulator.driver.disconnect()


@pytest.mark.asyncio
async def test_text_preserves_unicode_whitespace_and_literal_native_escape_sequences(simulator):
    await simulator.driver.connect()
    text = "한글🙂  'quoted'\n\t" + r"\u{000A} b h $(literal)"
    assert await simulator.driver.input_text(text, clear_existing=False)
    command = simulator.native.call.await_args.args[1]["interactionCommand"]
    assert command.startswith("sender keyboard kbd ")
    encoded = command.removeprefix("sender keyboard kbd ")
    # The native grammar decodes escapes once. Literal user escape syntax must
    # survive that decode, while actual controls and supplementary Unicode work.
    decoded = re.sub(r"\\u\{([0-9A-Fa-f]+)\}", lambda m: chr(int(m[1], 16)), encoded)
    assert decoded == text
    await simulator.driver.disconnect()


@pytest.mark.asyncio
async def test_unverified_text_replacement_is_explicitly_rejected(simulator):
    with pytest.raises(NotImplementedError, match="replace-text"):
        await simulator.driver.input_text("replacement", clear_existing=True)
    simulator.native.call.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key, command",
    [
        (KeyCode.HOME, "b h"),
        (KeyCode.APP_SWITCH, "b h b h"),
        (KeyCode.ENTER, r"sender keyboard kbd \u{000A}"),
    ],
)
async def test_supported_native_hardware_and_enter_keys(simulator, key, command):
    await simulator.driver.connect()
    assert await simulator.driver.press_key(key)
    assert simulator.native.call.await_args.args[1]["interactionCommand"] == command
    await simulator.driver.disconnect()


@pytest.mark.asyncio
async def test_android_back_and_shell_are_not_reported_as_success(simulator):
    with pytest.raises(NotImplementedError, match="not supported"):
        await simulator.driver.press_key(KeyCode.BACK)
    with pytest.raises(NotImplementedError, match="unavailable"):
        await simulator.driver.execute_shell("input keyevent BACK")
    simulator.native.call.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_hierarchy_prevents_coordinate_input(simulator):
    await simulator.driver.connect()
    simulator.native.observation.pop("hierarchyPath")
    before = len(simulator.native.call.await_args_list)
    with pytest.raises(RuntimeError, match="no accessibility hierarchy"):
        await simulator.driver.tap(15, 25)
    new_calls = simulator.native.call.await_args_list[before:]
    assert len(new_calls) == 1
    assert new_calls[0].args[1]["interactionCommand"] == ""
    await simulator.driver.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation, arguments",
    [
        ("launch_app", ("com.example.app",)),
        ("stop_app", ("com.example.app",)),
        ("open_url", ("example://",)),
        ("list_apps", ()),
        ("install_app", (Path("Untrusted.app"),)),
    ],
)
async def test_app_operations_require_connected_pinned_device(simulator, operation, arguments):
    with pytest.raises(RuntimeError, match="[Cc]onnect"):
        await getattr(simulator.driver, operation)(*arguments)
    simulator.commands.assert_not_awaited()


@pytest.mark.asyncio
async def test_directional_swipe_uses_current_screen_after_rotation(simulator):
    await simulator.driver.connect()
    Image.new("RGB", (400, 200)).save(simulator.screenshot)
    simulator.hierarchy.write_text(
        APPLE_HIERARCHY.replace("{200.0, 400.0}", "{400.0, 200.0}"), encoding="utf-8"
    )
    assert await simulator.driver.swipe_direction("up")
    command = simulator.native.call.await_args.args[1]["interactionCommand"].split()
    assert list(map(float, command[1:3] + command[4:6])) == [200, 150, 200, 50]
    await simulator.driver.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("action, arguments", [("tap", (15, 25)), ("swipe", (15, 25, 100, 150))])
async def test_stale_absolute_coordinates_are_rejected_after_rotation(simulator, action, arguments):
    await simulator.driver.connect()
    Image.new("RGB", (400, 200)).save(simulator.screenshot)
    simulator.hierarchy.write_text(
        APPLE_HIERARCHY.replace("{200.0, 400.0}", "{400.0, 200.0}"), encoding="utf-8"
    )
    simulator.native.call.reset_mock()
    with pytest.raises(ValueError, match="changed orientation or size"):
        await getattr(simulator.driver, action)(*arguments)
    simulator.native.call.assert_awaited_once_with(
        "DeviceInteractionSynthesize",
        {"interactSessionKey": "test-session", "interactionCommand": ""},
    )
    await simulator.driver.disconnect()


def test_apple_frame_formats_and_hitpoints_are_parsed_without_retina_assumptions():
    hierarchy = """Application bundle identifier: com.example.app
UIWindow {{0, 0}, {200, 400}}, hitPoint: {100, 200}
 UIButton "Login" {{10, 20}, {60, 30}}, hitPoint: {15, 25}
 UIView {{-10, 50}, {20, 20}}, hitPoint: {5, 60}
 UIButton "Offscreen" {{300, 500}, {60, 30}}
 UIView {{10, 10}, {0, 5}}
"""
    elements, scale = parse_hierarchy(hierarchy, 200, 400)
    assert scale == (1, 1)
    login = next(element for element in elements if element["text"] == "Login")
    assert login["class"] == "UIButton"
    assert login["bounds"] == "[10,20][70,50]"
    assert login["hit_point"] == [15, 25]
    assert not any(element["text"] == "Offscreen" for element in elements)
    assert len(elements) == 3


def test_known_rotated_window_geometry_is_not_guessed():
    with pytest.raises(RuntimeError, match="matching the screenshot"):
        parse_hierarchy("Window {{0, 0}, {400, 200}}", 200, 400)


def test_inaccessible_canvas_can_use_native_logical_screenshot_space():
    elements, scale = parse_hierarchy("AX hierarchy is unavailable", 200, 400)
    assert elements == []
    assert scale == (1, 1)


EMPTY_FIELD = (
    "TextField, {{32.0, 199.0}, {338.0, 22.0}}, "
    "placeholderValue: 'Habit to avoid', Keyboard Focused, hitPoint: {201.0, 210.0}"
)
FILLED_FIELD = (
    "TextField, {{32.0, 199.0}, {338.0, 22.0}}, "
    "placeholderValue: 'Habit to avoid', value: caf\u00e9 \U0001f642, "
    "Keyboard Focused, hitPoint: {201.0, 210.0}"
)
TRUNCATED_FIELD = (
    "TextField, {{32.0, 199.0}, {338.0, 22.0}}, "
    "placeholderValue: 'Habit to avoid', value: Artemis iOS direct..., "
    "Keyboard Focused, hitPoint: {201.0, 210.0}"
)
COMMA_FIELD = (
    "TextField, {{32.0, 199.0}, {338.0, 22.0}}, "
    "value: hello, world, Keyboard Focused, hitPoint: {201.0, 210.0}"
)
QUOTED_FIELD = (
    "TextField, {{32.0, 199.0}, {338.0, 22.0}}, "
    "value: 'quoted text', Keyboard Focused, hitPoint: {201.0, 210.0}"
)
LABELED_FIELD = (
    "TextField, {{32.0, 199.0}, {338.0, 22.0}}, label: 'Title', "
    "value: 'typed', placeholderValue: 'Habit to avoid', hitPoint: {201.0, 210.0}"
)


def test_empty_textfield_exposes_placeholder_as_text_and_metadata():
    elements, _ = parse_hierarchy(EMPTY_FIELD, 402, 874)
    assert len(elements) == 1
    field = elements[0]
    assert field["class"] == "TextField"
    assert field["placeholder"] == "Habit to avoid"
    assert field["text"] == "Habit to avoid"
    assert "value" not in field
    assert field["hit_point"] == [201, 210]


def test_populated_unlabeled_textfield_exposes_value_as_text_and_metadata():
    elements, _ = parse_hierarchy(FILLED_FIELD, 402, 874)
    field = elements[0]
    assert field["value"] == "caf\u00e9 \U0001f642"
    assert field["text"] == "caf\u00e9 \U0001f642"
    assert field["placeholder"] == "Habit to avoid"
    assert field["hit_point"] == [201, 210]


def test_elided_unquoted_value_is_preserved_verbatim():
    elements, _ = parse_hierarchy(TRUNCATED_FIELD, 402, 874)
    field = elements[0]
    assert field["value"] == "Artemis iOS direct..."
    assert field["text"] == "Artemis iOS direct..."


def test_unquoted_value_keeps_ordinary_commas_before_metadata():
    elements, _ = parse_hierarchy(COMMA_FIELD, 402, 874)
    field = elements[0]
    assert field["value"] == "hello, world"
    assert field["text"] == "hello, world"


def test_quoted_value_format_still_parsed():
    elements, _ = parse_hierarchy(QUOTED_FIELD, 402, 874)
    field = elements[0]
    assert field["value"] == "quoted text"
    assert field["text"] == "quoted text"


def test_labeled_textfield_keeps_label_text_and_retains_field_metadata():
    elements, _ = parse_hierarchy(LABELED_FIELD, 402, 874)
    field = elements[0]
    assert field["text"] == "Title"
    assert field["value"] == "typed"
    assert field["placeholder"] == "Habit to avoid"
    assert field["hit_point"] == [201, 210]


def test_unquoted_native_value_metadata_keeps_label_text():
    line = (
        "Other, {{369.0, 132.0}, {30.0, 414.0}}, "
        "label: 'Vertical scroll bar, 1 page', value: 0%, hitPoint: {384.0, 339.0}"
    )
    elements, _ = parse_hierarchy(line, 402, 874)
    field = elements[0]
    assert field["text"] == "Vertical scroll bar, 1 page"
    assert field["value"] == "0%"


def test_foreground_package_is_unknown_when_multiple_apps_overlap():
    assert application_bundle(APPLE_HIERARCHY) == "com.example.app"
    assert (
        application_bundle(APPLE_HIERARCHY + "Application bundle identifier: com.other.app") is None
    )
    assert application_bundle("unavailable hierarchy") is None


class BlockingProcess:
    def __init__(self):
        self.returncode = None
        self.started = asyncio.Event()
        self.communications = 0
        self.kills = 0

    async def communicate(self):
        self.communications += 1
        if self.communications == 1:
            self.started.set()
            await asyncio.Event().wait()
        return b"", b""

    def kill(self):
        self.kills += 1
        self.returncode = -9


@pytest.mark.asyncio
async def test_simctl_timeout_kills_and_reaps_child(monkeypatch):
    child = BlockingProcess()
    spawn = AsyncMock(return_value=child)
    monkeypatch.setattr(xcode_driver.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(TimeoutError):
        await run_xcrun("simctl", "bootstatus", IOS_A, "-b", timeout=0.01)
    assert child.kills == 1
    assert child.communications == 2
    assert spawn.await_args.args == ("xcrun", "simctl", "bootstatus", IOS_A, "-b")


@pytest.mark.asyncio
async def test_simctl_cancellation_kills_and_reaps_child(monkeypatch):
    child = BlockingProcess()
    monkeypatch.setattr(
        xcode_driver.asyncio, "create_subprocess_exec", AsyncMock(return_value=child)
    )
    task = asyncio.create_task(run_xcrun("simctl", "launch", IOS_A, "com.example.app"))
    await child.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert child.kills == 1
    assert child.communications == 2


@pytest.mark.asyncio
async def test_simctl_failures_report_stderr_and_preserve_literal_argv(monkeypatch):
    child = SimpleNamespace(returncode=1, communicate=AsyncMock(return_value=(b"", b"No app\xff")))
    spawn = AsyncMock(return_value=child)
    monkeypatch.setattr(xcode_driver.asyncio, "create_subprocess_exec", spawn)
    url = "example://item?command=$(literal)&x='quote'"
    with pytest.raises(RuntimeError, match="No app"):
        await run_xcrun("simctl", "openurl", IOS_A, url)
    assert spawn.await_args.args == ("xcrun", "simctl", "openurl", IOS_A, url)


DENIAL = "This agent isn't approved to use Xcode's tools yet. Call XcodeOpenWorkspace first."
PENDING = "Xcode is waiting for the user to approve this request; it has been recorded."


def _tool_names(native):
    return [call.args[0] for call in native.call.call_args_list]


@pytest.mark.asyncio
async def test_initial_approval_refusal_opens_workspace_once_and_retries_start(simulator, tmp_path):
    project = tmp_path / "Example App [2].xcodeproj"
    project.mkdir()
    native = simulator.native
    native.tools.add("XcodeOpenWorkspace")
    starts = []

    async def start_hook(arguments):
        starts.append(dict(arguments))
        if len(starts) == 1:
            raise XcodeApprovalRequiredError("DeviceInteractionStartSession", DENIAL)
        return native.start_result

    native.hooks["DeviceInteractionStartSession"] = start_hook
    driver = XcodeSimulatorDriver(workspace_path=project)
    await driver.connect()
    assert _tool_names(native) == [
        "DeviceInteractionStartSession",
        "XcodeOpenWorkspace",
        "DeviceInteractionStartSession",
        "DeviceInteractionSynthesize",
    ]
    assert native.call.call_args_list[1].args == (
        "XcodeOpenWorkspace",
        {"path": str(project)},
    )
    assert starts[0] == starts[1]
    assert starts[0]["deviceIdentifier"] == IOS_A
    invoked = [call.args for call in simulator.commands.call_args_list]
    assert ("simctl", "boot") not in [args[:2] for args in invoked]
    assert not any("mcp-server" in args for args in invoked)
    await driver.disconnect()
    assert "DeviceInteractionEndSession" in _tool_names(native)


@pytest.mark.asyncio
async def test_pending_workspace_approval_propagates_without_retry(simulator, tmp_path):
    project = tmp_path / "App.xcodeproj"
    project.mkdir()
    native = simulator.native
    native.tools.add("XcodeOpenWorkspace")

    async def fail_start(arguments):
        raise XcodeApprovalRequiredError("DeviceInteractionStartSession", DENIAL)

    async def pending_open(arguments):
        raise XcodeApprovalRequiredError("XcodeOpenWorkspace", PENDING)

    native.hooks["DeviceInteractionStartSession"] = fail_start
    native.hooks["XcodeOpenWorkspace"] = pending_open
    driver = XcodeSimulatorDriver(workspace_path=project)
    with pytest.raises(XcodeApprovalRequiredError, match="waiting for the user") as caught:
        await driver.connect()
    assert caught.value.workspace_path == project
    assert str(project) in caught.value.guidance
    assert "not supplied" not in caught.value.guidance
    assert _tool_names(native) == ["DeviceInteractionStartSession", "XcodeOpenWorkspace"]
    native.start.assert_awaited_once()
    native.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("identifier", [None, "", 123])
async def test_malformed_workspace_acceptance_fails_closed(simulator, tmp_path, identifier):
    project = tmp_path / "App.xcodeproj"
    project.mkdir()
    native = simulator.native
    native.tools.add("XcodeOpenWorkspace")

    async def fail_start(arguments):
        raise XcodeApprovalRequiredError("DeviceInteractionStartSession", DENIAL)

    async def open_workspace(arguments):
        return {"workspaceIdentifier": identifier}

    native.hooks["DeviceInteractionStartSession"] = fail_start
    native.hooks["XcodeOpenWorkspace"] = open_workspace
    driver = XcodeSimulatorDriver(workspace_path=project)
    with pytest.raises(RuntimeError, match="no usable workspace identifier"):
        await driver.connect()
    assert _tool_names(native) == ["DeviceInteractionStartSession", "XcodeOpenWorkspace"]
    native.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_still_pending_after_workspace_open_stops_after_one_retry(simulator, tmp_path):
    project = tmp_path / "App.xcodeproj"
    project.mkdir()
    native = simulator.native
    native.tools.add("XcodeOpenWorkspace")

    async def fail_start(arguments):
        raise XcodeApprovalRequiredError("DeviceInteractionStartSession", PENDING)

    native.hooks["DeviceInteractionStartSession"] = fail_start
    driver = XcodeSimulatorDriver(workspace_path=project)
    with pytest.raises(XcodeApprovalRequiredError, match="waiting for the user") as caught:
        await driver.connect()
    assert caught.value.workspace_path == project
    assert _tool_names(native) == [
        "DeviceInteractionStartSession",
        "XcodeOpenWorkspace",
        "DeviceInteractionStartSession",
    ]
    native.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_approval_refusal_without_workspace_propagates_guidance(simulator):
    native = simulator.native
    native.tools.add("XcodeOpenWorkspace")

    async def fail_start(arguments):
        raise XcodeApprovalRequiredError("DeviceInteractionStartSession", DENIAL)

    native.hooks["DeviceInteractionStartSession"] = fail_start
    with pytest.raises(XcodeApprovalRequiredError) as caught:
        await simulator.driver.connect()
    assert _tool_names(native) == ["DeviceInteractionStartSession"]
    assert caught.value.workspace_path is None
    assert "Workspace: not supplied" in caught.value.guidance
    assert "--ios-workspace" in caught.value.guidance
    native.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_approved_sessions_never_open_workspace(simulator, tmp_path):
    project = tmp_path / "App.xcodeproj"
    project.mkdir()
    simulator.native.tools.add("XcodeOpenWorkspace")
    driver = XcodeSimulatorDriver(workspace_path=project)
    await driver.connect()
    await driver.connect()
    names = _tool_names(simulator.native)
    assert "XcodeOpenWorkspace" not in names
    assert names.count("DeviceInteractionStartSession") == 1
    await driver.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("native boom"), TimeoutError("slow tool")])
async def test_non_approval_failures_skip_workspace_and_retry(simulator, tmp_path, failure):
    project = tmp_path / "App.xcodeproj"
    project.mkdir()
    native = simulator.native
    native.tools.add("XcodeOpenWorkspace")

    async def fail(arguments):
        raise failure

    native.hooks["DeviceInteractionStartSession"] = fail
    driver = XcodeSimulatorDriver(workspace_path=project)
    with pytest.raises(type(failure)):
        await driver.connect()
    assert _tool_names(native) == ["DeviceInteractionStartSession"]
    native.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_workspace_tool_reports_actionable_approval_error(simulator, tmp_path):
    project = tmp_path / "App.xcodeproj"
    project.mkdir()
    native = simulator.native

    async def fail_start(arguments):
        raise XcodeApprovalRequiredError("DeviceInteractionStartSession", DENIAL)

    native.hooks["DeviceInteractionStartSession"] = fail_start
    driver = XcodeSimulatorDriver(workspace_path=project)
    with pytest.raises(XcodeApprovalRequiredError, match="XcodeOpenWorkspace") as caught:
        await driver.connect()
    assert caught.value.workspace_path == project
    assert str(project) in caught.value.guidance
    assert _tool_names(native) == ["DeviceInteractionStartSession"]
    native.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_workspace_must_be_existing_project_directory(simulator, tmp_path):
    (tmp_path / "plain-folder").mkdir()
    (tmp_path / "file.xcodeproj").write_text("x")
    bad_paths = [
        tmp_path / "missing.xcodeproj",
        tmp_path / "plain-folder",
        tmp_path / "file.xcodeproj",
    ]
    for path in bad_paths:
        driver = XcodeSimulatorDriver(workspace_path=path)
        with pytest.raises(ValueError, match="xcodeproj or .xcworkspace"):
            await driver.connect()
    simulator.commands.assert_not_called()
    simulator.native.start.assert_not_called()

    workspace = tmp_path / "Nested Dir [x].xcworkspace"
    workspace.mkdir()
    driver = XcodeSimulatorDriver(workspace_path=workspace)
    await driver.connect()
    await driver.disconnect()


@pytest.mark.asyncio
async def test_duplicate_simulator_names_reject_instead_of_first_matching(simulator):
    """Two same-named sims must not silently bind — pin the UDID instead."""
    simulator.driver._device_id = "Office iPhone"
    simulator.inventory["devices"]["com.apple.CoreSimulator.SimRuntime.iOS-27-0"] = [
        device(IOS_A) | {"name": "Office iPhone"},
        device(IOS_B, state="Shutdown") | {"name": "Office iPhone"},
    ]
    with pytest.raises(ValueError, match="UDID"):
        await simulator.driver.connect()
    simulator.native.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_exact_simulator_name_resolves_to_its_udid(simulator):
    simulator.driver._device_id = "iPhone"
    await simulator.driver.connect()
    assert simulator.driver.device_id == IOS_A
    await simulator.driver.disconnect()


@pytest.mark.asyncio
async def test_dropped_interaction_session_restarts_and_retries_once(simulator):
    """Xcode drops idle interaction sessions; a "Session not found" reply means
    the command never ran, so the driver re-opens the session and retries."""
    driver = simulator.driver
    await driver.connect()

    calls = []

    async def session_loss(arguments):
        calls.append(arguments)
        if len(calls) == 1:
            raise RuntimeError(
                'Xcode tool DeviceInteractionSynthesize failed: {"type":"error",'
                '"data":"Session not found. It may have already been closed, '
                'or the identifier is wrong"}'
            )
        return {"userMessage": "ok"}

    simulator.native.hooks["DeviceInteractionSynthesize"] = session_loss
    simulator.native.start_result = {
        "interactionSessionKey": "fresh-session",
        "deviceUUID": IOS_A,
        "deviceIsSimulator": True,
    }

    assert await driver.press_key("home") is True
    # The retry must run under the new session key, not the dropped one.
    assert calls[1]["interactSessionKey"] == "fresh-session"
    starts = [
        c for c in simulator.native.call.await_args_list if c.args[0].endswith("StartSession")
    ]
    assert len(starts) == 2
    await driver.disconnect()


@pytest.mark.asyncio
async def test_dropped_session_restart_failure_does_not_retry_forever(simulator):
    driver = simulator.driver
    await driver.connect()

    async def always_lost(arguments):
        raise RuntimeError("Session not found")

    simulator.native.hooks["DeviceInteractionSynthesize"] = always_lost
    simulator.native.start_result = {
        "interactionSessionKey": "fresh-session",
        "deviceUUID": IOS_A,
        "deviceIsSimulator": True,
    }

    with pytest.raises(RuntimeError, match="Session not found"):
        await driver.press_key("home")
    starts = [
        c for c in simulator.native.call.await_args_list if c.args[0].endswith("StartSession")
    ]
    assert len(starts) == 2
    await driver.disconnect()


@pytest.mark.asyncio
async def test_orphaned_native_session_is_ended_and_start_retried(simulator):
    """A crashed runner leaves an interaction session behind; the next connect
    ends the reported orphan and retries instead of failing."""
    calls = []

    async def busy_once(arguments):
        calls.append(arguments)
        if len(calls) == 1:
            raise RuntimeError(
                "The target device is already in use by a different session "
                "with key 'Artemis Simulator deadbeef'. If that session is no "
                "longer needed, stop it first and retry."
            )
        return simulator.native.start_result

    simulator.native.hooks["DeviceInteractionStartSession"] = busy_once
    await simulator.driver.connect()
    ends = [c for c in simulator.native.call.await_args_list if c.args[0].endswith("EndSession")]
    assert ends[0].args[1] == {"interactionSessionKey": "Artemis Simulator deadbeef"}
    assert len([c for c in calls]) == 2
    await simulator.driver.disconnect()
