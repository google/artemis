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

"""Device Pool Manager for discovering, tracking, and allocating mobile devices."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import shutil
import subprocess
from typing import Any

from artemis.runtime.device_lock import DeviceExecutionLock
from artemis.toolchain import toolchain
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class DeviceStatus:
    """State and lock allocation metadata for a connected device."""

    serial: str
    state: str  # "device", "offline", "unauthorized", etc.
    model: str | None = None
    product: str | None = None
    is_emulator: bool = False
    is_busy: bool = False
    active_pid: int | None = None
    active_task_desc: str | None = None
    active_session_id: str | None = None
    acquired_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "serial": self.serial,
            "state": self.state,
            "model": self.model,
            "product": self.product,
            "is_emulator": self.is_emulator,
            "is_busy": self.is_busy,
            "active_pid": self.active_pid,
            "active_task_desc": self.active_task_desc,
            "active_session_id": self.active_session_id,
            "acquired_at": self.acquired_at,
        }


class DevicePool:
    """Manages discovery and assignment across all connected Android devices."""

    def __init__(self, adb_path: str | None = None):
        self._adb_path = adb_path

    def _resolve_adb(self) -> str | None:
        if self._adb_path:
            return self._adb_path
        try:
            return toolchain.resolve("adb") or shutil.which("adb")
        except Exception:
            return shutil.which("adb")

    def _query_adb_devices_sync(self, timeout: float = 2.0) -> list[tuple[str, str, str | None, str | None]]:
        """Run `adb devices -l` synchronously to parse attached devices."""
        adb = self._resolve_adb()
        if not adb:
            return []
        try:
            res = subprocess.run(
                [adb, "devices", "-l"],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            if res.returncode != 0:
                return []
            return self._parse_device_lines(res.stdout.splitlines())
        except Exception as exc:
            logger.debug(f"Error querying adb devices: {exc}")
            return []

    async def _query_adb_devices_async(self, timeout: float = 2.0) -> list[tuple[str, str, str | None, str | None]]:
        """Run `adb devices -l` asynchronously to parse attached devices."""
        adb = self._resolve_adb()
        if not adb:
            return []
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                adb,
                "devices",
                "-l",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            if proc.returncode != 0:
                return []
            return self._parse_device_lines(stdout.decode(errors="replace").splitlines())
        except Exception as exc:
            logger.debug(f"Error querying adb devices asynchronously: {exc}")
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass
            return []

    @staticmethod
    def _parse_device_lines(lines: list[str]) -> list[tuple[str, str, str | None, str | None]]:
        results: list[tuple[str, str, str | None, str | None]] = []
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("List of devices"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            serial = parts[0]
            state = parts[1]
            model = None
            product = None
            for token in parts[2:]:
                if token.startswith("model:"):
                    model = token.split(":", 1)[1].replace("_", " ")
                elif token.startswith("product:"):
                    product = token.split(":", 1)[1]
            results.append((serial, state, model, product))
        return results

    def list_devices(self) -> list[DeviceStatus]:
        """Synchronously list all connected devices along with their active lock state."""
        raw_devices = self._query_adb_devices_sync()
        active_owners = DeviceExecutionLock.get_active_owners()

        devices: list[DeviceStatus] = []
        for serial, state, model, product in raw_devices:
            is_emu = serial.startswith("emulator-") or "127.0.0.1" in serial or "localhost" in serial
            clean_id = DeviceExecutionLock._normalize_device_id(serial)
            owner = active_owners.get(clean_id)

            status = DeviceStatus(
                serial=serial,
                state=state,
                model=model,
                product=product,
                is_emulator=is_emu,
                is_busy=owner is not None,
                active_pid=owner.pid if owner else None,
                active_task_desc=owner.description if owner else None,
                active_session_id=owner.session_id if owner else None,
                acquired_at=owner.acquired_at if owner else None,
            )
            devices.append(status)
        return devices

    async def list_devices_async(self) -> list[DeviceStatus]:
        """Asynchronously list all connected devices along with their active lock state."""
        raw_devices = await self._query_adb_devices_async()
        active_owners = DeviceExecutionLock.get_active_owners()

        devices: list[DeviceStatus] = []
        for serial, state, model, product in raw_devices:
            is_emu = serial.startswith("emulator-") or "127.0.0.1" in serial or "localhost" in serial
            clean_id = DeviceExecutionLock._normalize_device_id(serial)
            owner = active_owners.get(clean_id)

            status = DeviceStatus(
                serial=serial,
                state=state,
                model=model,
                product=product,
                is_emulator=is_emu,
                is_busy=owner is not None,
                active_pid=owner.pid if owner else None,
                active_task_desc=owner.description if owner else None,
                active_session_id=owner.session_id if owner else None,
                acquired_at=owner.acquired_at if owner else None,
            )
            devices.append(status)
        return devices

    def get_ready_devices(self) -> list[DeviceStatus]:
        """Return devices in authorized 'device' state."""
        return [d for d in self.list_devices() if d.state == "device"]

    def get_idle_devices(self) -> list[DeviceStatus]:
        """Return ready devices that are not currently holding an execution lock."""
        return [d for d in self.list_devices() if d.state == "device" and not d.is_busy]

    def select_device(self, preferred_serial: str | None = None) -> str | None:
        """Select a target device serial for task execution.

        If `preferred_serial` is provided:
            Returns preferred_serial if it is present or online.
        Otherwise:
            Selects the first idle/unlocked ready device. If all are busy,
            selects the first ready device so the task will queue on it.
            Returns None if no devices are attached.
        """
        all_devs = self.list_devices()
        ready_devs = [d for d in all_devs if d.state == "device"]

        if preferred_serial:
            # Check if preferred is available or exists
            for dev in all_devs:
                if dev.serial == preferred_serial:
                    return dev.serial
            # If not detected by ADB, still return it so ADB can attempt connection
            return preferred_serial

        # Pick first idle device
        for dev in ready_devs:
            if not dev.is_busy:
                return dev.serial

        # If all busy, return the first ready device (it will queue)
        if ready_devs:
            return ready_devs[0].serial

        # Fallback to any attached device if none ready
        if all_devs:
            return all_devs[0].serial

        return None


# Global device pool instance
device_pool = DevicePool()
