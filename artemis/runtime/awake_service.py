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

"""USB-scoped screen-awake policy shared by every local Artemis entrypoint."""

from __future__ import annotations

import os
import re
import subprocess
import threading
from typing import Iterable

from adbutils import AdbClient

from artemis.config import settings
from artemis.runtime.adb_endpoint import adb_command
from artemis.runtime.awake_lease import ScreenAwakeLease
from artemis.utils.logger import get_logger

logger = get_logger(__name__)

AWAKE_STRATEGY_USB = "usb_stay_on"
AWAKE_STRATEGY_HEARTBEAT = "host_heartbeat"
AWAKE_HEARTBEAT_INTERVAL_SECONDS = 5.0
DEVICE_DISCOVERY_INTERVAL_SECONDS = 2.0


def _awake_enabled() -> bool:
    enabled = os.environ.get("ARTEMIS_KEEP_DEVICE_AWAKE", "true").strip().lower()
    return enabled not in {"0", "false", "no", "off"}


def _run_awake_adb_command(
    device_id: str, args: list[str], description: str
) -> subprocess.CompletedProcess[str] | None:
    """Run one non-fatal ADB command used by the USB awake policy."""
    try:
        result = subprocess.run(
            adb_command(["-s", device_id, *args]),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            logger.warning(
                f"Could not {description} on {device_id}: {detail or 'ADB command failed'}"
            )
        return result
    except Exception as exc:
        logger.warning(f"Could not {description} on {device_id}: {exc}")
        return None


def sanitize_device_state(device_id: str) -> None:
    """Sanitize Android device state by cleaning up orphan screenrecord or modal dialogs."""
    if not device_id:
        return
    # 1. Kill any dangling screenrecord processes left from interrupted tasks
    _run_awake_adb_command(
        device_id,
        ["shell", "pkill", "-f", "screenrecord"],
        "kill orphan screenrecord processes",
    )
    # 2. Close system modal dialogs (ANR / crash popups)
    _run_awake_adb_command(
        device_id,
        ["shell", "am", "broadcast", "-a", "android.intent.action.CLOSE_SYSTEM_DIALOGS"],
        "dismiss system crash/ANR dialogs",
    )


def _configure_usb_stay_awake(device_id: str) -> str | None:
    """Enable USB stay-awake, falling back to an effective host heartbeat.

    The policy is intentionally scoped to USB power rather than an Artemis
    process or task. Android applies the normal screen timeout as soon as USB
    power is removed, even though no ADB command can be sent after unplugging.
    AC, wireless, and dock charging retain the user's normal behavior.
    """
    if not _awake_enabled() or os.environ.get("ARTEMIS_CLOUD_MODE") == "1":
        return None

    # Migration cleanup: previous Artemis versions used a persistent shell
    # wake lock. Drain it only after every live legacy owner has exited.
    ScreenAwakeLease(device_id).cleanup_unowned_references()

    commands = (
        (
            "enable USB-scoped stay-awake policy",
            ["shell", "svc", "power", "stayon", "usb"],
        ),
        ("wake display", ["shell", "input", "keyevent", "KEYCODE_WAKEUP"]),
        ("dismiss non-secure keyguard", ["shell", "wm", "dismiss-keyguard"]),
    )
    for description, args in commands:
        _run_awake_adb_command(device_id, args, description)

    configured = _run_awake_adb_command(
        device_id,
        ["shell", "settings", "get", "global", "stay_on_while_plugged_in"],
        "verify USB-scoped stay-awake policy",
    )
    power_state = _run_awake_adb_command(
        device_id,
        ["shell", "dumpsys", "power"],
        "verify active USB stay-awake state",
    )
    try:
        value = int(configured.stdout.strip()) if configured is not None else -1
    except (TypeError, ValueError):
        value = -1
    stay_on_active = (
        configured is not None
        and configured.returncode == 0
        and value == 2
        and power_state is not None
        and power_state.returncode == 0
        and re.search(r"\bmStayOn=true\b", power_state.stdout) is not None
    )
    if stay_on_active:
        logger.info(f"USB-scoped stay-awake policy verified active on {device_id}")
        return AWAKE_STRATEGY_USB

    logger.warning(
        f"USB stay-awake is not active on {device_id} (setting={value}); "
        f"using a {AWAKE_HEARTBEAT_INTERVAL_SECONDS:g}-second host heartbeat"
    )
    primed = _run_awake_adb_command(
        device_id,
        ["shell", "input", "keyevent", "KEYCODE_UNKNOWN"],
        "prime the host stay-awake heartbeat",
    )
    if primed is None or primed.returncode != 0:
        return None
    return AWAKE_STRATEGY_HEARTBEAT


def _discover_connected_device_ids() -> list[str]:
    """Return the explicitly targeted device, or the connected devices Artemis manages."""
    if not _awake_enabled() or os.environ.get("ARTEMIS_CLOUD_MODE") == "1":
        return []

    target = os.environ.get("ARTEMIS_DEVICE_ID") or os.environ.get("ADB_DEVICE_SERIAL")
    host = os.environ.get("ADB_HOST") or settings.ADB_HOST or "127.0.0.1"
    port_text = os.environ.get("ADB_PORT")
    try:
        port = int(port_text) if port_text else int(settings.ADB_PORT or 5037)
    except (TypeError, ValueError):
        port = 5037

    try:
        devices = AdbClient(host=host, port=port).device_list()
        serials = [device.serial for device in devices if device.serial]
    except Exception as exc:
        logger.debug(f"Could not discover a device for the USB awake policy: {exc}")
        return []

    if target:
        return [target] if target in serials else []
    # Keep awake only the devices Artemis actually manages: serials claimed in
    # the device pool by an active execution lock or a live queue reservation.
    # Other devices sharing the same ADB server belong to unrelated tools or
    # users, and their power policy must not be touched.
    try:
        from artemis.runtime.device_pool import device_pool

        claimed = device_pool.get_claimed_serials()
    except Exception as exc:
        logger.debug(f"Could not query claimed devices for the awake policy: {exc}")
        return []
    return [serial for serial in serials if serial in claimed]


class ScreenAwakeService:
    """Apply USB stay-awake or a connection-scoped host heartbeat."""

    def __init__(self):
        self._lock = threading.RLock()
        self._strategies: dict[str, str] = {}
        self._heartbeat_stops: dict[str, threading.Event] = {}
        self._heartbeat_threads: dict[str, threading.Thread] = {}
        self._monitor_stop: threading.Event | None = None
        self._monitor_thread: threading.Thread | None = None
        self._shutdown_requested = False

    def start(self, device_ids: Iterable[str] | None = None) -> dict[str, str | None]:
        if self._shutdown_requested:
            return {}
        selected = list(device_ids) if device_ids is not None else _discover_connected_device_ids()
        results = {}
        for device_id in dict.fromkeys(selected):
            if self._shutdown_requested:
                break
            if device_id:
                results[device_id] = self.ensure_device(device_id)
        if (
            _awake_enabled()
            and os.environ.get("ARTEMIS_CLOUD_MODE") != "1"
            and not self._shutdown_requested
        ):
            self._start_device_monitor()
        return results

    def ensure_device(self, device_id: str) -> str | None:
        """Configure one device once per process; Android owns unplug behavior."""
        if (
            not device_id
            or not _awake_enabled()
            or os.environ.get("ARTEMIS_CLOUD_MODE") == "1"
            or self._shutdown_requested
        ):
            return None
        with self._lock:
            existing = self._strategies.get(device_id)
            if existing is not None:
                return existing
            strategy = _configure_usb_stay_awake(device_id)
            if strategy is not None:
                self._strategies[device_id] = strategy
                if strategy == AWAKE_STRATEGY_HEARTBEAT:
                    self._start_heartbeat(device_id)
            return strategy

    def _start_heartbeat(self, device_id: str) -> None:
        if device_id in self._heartbeat_threads:
            return
        stop_event = threading.Event()

        def heartbeat() -> None:
            while not stop_event.wait(AWAKE_HEARTBEAT_INTERVAL_SECONDS):
                _run_awake_adb_command(
                    device_id,
                    ["shell", "input", "keyevent", "KEYCODE_UNKNOWN"],
                    "send the host stay-awake heartbeat",
                )

        thread = threading.Thread(
            target=heartbeat,
            name=f"artemis-awake-heartbeat-{device_id}",
            daemon=True,
        )
        self._heartbeat_stops[device_id] = stop_event
        self._heartbeat_threads[device_id] = thread
        thread.start()

    def _start_device_monitor(self) -> None:
        with self._lock:
            if self._monitor_thread is not None and self._monitor_thread.is_alive():
                return
            stop_event = threading.Event()

            def monitor() -> None:
                while not stop_event.wait(DEVICE_DISCOVERY_INTERVAL_SECONDS):
                    connected = set(_discover_connected_device_ids())
                    if stop_event.is_set():
                        break
                    self._reconcile_connected_devices(connected)

            thread = threading.Thread(
                target=monitor,
                name="artemis-awake-device-monitor",
                daemon=True,
            )
            self._monitor_stop = stop_event
            self._monitor_thread = thread
            thread.start()

    def _reconcile_connected_devices(self, connected: set[str]) -> None:
        """Stop unplugged heartbeats and enroll newly attached devices."""
        with self._lock:
            missing = set(self._strategies) - connected
        for device_id in missing:
            self._stop_device(device_id)
        for device_id in connected:
            self.ensure_device(device_id)

    def _stop_device(self, device_id: str) -> None:
        with self._lock:
            stop_event = self._heartbeat_stops.pop(device_id, None)
            thread = self._heartbeat_threads.pop(device_id, None)
            self._strategies.pop(device_id, None)
            if stop_event is not None:
                stop_event.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def shutdown(self) -> None:
        """Stop host work; unplug behavior remains owned by Android's USB policy."""
        self._shutdown_requested = True
        with self._lock:
            monitor_stop = self._monitor_stop
            monitor_thread = self._monitor_thread
            heartbeat_stops = list(self._heartbeat_stops.values())
            heartbeat_threads = list(self._heartbeat_threads.values())
            if monitor_stop is not None:
                monitor_stop.set()
            for stop_event in heartbeat_stops:
                stop_event.set()
            self._monitor_stop = None
            self._monitor_thread = None
            self._heartbeat_stops.clear()
            self._heartbeat_threads.clear()
            self._strategies.clear()

        if monitor_thread is not None:
            monitor_thread.join(timeout=2.0)
        for thread in heartbeat_threads:
            thread.join(timeout=2.0)

    @property
    def device_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._strategies)


screen_awake_service = ScreenAwakeService()


def start_awake_service(device_ids: Iterable[str] | None = None) -> dict[str, str | None]:
    return screen_awake_service.start(device_ids)


def ensure_device_awake(device_id: str) -> str | None:
    return screen_awake_service.ensure_device(device_id)


def shutdown_awake_service() -> None:
    screen_awake_service.shutdown()
