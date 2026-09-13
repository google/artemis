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
import time
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

    _ENRICHMENT_CACHE_TTL_SECONDS = 60.0
    _LAST_LOCK_STATE_TTL_SECONDS = 15.0

    def __init__(self, target_serial: str | None = None):
        self._target_serial = target_serial
        self._device_enrichment_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._last_lock_states: dict[str, tuple[float, bool]] = {}
        self._lock_state_sources: dict[str, str] = {}

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

    def invalidate_enrichment_cache(self) -> None:
        """Refresh static device metadata on an explicit full re-check."""
        self._device_enrichment_cache.clear()

    def _locate_adb(self) -> str | None:
        """Find the adb binary path from ToolchainResolver."""
        return toolchain.resolve("adb")

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
        trust_locked = trust_match.group(1).lower() in {"true", "1"} if trust_match else None

        # Prefer the modern, current-user signals. Legacy fields can coexist in
        # dumpsys output as stale or display-specific diagnostics and must not
        # override an explicit modern unlocked result.
        modern_states = [state for state in [policy_showing, trust_locked] if state is not None]
        if any(modern_states):
            return True
        if modern_states:
            return False
        if any(legacy_states):
            return True
        if legacy_states:
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
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
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

    async def _get_confirmed_device_lock_state(
        self,
        adb_path: str,
        serial: str,
        timeout_seconds: float = 2.0,
    ) -> bool | None:
        """Confirm a positive lock result so one transient sample cannot flap the UI."""
        first = await self._get_device_lock_state(adb_path, serial, timeout_seconds=timeout_seconds)
        if first is not True:
            return first

        second = await self._get_device_lock_state(
            adb_path, serial, timeout_seconds=timeout_seconds
        )
        if second is True:
            return True
        if second is False:
            return False
        return None

    async def _get_dashboard_lock_state(
        self,
        adb_path: str,
        serial: str,
        timeout_seconds: float = 2.0,
    ) -> bool | None:
        """Use a recent confirmed state when one dashboard sample times out.

        This smoothing is intentionally dashboard-only. Task submission uses the
        fresh confirmed probe and fails closed when the state is unknown.
        """
        observed = await self._get_confirmed_device_lock_state(
            adb_path, serial, timeout_seconds=timeout_seconds
        )
        now = time.monotonic()
        if observed is not None:
            self._last_lock_states[serial] = (now, observed)
            self._lock_state_sources[serial] = "observed"
            return observed

        previous = self._last_lock_states.get(serial)
        if previous and now - previous[0] <= self._LAST_LOCK_STATE_TTL_SECONDS:
            self._lock_state_sources[serial] = "recent_confirmed"
            return previous[1]

        self._lock_state_sources[serial] = "unknown"
        return None

    @staticmethod
    async def _run_adb_shell(
        adb_path: str,
        serial: str,
        *args: str,
        timeout_seconds: float = 2.5,
    ) -> str:
        """Run a bounded ADB shell command and always reap timed-out children."""
        proc = await asyncio.create_subprocess_exec(
            adb_path,
            "-s",
            serial,
            "shell",
            *args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            return ""
        if proc.returncode != 0:
            return ""
        return stdout.decode(errors="replace").strip()

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
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
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
                # The process may have exited between the timeout and the
                # kill (frequent under concurrent submissions on Windows).
                try:
                    proc.kill()
                    await proc.communicate()
                except (ProcessLookupError, OSError):
                    pass
            return None
        except Exception as exc:
            logger.debug(f"Failed submission-time ADB device check: {exc}")
            return None

    async def probe_submission_readiness(self, target_serial: str | None = None) -> ProbeResult:
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
                        {"serial": serial, "state": state} for serial, state in device_states
                    ],
                },
            )

        preferred = (
            target_serial
            if target_serial and target_serial in ready_serials
            else self._target_serial
            if self._target_serial in ready_serials
            else ready_serials[0]
        )

        serial = preferred
        is_locked = await self._get_confirmed_device_lock_state(
            adb_path,
            serial,
            timeout_seconds=1.0,
        )

        # If preferred device is locked or unknown, check remaining ready devices for an unlocked one
        if is_locked is not False and len(ready_serials) > 1:
            for candidate in ready_serials:
                if candidate == preferred:
                    continue
                candidate_locked = await self._get_confirmed_device_lock_state(
                    adb_path,
                    candidate,
                    timeout_seconds=1.0,
                )
                if candidate_locked is False:
                    logger.info(
                        f"Device {preferred} is not ready/unlocked ({is_locked}); "
                        f"switching to unlocked ready device {candidate}"
                    )
                    serial = candidate
                    is_locked = False
                    break
                elif candidate_locked is None and is_locked is True:
                    serial = candidate
                    is_locked = None
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

        # Lock state stays live, while static device properties and the installed
        # package inventory are cached to keep frequent dashboard probes cheap.
        for dev in devices:
            if dev.state == "device":
                try:
                    lock_state_task = asyncio.create_task(
                        self._get_dashboard_lock_state(adb_path, dev.serial)
                    )
                    cached = self._device_enrichment_cache.get(dev.serial)
                    now = time.monotonic()
                    if cached and now - cached[0] <= self._ENRICHMENT_CACHE_TTL_SECONDS:
                        enrichment = cached[1]
                    else:
                        prop_task = self._run_adb_shell(
                            adb_path,
                            dev.serial,
                            "getprop",
                            "ro.build.version.release",
                        )
                        size_task = self._run_adb_shell(adb_path, dev.serial, "wm", "size")
                        packages_task = self._run_adb_shell(
                            adb_path,
                            dev.serial,
                            "pm",
                            "list",
                            "packages",
                        )
                        android_version, size_str, packages_output = await asyncio.gather(
                            prop_task, size_task, packages_task
                        )
                        enrichment = {
                            "android_version": android_version or None,
                            "screen_resolution": (
                                size_str.split("Physical size:")[-1].strip()
                                if "Physical size:" in size_str
                                else None
                            ),
                            "installed_packages": [
                                line.split("package:", 1)[1].strip()
                                for line in packages_output.splitlines()
                                if line.startswith("package:")
                                and line.split("package:", 1)[1].strip()
                            ],
                        }
                        self._device_enrichment_cache[dev.serial] = (now, enrichment)

                    dev.android_version = enrichment["android_version"]
                    dev.screen_resolution = enrichment["screen_resolution"]
                    dev.installed_packages = list(enrichment["installed_packages"])
                    dev.is_locked = await lock_state_task
                except Exception as exc:
                    logger.debug(f"Failed to enrich device {dev.serial}: {exc}")

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
        preferred_dev = None
        if self._target_serial:
            preferred_dev = next(
                (d for d in ready_devices if d.serial == self._target_serial), None
            )

        if preferred_dev and preferred_dev.is_locked is False:
            active_dev = preferred_dev
        else:
            # Prefer a confirmed unlocked ready device
            unlocked_dev = next((d for d in ready_devices if d.is_locked is False), None)
            if unlocked_dev:
                active_dev = unlocked_dev
            else:
                # If no confirmed unlocked device, prefer undetermined lock state over confirmed locked
                unknown_dev = next((d for d in ready_devices if d.is_locked is None), None)
                active_dev = preferred_dev or unknown_dev or ready_devices[0]

        display_name = active_dev.model or active_dev.serial
        if active_dev.screen_resolution:
            display_name = f"{display_name} ({active_dev.screen_resolution})"

        metadata["active_device"] = active_dev.model_dump()
        metadata["lock_state_source"] = self._lock_state_sources.get(active_dev.serial, "unknown")

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
