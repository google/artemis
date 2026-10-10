# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Shared iOS device enumeration: ``simctl`` for simulators, ``devicectl`` for physical.

Both the Xcode driver (target validation) and the runtime pools (discovery,
explicit-serial validation, auto-selection) read the same
``xcrun simctl list devices --json`` output through this module so parsing
stays in one place; paired physical hardware is enumerated through
``xcrun devicectl list devices``. Every function fails closed to
``None``/``[]`` on missing tooling so Android-only hosts never see iOS errors.
"""

import asyncio
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any

from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

SIMCTL_QUERY_TIMEOUT = 15.0

# ``simctl`` wildcard that resolves to whichever simulator is currently
# booted; also the public sentinel callers pass to mean "the booted one".
BOOTED_SIMULATOR_ID = "booted"

# ``simctl list devices`` / ``devicectl list devices`` take seconds on a busy
# host and every iOS consumer (readiness probe, device pool validation,
# /api/devices) enumerates them. Share one result briefly so polling UIs do
# not spawn back-to-back tool invocations.
_ENUMERATION_CACHE_TTL = 10.0


class _TtlCache:
    """Monotonic-TTL cache holding one enumeration result (or nothing)."""

    def __init__(self) -> None:
        self._devices: list[dict[str, Any]] | None = None
        self._time = 0.0

    def clear(self) -> None:
        self._devices = None
        self._time = 0.0

    def store(self, devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self._devices = devices
        self._time = time.monotonic()
        return devices

    def get(self) -> list[dict[str, Any]] | None:
        if self._devices is None:
            return None
        if time.monotonic() - self._time > _ENUMERATION_CACHE_TTL:
            return None
        return self._devices


_simulator_cache = _TtlCache()
_core_device_cache = _TtlCache()


def clear_ios_simulator_cache() -> None:
    """Drop the cached enumeration (e.g. after the driver boots a simulator)."""
    _simulator_cache.clear()


async def reap_process(process: asyncio.subprocess.Process) -> None:
    """Kill a still-running child and drain its pipes; never raises."""
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.communicate()


async def run_xcrun(*arguments: str, timeout: float = 30.0) -> bytes:
    """Run argv directly, reporting native errors and reaping cancelled children."""
    process = await asyncio.create_subprocess_exec(
        "xcrun",
        *arguments,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except (TimeoutError, asyncio.CancelledError):
        await reap_process(process)
        raise
    if process.returncode:
        raise RuntimeError(
            f"xcrun {' '.join(arguments[:3])} failed: {stderr.decode(errors='replace').strip()}"
        )
    return stdout


async def plist_to_json(payload: bytes, timeout: float = 30.0) -> bytes:
    """Convert an OpenStep/XML/binary plist payload to JSON via ``plutil``.

    ``simctl listapps`` emits OpenStep (ASCII) plists that ``plistlib``
    cannot read; ``plutil -convert json`` accepts every plist flavor.
    """
    process = await asyncio.create_subprocess_exec(
        "plutil",
        "-convert",
        "json",
        "-o",
        "-",
        "--",
        "-",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(input=payload), timeout)
    except (TimeoutError, asyncio.CancelledError):
        await reap_process(process)
        raise
    if process.returncode != 0:
        raise RuntimeError(f"plutil conversion failed: {stderr.decode(errors='replace').strip()}")
    return stdout


def simctl_available() -> bool:
    """Whether this host can enumerate iOS simulators at all."""
    return sys.platform == "darwin" and shutil.which("xcrun") is not None


def parse_xcode_version(payload: bytes | str) -> str | None:
    """Extract the Xcode version string from ``xcodebuild -version`` output."""
    text = payload.decode(errors="replace") if isinstance(payload, bytes) else payload
    match = re.search(r"Xcode\s+(\d+(?:\.\d+)*)", text)
    return match.group(1) if match else None


def parse_simctl_devices(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten ``simctl list devices --json`` into available iOS entries."""
    return [
        {
            "udid": device.get("udid"),
            "name": device.get("name"),
            "state": device.get("state"),
            "runtime": runtime,
        }
        for runtime, entries in payload.get("devices", {}).items()
        if ".iOS-" in runtime
        for device in entries
        if device.get("isAvailable") and device.get("udid")
    ]


async def list_ios_simulators(
    force_refresh: bool = False,
) -> list[dict[str, Any]] | None:
    """All available iOS simulators, or ``None`` when enumeration fails.

    Successful enumerations are cached for ``_ENUMERATION_CACHE_TTL`` seconds;
    failures are never cached so callers retry against live simctl.
    """
    if not simctl_available():
        return None
    if not force_refresh:
        cached = _simulator_cache.get()
        if cached is not None:
            return cached
    try:
        raw = await run_xcrun("simctl", "list", "devices", "--json", timeout=SIMCTL_QUERY_TIMEOUT)
    except (OSError, RuntimeError, TimeoutError) as exc:
        logger.debug(f"simctl device enumeration failed: {exc}")
        return None
    try:
        return _simulator_cache.store(parse_simctl_devices(json.loads(raw)))
    except (ValueError, TypeError) as exc:
        logger.debug(f"simctl device list parse failed: {exc}")
        return None


def list_ios_simulators_sync(
    force_refresh: bool = False,
) -> list[dict[str, Any]] | None:
    """Synchronous variant for non-async callers (e.g. replay device lists)."""
    if not simctl_available():
        return None
    if not force_refresh:
        cached = _simulator_cache.get()
        if cached is not None:
            return cached
    try:
        completed = subprocess.run(
            ["xcrun", "simctl", "list", "devices", "--json"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=SIMCTL_QUERY_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug(f"simctl device enumeration failed: {exc}")
        return None
    if completed.returncode != 0:
        return None
    try:
        return _simulator_cache.store(parse_simctl_devices(json.loads(completed.stdout)))
    except (ValueError, TypeError) as exc:
        logger.debug(f"simctl device list parse failed: {exc}")
        return None


# --- CoreDevice (physical iPhone/iPad) enumeration -------------------------

DEVICECTL_ENUMERATE_TIMEOUT = 20.0


async def devicectl_screenshot(device_id: str, destination: Path, timeout: float) -> None:
    """Capture one PNG frame from a paired device via ``devicectl``."""
    await run_xcrun(
        "devicectl",
        "device",
        "capture",
        "screenshot",
        "--device",
        device_id,
        "--destination",
        str(destination),
        timeout=timeout,
    )


def _device_property(device: dict[str, Any], section: str, key: str) -> Any:
    """Read a property across devicectl's current and deprecated JSON shapes.

    Xcode marks ``hardwareProperties``/``deviceProperties``/``connectionProperties``
    deprecated in favor of a nested ``properties`` dictionary; accept both.
    Sections may be present-but-null, so guard every hop.
    """
    parent = device.get(section)
    value = parent.get(key) if isinstance(parent, dict) else None
    if value is not None:
        return value
    properties = device.get("properties")
    if not isinstance(properties, dict):
        return None
    nested = properties.get(section)
    return nested.get(key) if isinstance(nested, dict) else None


def _modern_property(device: dict[str, Any], section: str, key: str) -> Any:
    """Read ``properties.<section>.<key>`` from Xcode 27's devicectl shape.

    The modern ``properties`` map uses short section names (``hardware``,
    ``software``, ``state``, ``connection``) rather than the deprecated
    ``hardwareProperties``/``deviceProperties``/``connectionProperties``
    spellings. Sections may be present-but-null, so guard every hop.
    """
    properties = device.get("properties")
    if not isinstance(properties, dict):
        return None
    nested = properties.get(section)
    return nested.get(key) if isinstance(nested, dict) else None


def _modern_os_version(device: dict[str, Any]) -> Any:
    """``properties.software.osVersionNumber`` may carry a stringValue map."""
    version = _modern_property(device, "software", "osVersionNumber")
    if isinstance(version, dict):
        return version.get("stringValue") or version.get("string")
    return version


def parse_devicectl_devices(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten ``devicectl list devices --json-output -`` into device entries."""
    devices = []
    result = payload.get("result")
    if not isinstance(result, dict):
        return devices
    entries = result.get("devices")
    if not isinstance(entries, list):
        return devices
    for device in entries:
        if not isinstance(device, dict):
            continue
        udid = (
            _device_property(device, "hardwareProperties", "udid")
            or _modern_property(device, "hardware", "udid")
            or device.get("identifier")
        )
        if not udid:
            continue
        devices.append(
            {
                "udid": udid,
                "name": _device_property(device, "deviceProperties", "name")
                or _modern_property(device, "state", "name"),
                "os_version": _device_property(device, "deviceProperties", "osVersionNumber")
                or _modern_os_version(device),
                "platform": _device_property(device, "hardwareProperties", "platform")
                or _modern_property(device, "hardware", "platform"),
                "reality": _device_property(device, "hardwareProperties", "reality")
                or _modern_property(device, "hardware", "reality"),
                "product_type": _device_property(device, "hardwareProperties", "productType")
                or _modern_property(device, "hardware", "productType"),
                "connection_state": _device_property(device, "connectionProperties", "tunnelState")
                or _modern_property(device, "connection", "state"),
                "pairing_state": _device_property(device, "connectionProperties", "pairingState")
                or _modern_property(device, "connection", "pairingState"),
                "visibility": device.get("visibilityClass")
                or _modern_property(device, "state", "visibilityClass"),
            }
        )
    return devices


async def list_core_devices(
    force_refresh: bool = False,
) -> list[dict[str, Any]] | None:
    """All devices known to CoreDevice (physical and simulated), cached briefly."""
    if not simctl_available():
        return None
    if not force_refresh:
        cached = _core_device_cache.get()
        if cached is not None:
            return cached
    try:
        raw = await run_xcrun(
            "devicectl",
            "list",
            "devices",
            "--json-output",
            "-",
            timeout=DEVICECTL_ENUMERATE_TIMEOUT,
        )
    except (OSError, RuntimeError, TimeoutError) as exc:
        logger.debug(f"devicectl device enumeration failed: {exc}")
        return None
    try:
        return _core_device_cache.store(parse_devicectl_devices(json.loads(raw)))
    except (ValueError, TypeError) as exc:
        logger.debug(f"devicectl device list parse failed: {exc}")
        return None


def list_core_devices_sync(
    force_refresh: bool = False,
) -> list[dict[str, Any]] | None:
    """Synchronous variant for the driver factory and other sync callers."""
    if not simctl_available():
        return None
    if not force_refresh:
        cached = _core_device_cache.get()
        if cached is not None:
            return cached
    try:
        completed = subprocess.run(
            ["xcrun", "devicectl", "list", "devices", "--json-output", "-"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=DEVICECTL_ENUMERATE_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug(f"devicectl device enumeration failed: {exc}")
        return None
    if completed.returncode != 0:
        return None
    try:
        return _core_device_cache.store(parse_devicectl_devices(json.loads(completed.stdout)))
    except (ValueError, TypeError) as exc:
        logger.debug(f"devicectl device list parse failed: {exc}")
        return None


def is_physical_ios(device: dict[str, Any]) -> bool:
    """Whether a CoreDevice entry is a physical iPhone/iPad (not a simulator)."""
    return device.get("platform") in ("iOS", "iPadOS") and device.get("reality") == "physical"


def physical_ios_ready(device: dict[str, Any]) -> bool:
    """Whether a CoreDevice entry is a paired, reachable physical iOS device.

    ``connection_state`` (CoreDevice ``tunnelState``) may be absent on paired
    USB devices running older iOS — an absent value is acceptable;
    ``"disconnected"`` is not. Every consumer (pool validators, readiness
    probe, replay manager, driver) must agree on this rule.
    """
    if not is_physical_ios(device):
        return False
    if device.get("pairing_state") != "paired":
        return False
    return device.get("connection_state") in ("connected", None)


def device_matches_identifier(device: dict[str, Any], identifier: str) -> bool:
    """Match a device entry by case-insensitive UDID or exact name."""
    if not identifier:
        return False
    return (
        str(device.get("udid") or "").lower() == identifier.lower()
        or device.get("name") == identifier
    )


def find_physical_ios_device_sync(identifier: str) -> dict[str, Any] | None:
    """Match a physical iOS device by UDID or exact name, or ``None``.

    Raises ``ValueError`` when the identifier matches more than one device —
    silently picking the first would drive an arbitrary phone.
    """
    devices = list_core_devices_sync()
    if devices is None or not identifier:
        return None
    matches = [
        device
        for device in devices
        if is_physical_ios(device) and device_matches_identifier(device, identifier)
    ]
    if len(matches) > 1:
        udids = sorted(str(device.get("udid") or "?") for device in matches)
        raise ValueError(
            f"{len(matches)} physical iOS devices match {identifier!r} "
            f"({', '.join(udids)}); use the device UDID instead."
        )
    return matches[0] if matches else None
