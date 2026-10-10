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

"""iOS Simulator support using Xcode 27's native MCP and simctl tools."""

from __future__ import annotations

import asyncio
import base64
from io import BytesIO
import json
import re
from pathlib import Path
import plistlib
import sys
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from PIL import Image

from artemis.drivers.base import BaseDeviceDriver, KeyCode, ScreenData, SwipeDirection
from artemis.drivers.ios.bridge import XcodeApprovalRequiredError, XcodeBridge
from artemis.drivers.ios.discovery import (
    clear_ios_simulator_cache,
    BOOTED_SIMULATOR_ID,
    device_matches_identifier,
    parse_simctl_devices,
    parse_xcode_version,
    plist_to_json,
    run_xcrun,
)
from artemis.drivers.ios.hierarchy import application_bundle, parse_hierarchy
from artemis.drivers.ios.recording import (
    DEFAULT_MAX_DURATION_SECONDS,
    IosRecordingSession,
    IosScreenRecorder,
)
from third_party.mobile_use.controllers.types import ElementQuery
from third_party.mobile_use.utils.logger import get_logger
from third_party.mobile_use.utils.video import get_active_session, remove_active_session

if TYPE_CHECKING:
    from artemis.drivers.ios.physical_recording import PhysicalIosRecorder

logger = get_logger(__name__)

# Xcode reports an orphaned interaction session as "already in use by a
# different session with key '<label>'"; the label doubles as the end key.
_STALE_SESSION_RE = re.compile(r"in use by a different session with key '([^']+)'")


async def _parse_listapps_output(raw: bytes) -> dict[str, Any]:
    """Parse ``simctl listapps`` output into a bundle-id keyed dict.

    simctl emits OpenStep (ASCII) plists on older Xcode, XML/binary on newer
    ones; ``plistlib`` only reads the latter, so OpenStep payloads are routed
    through ``plutil -convert json``.
    """
    try:
        return plistlib.loads(raw)
    except (plistlib.InvalidFileException, ValueError):
        pass
    return json.loads(await plist_to_json(raw))


class XcodeSimulatorDriver(BaseDeviceDriver):
    """One explicitly selected iOS simulator and one native interaction session.

    Android remains the default platform. This driver neither enables Xcode
    permissions nor controls physical devices. Unsupported Android operations
    report an error instead of returning a synthetic success.
    """

    def __init__(
        self,
        device_id: str = BOOTED_SIMULATOR_ID,
        width: int = 0,
        height: int = 0,
        *,
        workspace_path: str | Path | None = None,
    ):
        self._device_id = device_id
        self._width, self._height = width, height
        self._workspace_path = (
            Path(workspace_path).expanduser().resolve() if workspace_path is not None else None
        )
        self._scale: tuple[float, float] | None = None
        self._session_key: str | None = None
        self._bridge = XcodeBridge()
        self._connect_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._recorder: IosScreenRecorder | PhysicalIosRecorder | None = None

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def screen_size(self) -> tuple[int, int]:
        return self._width, self._height

    async def resolve_device(self) -> str:
        """Read-only validation and UDID selection, safe before the execution lease."""
        async with self._connect_lock:
            await self._resolve_device()
            return self._device_id

    async def _require_ios_host(self) -> None:
        """Validate the macOS/Xcode host requirements shared by all iOS drivers."""
        if sys.platform != "darwin":
            raise RuntimeError("iOS support requires macOS and Xcode 27 or later.")
        version = parse_xcode_version(await run_xcrun("xcodebuild", "-version"))
        if version is None or int(version.split(".")[0]) < 27:
            raise RuntimeError(
                "Native iOS interaction requires Xcode 27 or later. Set DEVELOPER_DIR to select it."
            )

    def _validate_workspace(self) -> None:
        if self._workspace_path is not None and (
            self._workspace_path.suffix.lower() not in {".xcodeproj", ".xcworkspace"}
            or not self._workspace_path.is_dir()
        ):
            raise ValueError(
                "iOS workspace must be an existing .xcodeproj or .xcworkspace directory."
            )

    async def _resolve_device(self) -> dict[str, Any]:
        self._validate_workspace()
        await self._require_ios_host()
        devices = json.loads(await run_xcrun("simctl", "list", "devices", "--json"))
        available = parse_simctl_devices(devices)
        if self._device_id.strip().lower() == BOOTED_SIMULATOR_ID:
            candidates = [device for device in available if device.get("state") == "Booted"]
            if len(candidates) != 1:
                raise ValueError(
                    "Select --device-serial <iOS simulator UDID>; 'booted' requires exactly one booted iOS simulator."
                )
        else:
            candidates = [
                device for device in available if device_matches_identifier(device, self._device_id)
            ]
            if len(candidates) > 1:
                udids = sorted(str(d.get("udid") or "?") for d in candidates)
                raise ValueError(
                    f"{len(candidates)} iOS simulators match {self._device_id!r} "
                    f"({', '.join(udids)}); use the simulator UDID instead."
                )
            if not candidates:
                raise ValueError(f"Unavailable iOS simulator UDID: {self._device_id}")
        self._device_id = candidates[0]["udid"]
        return candidates[0]

    async def _prepare_device(self, candidate: dict[str, Any]) -> None:
        """Bring the resolved device to an interactable state before the session."""
        if candidate.get("state") == "Shutdown":
            await run_xcrun("simctl", "boot", self._device_id)
            clear_ios_simulator_cache()
        await run_xcrun("simctl", "bootstatus", self._device_id, "-b", timeout=180.0)

    def _session_label(self) -> str:
        return f"Artemis Simulator {uuid4().hex[:8]}"

    def _validate_session_device(self, session: dict[str, Any]) -> None:
        if (
            not session.get("deviceIsSimulator")
            or str(session.get("deviceUUID") or "").lower() != self._device_id.lower()
        ):
            raise RuntimeError("Xcode selected a different device; refusing to interact.")

    async def _start_interaction_session(self) -> dict[str, Any]:
        start_arguments = {
            "deviceIdentifier": self._device_id,
            "sessionIdentifier": self._session_label(),
        }
        try:
            return await self._bridge.call("DeviceInteractionStartSession", start_arguments)
        except XcodeApprovalRequiredError:
            if self._workspace_path is None:
                raise
            if "XcodeOpenWorkspace" not in self._bridge.tools:
                raise XcodeApprovalRequiredError(
                    "DeviceInteractionStartSession",
                    "Xcode's XcodeOpenWorkspace tool is unavailable; "
                    "approve Artemis's access in Xcode.",
                )
            opened = await self._bridge.call(
                "XcodeOpenWorkspace", {"path": str(self._workspace_path)}
            )
            identifier = opened.get("workspaceIdentifier")
            if not isinstance(identifier, str) or not identifier:
                raise RuntimeError("XcodeOpenWorkspace returned no usable workspace identifier.")
            return await self._bridge.call("DeviceInteractionStartSession", start_arguments)
        except RuntimeError as error:
            # A crashed runner leaves its interaction session behind and Xcode
            # rejects newcomers with "already in use by ... key '<label>'". End
            # that orphan — the label works as the key — and retry once.
            match = _STALE_SESSION_RE.search(str(error))
            if match is not None:
                await self._bridge.call(
                    "DeviceInteractionEndSession",
                    {"interactionSessionKey": match.group(1)},
                )
                return await self._bridge.call("DeviceInteractionStartSession", start_arguments)
            if "currently in use or was recently used" in str(error):
                # Xcode keeps a recently-used identifier reserved briefly;
                # retry once with a fresh label after a short delay.
                await asyncio.sleep(1.0)
                start_arguments["sessionIdentifier"] = self._session_label()
                return await self._bridge.call(
                    "DeviceInteractionStartSession", start_arguments)
            raise

    async def connect(self) -> None:
        async with self._connect_lock:
            if self._session_key:
                if self._bridge.connected:
                    return
                # The bridge retires itself on tool-call failures; release the
                # orphaned native session before starting over.
                await self.disconnect()
            candidate = await self._resolve_device()
            await self._prepare_device(candidate)
            await self._bridge.start()
            required = {
                "DeviceInteractionStartSession",
                "DeviceInteractionSynthesize",
                "DeviceInteractionEndSession",
            }
            if not required.issubset(self._bridge.tools):
                await self._bridge.close()
                raise RuntimeError(
                    "Xcode's native device interaction tools are unavailable. Select Xcode 27 and approve Artemis's access in Xcode."
                )
            try:
                session = await self._start_interaction_session()
                self._session_key = session.get("interactionSessionKey")
                if not self._session_key:
                    raise RuntimeError("Xcode did not return a device interaction session key.")
                self._validate_session_device(session)
                await self.get_screen_data(skip_settling=True)
            except (
                OSError,
                ValueError,
                RuntimeError,
                TimeoutError,
                asyncio.CancelledError,
            ) as error:
                if isinstance(error, XcodeApprovalRequiredError):
                    error.workspace_path = self._workspace_path
                try:
                    await self.disconnect()
                except (Exception, asyncio.CancelledError) as cleanup_error:
                    logger.warning(
                        f"Could not release the Xcode session after a connection failure: {cleanup_error}",
                    )
                raise

    async def disconnect(self) -> None:
        try:
            if self._recorder is not None:
                session = self._recorder.session
                if session is not None:
                    if session.is_active:
                        try:
                            await self._recorder.stop()
                        except Exception as exc:
                            logger.error(
                                f"iOS recording finalization failed during disconnect: {exc}"
                            )
                    if get_active_session(self._device_id) is session:
                        remove_active_session(self._device_id)
        finally:
            async with self._operation_lock:
                key, self._session_key = self._session_key, None
                try:
                    if key:
                        # A timeout retires the old connection; cleanup may
                        # create a fresh bridge solely to close the known
                        # native session.
                        if not self._bridge.connected:
                            await self._bridge.start()
                        await self._bridge.call(
                            "DeviceInteractionEndSession",
                            {"interactionSessionKey": key},
                        )
                finally:
                    self._scale = None
                    await self._bridge.close()

    async def _synthesize(self, command: str = "", activation: str | None = None) -> dict[str, Any]:
        self._require_connected()
        arguments = {"interactSessionKey": self._session_key, "interactionCommand": command}
        if activation:
            arguments["activationBundleId"] = activation
        try:
            return await self._bridge.call("DeviceInteractionSynthesize", arguments)
        except RuntimeError as error:
            # Xcode drops idle interaction sessions, and a crashed mcpbridge
            # fails in-flight calls with "MCP connection failed". Both mean
            # the command never ran, so a session restart + retry cannot
            # double-execute it.
            message = str(error).casefold()
            transient = (
                "session not found" in message
                or "mcp connection failed" in message
                or "not connected" in message
                or "invalid screen scale" in message
            )
            if not transient:
                raise
            logger.warning(
                f"Xcode interaction channel dropped ({error}); reconnecting and retrying."
            )
            old_key, self._session_key = self._session_key, None
            if old_key and self._bridge.connected:
                try:
                    await self._bridge.call(
                        "DeviceInteractionEndSession",
                        {"interactionSessionKey": old_key},
                    )
                except RuntimeError:
                    pass
            await self._restart_interaction_session()
            arguments["interactSessionKey"] = self._session_key
            return await self._bridge.call("DeviceInteractionSynthesize", arguments)

    async def _restart_interaction_session(self) -> None:
        """Replace the dropped Xcode interaction session, respawning the bridge."""
        # Retry the whole reconnect: a respawned mcpbridge may report the old
        # session identifier "recently used" for a moment, and the bridge
        # itself can take a beat to accept stdio.
        last_error: RuntimeError | None = None
        for _ in range(3):
            try:
                if not self._bridge.connected:
                    await self._bridge.start()
                session = await self._start_interaction_session()
                key = session.get("interactionSessionKey")
                if not key:
                    raise RuntimeError(
                        "Xcode did not return a device interaction session key."
                    )
                self._validate_session_device(session)
                self._session_key = key
                logger.warning(
                    "Re-established the Xcode interaction session after Xcode dropped it."
                )
                return
            except RuntimeError as error:
                last_error = error
                logger.warning(f"Xcode interaction reconnect attempt failed: {error}")
                await asyncio.sleep(1.0)
        raise last_error or RuntimeError("Xcode interaction session could not be restarted.")

    def _require_connected(self) -> None:
        if not self._session_key or self._device_id.strip().lower() == BOOTED_SIMULATOR_ID:
            raise RuntimeError("Connect the iOS simulator driver before interacting.")

    async def _capture(self) -> ScreenData:
        result = await self._synthesize()
        image_path = result.get("screenshotPath")
        if not image_path:
            raise RuntimeError("Xcode returned no screenshot; no observation is available.")
        screenshot = await asyncio.to_thread(Path(image_path).read_bytes)
        with Image.open(BytesIO(screenshot)) as image:
            self._width, self._height = image.size
        hierarchy_path = result.get("hierarchyPath")
        if not hierarchy_path:
            self._scale = None
            raise RuntimeError(
                "Xcode returned no accessibility hierarchy. Recapture after the UI settles."
            )
        hierarchy = await asyncio.to_thread(Path(hierarchy_path).read_text, encoding="utf-8")
        self._scale = None
        elements, self._scale = parse_hierarchy(hierarchy, self._width, self._height)
        return ScreenData(
            screenshot_bytes=screenshot,
            screenshot_base64=base64.b64encode(screenshot).decode("ascii"),
            ui_elements=elements,
            width=self._width,
            height=self._height,
            platform="ios",
        )

    async def get_screen_data(self, skip_settling: bool = False) -> ScreenData:
        # Xcode captures after animations settle; an extra Android delay is unnecessary.
        async with self._operation_lock:
            return await self._capture()

    async def _capture_unchanged(self, action: str) -> ScreenData:
        """Capture, refusing to continue when the screen geometry changed."""
        previous_size = self.screen_size
        data = await self._capture()
        if self.screen_size != previous_size:
            raise ValueError(
                f"The iOS screen changed orientation or size. Observe it again before {action}."
            )
        return data

    def _scaled_point(self, x: int, y: int) -> tuple[float, float]:
        """Convert screenshot pixels to the driver's logical point space."""
        if self._scale is None:
            raise RuntimeError("Capture an iOS screen before coordinate interaction.")
        if not 0 <= x < self._width or not 0 <= y < self._height:
            raise ValueError("iOS input coordinates are outside the current screenshot.")
        return x / self._scale[0], y / self._scale[1]

    def _point(self, x: int, y: int) -> str:
        px, py = self._scaled_point(x, y)
        return f"{px:.4f} {py:.4f}"

    @staticmethod
    def _direction_points(direction: str, w: int, h: int) -> tuple[int, int, int, int]:
        points = {
            "up": (w // 2, h * 3 // 4, w // 2, h // 4),
            "down": (w // 2, h // 4, w // 2, h * 3 // 4),
            "left": (w * 3 // 4, h // 2, w // 4, h // 2),
            "right": (w // 4, h // 2, w * 3 // 4, h // 2),
        }
        return points[direction]

    @staticmethod
    def _activation_for(data: ScreenData, x: int, y: int) -> str | None:
        """Native overlapping-app elements require activation before input."""
        candidates = []
        for element in data.ui_elements:
            bundle = element.get("activation_bundle_id")
            bounds = element.get("parsed_bounds")
            if not bundle or not isinstance(bounds, dict):
                continue
            if bounds["left"] <= x < bounds["right"] and bounds["top"] <= y < bounds["bottom"]:
                area = (bounds["right"] - bounds["left"]) * (bounds["bottom"] - bounds["top"])
                candidates.append((area, bundle))
        if not candidates:
            return None
        minimum = min(area for area, _ in candidates)
        bundles = {bundle for area, bundle in candidates if area == minimum}
        if len(bundles) != 1:
            raise ValueError(
                "The target overlaps multiple iOS applications; activate the intended app first."
            )
        return bundles.pop()

    async def tap(
        self, x: int, y: int, duration_ms: int = 100, times: int = 1, delay_ms: int = 100
    ) -> bool:
        if times < 1 or duration_ms < 0 or delay_ms < 0:
            raise ValueError("Tap count must be positive and durations nonnegative.")
        async with self._operation_lock:
            data = await self._capture_unchanged("tapping")
            command = f"t {self._point(x, y)} {duration_ms / 1000:.3f}"
            for index in range(times):
                await self._synthesize(command, activation=self._activation_for(data, x, y))
                if index < times - 1:
                    await asyncio.sleep(delay_ms / 1000)
            return True

    async def long_press(self, x: int, y: int, duration_ms: int = 1000) -> bool:
        return await self.tap(x, y, duration_ms=duration_ms)

    async def swipe(
        self, start_x: int, start_y: int, end_x: int, end_y: int, duration_ms: int = 800
    ) -> bool:
        if duration_ms <= 0:
            raise ValueError("Swipe duration must be positive.")
        async with self._operation_lock:
            data = await self._capture_unchanged("swiping")
            await self._synthesize(
                f"t {self._point(start_x, start_y)} f {self._point(end_x, end_y)} {duration_ms / 1000:.3f}",
                activation=self._activation_for(data, start_x, start_y),
            )
            return True

    async def swipe_direction(
        self,
        direction: SwipeDirection | Literal["up", "down", "left", "right"],
        duration_ms: int = 800,
    ) -> bool:
        direction = SwipeDirection(direction).value
        if duration_ms <= 0:
            raise ValueError("Swipe duration must be positive.")
        async with self._operation_lock:
            # swipe_direction is rotation-tolerant by design: recapture and
            # compute from the *current* size rather than refusing like tap.
            data = await self._capture()
            sx, sy, ex, ey = self._direction_points(direction, *self.screen_size)
            await self._synthesize(
                f"t {self._point(sx, sy)} f {self._point(ex, ey)} {duration_ms / 1000:.3f}",
                activation=self._activation_for(data, sx, sy),
            )
            return True

    async def input_text(self, text: str, clear_existing: bool = True) -> bool:
        if clear_existing:
            raise NotImplementedError(
                "Xcode has no verified replace-text operation. Use clear_existing=False to append, or clear the field through its UI."
            )
        # Encode every code point: literal native escape sequences cannot become
        # unintended control characters, and newlines/Unicode retain their value.
        escaped = "".join(f"\\u{{{ord(char):04X}}}" for char in text)
        async with self._operation_lock:
            await self._synthesize(f"sender keyboard kbd {escaped}")
        return True

    async def press_key(self, key: KeyCode | str | int) -> bool:
        key = key.value if isinstance(key, KeyCode) else str(key).lower()
        commands = {
            "home": "b h",
            "power": "b p",
            "volume_up": "b u",
            "volume_down": "b d",
            "app_switch": "b h b h",
            "enter": r"sender keyboard kbd \u{000A}",
        }
        if key not in commands:
            raise NotImplementedError(f"Key {key!r} is not supported by the native iOS driver.")
        async with self._operation_lock:
            await self._synthesize(commands[key])
        return True

    async def launch_app(self, package_name: str) -> bool:
        async with self._operation_lock:
            self._require_connected()
            await run_xcrun("simctl", "launch", self._device_id, package_name)
        return True

    async def stop_app(self, package_name: str) -> bool:
        async with self._operation_lock:
            self._require_connected()
            await run_xcrun("simctl", "terminate", self._device_id, package_name)
        return True

    async def install_app(self, app_path: Path) -> str:
        self._require_connected()
        path = app_path.expanduser().resolve()
        if path.suffix != ".app" or not path.is_dir():
            raise ValueError("iOS installation requires a simulator-built .app directory.")
        with (path / "Info.plist").open("rb") as stream:
            bundle = plistlib.load(stream).get("CFBundleIdentifier")
        if not isinstance(bundle, str) or not bundle:
            raise ValueError("The .app has no CFBundleIdentifier in Info.plist.")
        async with self._operation_lock:
            self._require_connected()
            await run_xcrun("simctl", "install", self._device_id, str(path), timeout=120)
        return bundle

    async def list_apps(self) -> dict[str, str]:
        async with self._operation_lock:
            self._require_connected()
            data = await _parse_listapps_output(
                await run_xcrun("simctl", "listapps", self._device_id)
            )
        return {
            bundle: info.get("CFBundleDisplayName") or info.get("CFBundleName") or bundle
            for bundle, info in data.items()
        }

    async def open_url(self, url: str) -> bool:
        async with self._operation_lock:
            self._require_connected()
            await run_xcrun("simctl", "openurl", self._device_id, url)
        return True

    async def get_current_package(self) -> str | None:
        async with self._operation_lock:
            result = await self._synthesize()
            path = result.get("hierarchyPath")
            if not path:
                return None
            hierarchy = await asyncio.to_thread(Path(path).read_text, encoding="utf-8")
            return application_bundle(hierarchy)

    async def find_element(
        self, query: ElementQuery, screen_data: ScreenData | None = None
    ) -> tuple[dict[str, Any] | None, list[int] | None, str | None]:
        element, center, error = await super().find_element(query, screen_data)
        if element is not None:
            center = element.get("hit_point", center)
        return element, center, error

    async def tap_element(
        self, query: ElementQuery, long_press: bool = False, duration_ms: int = 1000
    ) -> bool:
        async with self._operation_lock:
            data = await self._capture()
            element, center, error = await self.find_element(query, data)
            if error or element is None or center is None:
                return False
            await self._synthesize(
                f"t {self._point(*center)} {duration_ms / 1000 if long_press else 0.1:.3f}",
                activation=element.get("activation_bundle_id"),
            )
            return True

    async def execute_shell(self, command: str, timeout_seconds: float = 15.0) -> str:
        raise NotImplementedError("Android shell commands are unavailable on iOS Simulator.")

    # --- Recording (native segmented capture) ---

    def _new_recorder(self) -> IosScreenRecorder | PhysicalIosRecorder:
        """Recorder implementation for this driver (sim: simctl recordVideo)."""
        return IosScreenRecorder(self._device_id)

    @property
    def recording_session(self) -> IosRecordingSession | None:
        """Latest recording session, retained for error reporting after stop."""
        if self._recorder is None:
            return None
        return self._recorder.session

    async def start_video_recording(
        self,
        output_dir: Path | None = None,
        max_duration_seconds: int = DEFAULT_MAX_DURATION_SECONDS,
    ) -> None:
        self._require_connected()
        if not self._device_id:
            raise RuntimeError("iOS recording requires a pinned device UDID")
        if self._recorder is None:
            self._recorder = self._new_recorder()
        await self._recorder.start(output_dir, max_duration_seconds)

    async def seal_recording_segment(self, through_time: float | None = None) -> None:
        """Seal the current segment so its final MP4 can be read safely."""
        if self._recorder is not None:
            await self._recorder.seal(through_time)

    async def stop_video_recording(self) -> str | None:
        if self._recorder is None:
            return None
        path = await self._recorder.stop()
        return str(path) if path is not None else None
