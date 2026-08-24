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

"""Android ADB & Device Connectivity Probe with AVD Detection."""

import asyncio
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

from artemis.core.diagnostics.probes.base import BaseProbe
from artemis.core.diagnostics.schema import (
    DeviceInfo,
    ProbeAction,
    ProbeCategory,
    ProbeResult,
    ProbeStatus,
)
from artemis.platform import OSType, platform
from artemis.toolchain import toolchain
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


class AdbDeviceProbe(BaseProbe):
    """Deep inspection probe for Android Debug Bridge, connected mobile devices, and local AVD emulators."""

    def __init__(self, target_serial: str | None = None):
        self._target_serial = target_serial

    @property
    def probe_id(self) -> str:
        return "android_adb"

    @property
    def category(self) -> ProbeCategory:
        return ProbeCategory.DEVICE

    @property
    def is_blocker(self) -> bool:
        return True

    def set_target_serial(self, serial: str | None) -> None:
        """Set or update preferred active device serial."""
        self._target_serial = serial

    def _locate_adb(self) -> str | None:
        """Find the adb binary path from ToolchainResolver."""
        return toolchain.resolve("adb", force_refresh=True)

    def _locate_emulator(self) -> str | None:
        """Find the emulator binary path from PATH or standard SDK environments."""
        emu_path = shutil.which("emulator")
        if emu_path:
            return emu_path

        sdk_candidates = [
            os.environ.get("ANDROID_HOME"),
            os.environ.get("ANDROID_SDK_ROOT"),
            str(Path.home() / "Android" / "Sdk"),
            str(Path.home() / "Android" / "sdk"),
            str(Path.home() / "Library" / "Android" / "sdk"),
            os.getenv("LOCALAPPDATA", "") + "/Android/Sdk" if os.getenv("LOCALAPPDATA") else None,
            "/usr/lib/android-sdk",
            "/opt/android-sdk",
        ]
        for base in sdk_candidates:
            if base:
                cand = (
                    Path(base)
                    / "emulator"
                    / ("emulator.exe" if platform.os_type == OSType.WINDOWS else "emulator")
                )
                if cand.is_file():
                    return str(cand)
        return None

    def _list_installed_avds(self, emulator_path: str | None = None) -> list[str]:
        """Discover all locally created Android Virtual Devices (AVDs)."""
        avd_names: list[str] = []

        # Method 1: Scan ~/.android/avd/*.ini
        avd_dir = Path.home() / ".android" / "avd"
        if avd_dir.is_dir():
            for ini_file in avd_dir.glob("*.ini"):
                name = ini_file.stem
                if name and name not in avd_names:
                    avd_names.append(name)

        return avd_names

    async def _get_adb_version(self, adb_path: str) -> str:
        """Query ADB version string."""
        try:
            proc = await asyncio.create_subprocess_exec(
                adb_path,
                "version",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if stdout:
                first_line = stdout.decode(errors="replace").strip().splitlines()[0]
                return first_line
        except Exception as e:
            logger.debug(f"Failed to read adb version: {e}")
        return "Installed"

    @staticmethod
    def _parse_device_lock_state(policy_output: str, trust_output: str) -> bool | None:
        """Parse Android Keyguard state from dumpsys output.

        ``KeyguardServiceDelegate.showing`` also catches swipe-only lock screens,
        while Trust's current-user ``deviceLocked`` covers secure locks and
        screen-off transitions. Any positive signal wins so an occluded lock
        screen cannot accidentally be treated as ready.
        """
        policy_showing: bool | None = None
        policy_lines = policy_output.splitlines()
        for index, raw_line in enumerate(policy_lines):
            if raw_line.strip() != "KeyguardServiceDelegate":
                continue
            for delegate_line in policy_lines[index + 1 : index + 16]:
                match = re.fullmatch(
                    r"showing\s*=\s*(true|false|1|0)",
                    delegate_line.strip(),
                    flags=re.IGNORECASE,
                )
                if match:
                    policy_showing = match.group(1).lower() in {"true", "1"}
                    break
            break

        # Older Android releases expose differently named policy fields.
        legacy_matches = re.findall(
            r"\b(?:mShowingLockscreen|mKeyguardShowing|keyguardShowing|"
            r"isKeyguardLocked|showingAndNotOccluded)\s*=\s*(true|false|1|0)",
            policy_output,
            flags=re.IGNORECASE,
        )
        legacy_states = [value.lower() in {"true", "1"} for value in legacy_matches]

        trust_match = re.search(
            r"^.*\(current\).*?\bdeviceLocked\s*=\s*(true|false|1|0)\b",
            trust_output,
            flags=re.IGNORECASE | re.MULTILINE,
        )
        if trust_match is None:
            trust_match = re.search(
                r"\bdeviceLocked\s*=\s*(true|false|1|0)\b",
                trust_output,
                flags=re.IGNORECASE,
            )
        trust_locked = (
            trust_match.group(1).lower() in {"true", "1"} if trust_match else None
        )

        states = [state for state in [policy_showing, trust_locked, *legacy_states] if state is not None]
        if any(states):
            return True
        if states:
            return False
        return None

    async def _get_device_lock_state(
        self,
        adb_path: str,
        serial: str,
        timeout_seconds: float = 2.0,
    ) -> bool | None:
        """Query Keyguard state without changing or waking the target device."""

        async def run_dumpsys(*service_args: str) -> str:
            proc = await asyncio.create_subprocess_exec(
                adb_path,
                "-s",
                serial,
                "shell",
                "dumpsys",
                *service_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                stdout, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_seconds
                )
            except TimeoutError:
                proc.kill()
                await proc.communicate()
                return ""
            if proc.returncode != 0:
                return ""
            return stdout.decode(errors="replace")

        try:
            policy_output, trust_output = await asyncio.gather(
                run_dumpsys("window", "policy"),
                run_dumpsys("trust"),
            )
            return self._parse_device_lock_state(policy_output, trust_output)
        except Exception as exc:
            logger.debug(f"Failed to query lock state for {serial}: {exc}")
            return None

    async def _get_device_states(
        self,
        adb_path: str,
        timeout_seconds: float = 1.0,
    ) -> list[tuple[str, str]] | None:
        """Return only ADB serial/state pairs for the submission fast path.

        ``None`` means the command could not be completed within the bounded
        submission window. An empty list means ADB responded with no devices.
        """
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                adb_path,
                "devices",
                "-l",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds
            )
            if proc.returncode != 0:
                return None

            states: list[tuple[str, str]] = []
            lines = stdout.decode(errors="replace").splitlines()
            for raw_line in lines[1:]:
                parts = raw_line.strip().split()
                if len(parts) >= 2:
                    states.append((parts[0], parts[1]))
            return states
        except TimeoutError:
            if proc is not None:
                proc.kill()
                await proc.communicate()
            return None
        except Exception as exc:
            logger.debug(f"Failed submission-time ADB device check: {exc}")
            return None

    async def probe_submission_readiness(self) -> ProbeResult:
        """Run the minimal fail-safe device check required before enqueueing.

        The full diagnostics probe enriches device metadata, scans packages,
        and discovers emulators. None of that is needed to reject a locked
        device at submission time, so this path only checks ADB connectivity
        and Keyguard state with strict time bounds.
        """
        adb_path = toolchain.resolve("adb")
        if not adb_path:
            return ProbeResult(
                id=self.probe_id,
                category=self.category,
                title="Device / Emulator Connected",
                status=ProbeStatus.FAIL,
                is_blocker=self.is_blocker,
                summary="ADB Not Found",
                description="Android Debug Bridge (adb) is not installed or not in PATH.",
                metadata={"installed": False, "submission_probe": True},
            )

        device_states = await self._get_device_states(adb_path)
        if device_states is None:
            return ProbeResult(
                id=self.probe_id,
                category=self.category,
                title="Device / Emulator Connected",
                status=ProbeStatus.WARN,
                is_blocker=self.is_blocker,
                summary="Lock State Unknown",
                description=(
                    "ADB did not respond quickly enough to verify the Android lock-screen state."
                ),
                metadata={"installed": True, "submission_probe": True},
            )

        ready_serials = [serial for serial, state in device_states if state == "device"]
        if not ready_serials:
            states = {state for _, state in device_states}
            summary = "Device Unauthorized" if "unauthorized" in states else "No Device Found"
            return ProbeResult(
                id=self.probe_id,
                category=self.category,
                title="Device / Emulator Connected",
                status=ProbeStatus.WARN,
                is_blocker=self.is_blocker,
                summary=summary,
                description="No authorized Android device is currently ready.",
                metadata={
                    "installed": True,
                    "submission_probe": True,
                    "devices": [
                        {"serial": serial, "state": state}
                        for serial, state in device_states
                    ],
                },
            )

        serial = (
            self._target_serial
            if self._target_serial in ready_serials
            else ready_serials[0]
        )
        is_locked = await self._get_device_lock_state(
            adb_path,
            serial,
            timeout_seconds=1.0,
        )
        summary = (
            "Device Locked"
            if is_locked is True
            else "Connected"
            if is_locked is False
            else "Lock State Unknown"
        )
        status = ProbeStatus.PASS if is_locked is False else ProbeStatus.WARN
        description = (
            f"ADB connected to {serial}. Ready for task submission."
            if is_locked is False
            else f"Android device {serial} is locked. Unlock it before running a task."
            if is_locked is True
            else f"Android lock-screen state could not be verified for {serial}."
        )
        return ProbeResult(
            id=self.probe_id,
            category=self.category,
            title="Device / Emulator Connected",
            status=status,
            is_blocker=self.is_blocker,
            summary=summary,
            description=description,
            metadata={
                "installed": True,
                "submission_probe": True,
                "active_device": {"serial": serial, "is_locked": is_locked},
            },
        )

    async def _parse_adb_devices(self, adb_path: str) -> list[DeviceInfo]:
        """Execute `adb devices -l` and extract structured device metadata."""
        devices: list[DeviceInfo] = []
        try:
            proc = await asyncio.create_subprocess_exec(
                adb_path,
                "devices",
                "-l",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0 or not stdout:
                return devices

            lines = [
                line.strip()
                for line in stdout.decode(errors="replace").splitlines()
                if line.strip()
            ]
            for line in lines[1:]:  # Skip "List of devices attached"
                parts = line.split()
                if len(parts) < 2:
                    continue

                serial = parts[0]
                state = parts[1]
                extra_tokens = parts[2:]

                model = None
                product = None
                for token in extra_tokens:
                    if token.startswith("model:"):
                        model = token.split(":", 1)[1].replace("_", " ")
                    elif token.startswith("product:"):
                        product = token.split(":", 1)[1]

                is_emulator = (
                    serial.startswith("emulator-") or "127.0.0.1" in serial or "localhost" in serial
                )
                if is_emulator and not model:
                    model = "Android Emulator"

                device_info = DeviceInfo(
                    serial=serial,
                    state=state,
                    model=model,
                    product=product,
                    is_emulator=is_emulator,
                )
                devices.append(device_info)

        except Exception as e:
            logger.error(f"Error querying adb devices: {e}")

        # For authorized devices, attempt non-blocking quick property enrichment
        for dev in devices:
            if dev.state == "device":
                try:
                    p_prop = await asyncio.create_subprocess_exec(
                        adb_path,
                        "-s",
                        dev.serial,
                        "shell",
                        "getprop ro.build.version.release",
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    p_size = await asyncio.create_subprocess_exec(
                        adb_path,
                        "-s",
                        dev.serial,
                        "shell",
                        "wm size",
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    lock_state_task = asyncio.create_task(
                        self._get_device_lock_state(adb_path, dev.serial)
                    )
                    out_prop, _ = await p_prop.communicate()
                    out_size, _ = await p_size.communicate()
                    dev.is_locked = await lock_state_task

                    if out_prop:
                        dev.android_version = out_prop.decode(errors="replace").strip()
                    if out_size:
                        size_str = out_size.decode(errors="replace").strip()
                        if "Physical size:" in size_str:
                            dev.screen_resolution = size_str.split("Physical size:")[-1].strip()

                    # Query installed application packages with timeout protection
                    try:
                        p_pkg = await asyncio.create_subprocess_exec(
                            adb_path,
                            "-s",
                            dev.serial,
                            "shell",
                            "pm list packages",
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                        )
                        out_pkg, _ = await asyncio.wait_for(p_pkg.communicate(), timeout=2.5)
                        if out_pkg:
                            pkgs = [
                                line.split("package:", 1)[1].strip()
                                for line in out_pkg.decode(errors="replace").splitlines()
                                if line.startswith("package:")
                                and line.split("package:", 1)[1].strip()
                            ]
                            dev.installed_packages = pkgs
                    except Exception as e_pkg:
                        logger.debug(f"Failed to query packages for {dev.serial}: {e_pkg}")
                except Exception:
                    pass

        return devices

    async def probe(self) -> ProbeResult:
        """Run complete ADB, device, and local AVD connectivity probe."""
        adb_path = self._locate_adb()
        emulator_path = self._locate_emulator()
        installed_avds = self._list_installed_avds(emulator_path)

        # 1. ADB binary check
        if not adb_path:
            actions = []
            if platform.os_type == OSType.WINDOWS:
                actions.append(
                    ProbeAction(
                        action_type="command",
                        label="Install via WinGet",
                        payload="winget install Google.PlatformTools",
                    )
                )
                actions.append(
                    ProbeAction(
                        action_type="command",
                        label="Run PowerShell Setup",
                        payload="powershell -ExecutionPolicy Bypass -File scripts/install_deps.ps1",
                    )
                )
            elif platform.os_type == OSType.MACOS:
                actions.append(
                    ProbeAction(
                        action_type="command",
                        label="Install via Homebrew",
                        payload="brew install android-platform-tools",
                    )
                )
            else:
                actions.append(
                    ProbeAction(
                        action_type="command",
                        label="Linux Quick Install",
                        payload="sudo apt-get install -y adb",
                    )
                )
                actions.append(
                    ProbeAction(
                        action_type="command",
                        label="One-Click Install All",
                        payload="bash scripts/install_deps.sh",
                    )
                )

            actions.append(
                ProbeAction(
                    action_type="link",
                    label="Download Android Platform Tools",
                    payload="https://developer.android.com/tools/releases/platform-tools",
                )
            )

            return ProbeResult(
                id=self.probe_id,
                category=self.category,
                title="Device / Emulator Connected",
                status=ProbeStatus.FAIL,
                is_blocker=self.is_blocker,
                summary="ADB Not Found",
                description="Android Debug Bridge (adb) is not installed or not in PATH.",
                metadata={"installed": False},
                actions=actions,
            )

        adb_ver = await self._get_adb_version(adb_path)
        devices = await self._parse_adb_devices(adb_path)

        # Inspect ADB authentication key health
        from artemis.core.diagnostics.adb_keys import inspect_adb_keys

        key_status = inspect_adb_keys()

        # Normalize emulator command display
        is_emu_in_path = shutil.which("emulator") is not None
        emu_display_cmd = "emulator" if is_emu_in_path else (emulator_path or "emulator")

        metadata: dict[str, Any] = {
            "installed": True,
            "adb_path": adb_path,
            "adb_version": adb_ver,
            "adb_keys": key_status.to_dict(),
            "emulator_path": emulator_path,
            "is_emulator_in_path": is_emu_in_path,
            "installed_avds": installed_avds,
            "device_count": len(devices),
            "devices": [d.model_dump() for d in devices],
        }

        # 2. Case: No devices found
        if not devices:
            actions: list[ProbeAction] = []

            if key_status.is_corrupted:
                actions.append(
                    ProbeAction(
                        action_type="command",
                        label="Auto-Heal ADB Keys",
                        payload="artemis doctor --fix",
                    )
                )

            # If user has installed AVDs, generate exact launch commands for each
            if installed_avds:
                for avd in installed_avds:
                    actions.append(
                        ProbeAction(
                            action_type="command",
                            label=f"Launch {avd}",
                            payload=f"{emu_display_cmd} -avd {avd}",
                        )
                    )
            else:
                actions.append(
                    ProbeAction(
                        action_type="command",
                        label="Start Default Emulator",
                        payload=f"{emu_display_cmd} -avd Pixel_8_API_34",
                    )
                )

            actions.append(
                ProbeAction(
                    action_type="hint",
                    label="Connect via USB",
                    payload="Connect your Android phone via USB cable and enable Developer Options -> USB Debugging.",
                )
            )

            desc = "ADB is ready, but no active Android device or emulator was detected."
            if key_status.is_corrupted:
                desc += f" Warning: Corrupted ADB RSA key detected ({key_status.error_reason})."
            if installed_avds:
                desc += f" Found {len(installed_avds)} installed emulator(s): {', '.join(installed_avds)}."

            return ProbeResult(
                id=self.probe_id,
                category=self.category,
                title="Device / Emulator Connected",
                status=ProbeStatus.WARN,
                is_blocker=self.is_blocker,
                summary="No Device Found",
                description=desc,
                metadata=metadata,
                actions=actions,
            )

        # 3. Check for authorized and ready devices
        ready_devices = [d for d in devices if d.state == "device"]
        unauthorized_devices = [d for d in devices if d.state == "unauthorized"]

        if not ready_devices:
            if unauthorized_devices:
                unauth_serial = unauthorized_devices[0].serial
                if key_status.is_corrupted:
                    return ProbeResult(
                        id=self.probe_id,
                        category=self.category,
                        title="Device / Emulator Connected",
                        status=ProbeStatus.WARN,
                        is_blocker=self.is_blocker,
                        summary="ADB Key Corrupted",
                        description=(
                            f"Device detected ({unauth_serial}), but ADB authentication keys are corrupted "
                            f"({key_status.error_reason}). This prevents the device from displaying the USB Debugging prompt."
                        ),
                        metadata=metadata,
                        actions=[
                            ProbeAction(
                                action_type="command",
                                label="Auto-Heal ADB Keys",
                                payload="artemis doctor --fix",
                            ),
                            ProbeAction(
                                action_type="command",
                                label="Restart ADB Server",
                                payload=f"{adb_path} kill-server && {adb_path} start-server",
                            ),
                        ],
                    )

                return ProbeResult(
                    id=self.probe_id,
                    category=self.category,
                    title="Device / Emulator Connected",
                    status=ProbeStatus.WARN,
                    is_blocker=self.is_blocker,
                    summary="Device Unauthorized",
                    description=f"Device detected ({unauth_serial}), but USB debugging is not yet authorized.",
                    metadata=metadata,
                    actions=[
                        ProbeAction(
                            action_type="hint",
                            label="Authorize USB Debugging",
                            payload="Please unlock your Android phone and tap 'Allow' on the USB Debugging permission prompt.",
                        ),
                        ProbeAction(
                            action_type="command",
                            label="Restart ADB Server",
                            payload=f"{adb_path} kill-server && {adb_path} start-server",
                        ),
                    ],
                )
            else:
                first_serial = devices[0].serial
                first_state = devices[0].state
                return ProbeResult(
                    id=self.probe_id,
                    category=self.category,
                    title="Device / Emulator Connected",
                    status=ProbeStatus.WARN,
                    is_blocker=self.is_blocker,
                    summary="Device Booting",
                    description=f"Device detected ({first_serial}, state: {first_state}). It is currently booting up or offline. Please wait a few seconds...",
                    metadata=metadata,
                    actions=[
                        ProbeAction(
                            action_type="hint",
                            label="Device Booting",
                            payload=f"Device {first_serial} is initializing. It will be ready automatically in a few seconds.",
                        )
                    ],
                )

        # 4. Ready device available
        active_dev = ready_devices[0]
        if self._target_serial:
            matched = next((d for d in ready_devices if d.serial == self._target_serial), None)
            if matched:
                active_dev = matched

        display_name = active_dev.model or active_dev.serial
        if active_dev.screen_resolution:
            display_name = f"{display_name} ({active_dev.screen_resolution})"

        metadata["active_device"] = active_dev.model_dump()

        if active_dev.is_locked is not False:
            is_locked = active_dev.is_locked is True
            summary = "Device Locked" if is_locked else "Lock State Unknown"
            description = (
                f"ADB connected to {display_name}, but the Android lock screen is active. "
                "Unlock the device and enter the home screen before running a task."
                if is_locked
                else f"ADB connected to {display_name}, but Android lock-screen state could not "
                "be verified. Keep the device unlocked on the home screen and check again."
            )
            return ProbeResult(
                id=self.probe_id,
                category=self.category,
                title="Device / Emulator Connected",
                status=ProbeStatus.WARN,
                is_blocker=self.is_blocker,
                summary=summary,
                description=description,
                metadata=metadata,
                actions=[
                    ProbeAction(
                        action_type="hint",
                        label="Unlock Device" if is_locked else "Verify Device Screen",
                        payload=(
                            "Unlock the Android device and leave it on the home screen. "
                            "Readiness is checked automatically every few seconds."
                        ),
                    )
                ],
            )

        return ProbeResult(
            id=self.probe_id,
            category=self.category,
            title="Device / Emulator Connected",
            status=ProbeStatus.PASS,
            is_blocker=self.is_blocker,
            summary="Connected",
            description=f"ADB connected to {display_name}. Ready for perception and touch automation.",
            metadata=metadata,
            actions=[
                ProbeAction(
                    action_type="hint",
                    label="Device Ready",
                    payload=f"Serial: {active_dev.serial} | Android OS: {active_dev.android_version or 'Unknown'}",
                )
            ],
        )
