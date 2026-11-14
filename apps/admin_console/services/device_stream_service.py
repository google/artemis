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

"""Device Live Screen Streaming Service.

Provides real-time, low-latency device screen frames over HTTP MJPEG and WebSocket.
Android frames come from ``adb exec-out screencap``; iOS frames come
from the native ``xcrun simctl io <UDID> screenshot`` capture. The target is
picked per frame so the stream follows whichever platform is under automation.
"""

import asyncio
import logging
import subprocess
import time
from collections.abc import AsyncGenerator
from pathlib import Path

from artemis.config.paths import get_temp_dir
from artemis.runtime import DeviceExecutionLock, ios_device_pool
from artemis.runtime.device_target import IOS_LOCK_SCOPE
from artemis.toolchain import find_adb

logger = logging.getLogger("artemis.stream_service")


class DeviceStreamService:
    """Manages real-time screen capture and distribution to web clients."""

    def __init__(self):
        self._active_listeners = 0
        self._lock = asyncio.Lock()
        self._latest_frame: bytes | None = None
        self._last_frame_time: float = 0.0
        self._is_capturing = False
        self._capture_task: asyncio.Task | None = None
        self._last_target: dict[str, str] | None = None

    async def get_stream_target(self) -> dict[str, str] | None:
        """Pick the device the stream should follow right now.

        An actively locked iOS device wins (it is the device under
        automation), then a connected Android device, then the single
        unambiguous booted simulator. Returns ``{"platform", "serial"}``.
        """
        try:
            for owner in DeviceExecutionLock.get_active_owners().values():
                if (
                    owner
                    and getattr(owner, "lock_scope", None) == IOS_LOCK_SCOPE
                    and owner.device_id
                ):
                    return {"platform": "ios", "serial": str(owner.device_id)}
        except Exception as exc:
            logger.debug(f"[StreamService] iOS lock-owner scan failed: {exc}")

        serial = await self._android_serial()
        if serial:
            return {"platform": "android", "serial": serial}

        udid = await ios_device_pool.select_device_async()
        if udid:
            return {"platform": "ios", "serial": udid}
        return None

    async def get_device_serial(self) -> str | None:
        """Serial of the currently streamable device, regardless of platform."""
        target = await self.get_stream_target()
        return target["serial"] if target else None

    async def _android_serial(self) -> str | None:
        """Find the currently connected active ADB device serial."""
        try:
            adb_bin = find_adb()
            proc = await asyncio.create_subprocess_exec(
                adb_bin,
                "devices",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            lines = stdout.decode().strip().splitlines()
            for line in lines[1:]:
                parts = line.strip().split()
                if len(parts) >= 2 and parts[1] == "device":
                    return parts[0]
        except Exception as e:
            logger.warning(f"Error checking adb devices: {e}")
        return None

    async def _capture_android(self, serial: str | None) -> bytes | None:
        adb_bin = find_adb()
        cmd = (
            [adb_bin, "-s", serial, "exec-out", "screencap", "-p"]
            if serial
            else [adb_bin, "exec-out", "screencap", "-p"]
        )
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return stdout if proc.returncode == 0 and len(stdout) > 1000 else None

    async def _capture_ios(self, udid: str) -> bytes | None:
        """One PNG frame via simctl (simulator) or devicectl (physical).

        Both tools only write files, so frames stream through one reused temp
        path per UDID.
        """
        from artemis.drivers.ios.discovery import find_physical_ios_device_sync

        frame_path = Path(get_temp_dir("streams")) / f"ios_stream_{udid}.png"
        physical = await asyncio.to_thread(find_physical_ios_device_sync, udid)
        if physical is not None:
            cmd = [
                "xcrun",
                "devicectl",
                "device",
                "capture",
                "screenshot",
                "--device",
                udid,
                "--destination",
                str(frame_path),
            ]
        else:
            cmd = [
                "xcrun",
                "simctl",
                "io",
                udid,
                "screenshot",
                "--type=png",
                str(frame_path),
            ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                await proc.communicate()
            except asyncio.CancelledError:
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                await proc.communicate()
                raise
            if proc.returncode == 0 and frame_path.exists():
                data = frame_path.read_bytes()
                return data if len(data) > 1000 else None
        finally:
            frame_path.unlink(missing_ok=True)
        return None

    async def _capture_loop(self):
        """Background frame capture loop that runs while listeners > 0."""
        logger.info("[StreamService] Starting live screen capture loop...")
        while self._active_listeners > 0:
            try:
                start_t = time.time()
                target = await self.get_stream_target()
                if target != self._last_target:
                    # Never carry frames across a device/platform switch.
                    self._latest_frame = None
                    self._last_target = target
                frame = None
                if target is not None:
                    if target["platform"] == "ios":
                        frame = await self._capture_ios(target["serial"])
                    else:
                        frame = await self._capture_android(target["serial"])
                if frame is not None:
                    self._latest_frame = frame
                    self._last_frame_time = time.time()

                elapsed = time.time() - start_t
                delay = max(0.03, 0.08 - elapsed)
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[StreamService] Capture error: {e}")
                await asyncio.sleep(0.5)

        logger.info("[StreamService] Stopping live screen capture loop (0 listeners).")
        self._is_capturing = False

    async def start_capturing(self):
        """Register a new listener and start background capture if needed."""
        async with self._lock:
            self._active_listeners += 1
            if not self._is_capturing or self._capture_task is None or self._capture_task.done():
                self._is_capturing = True
                self._capture_task = asyncio.create_task(self._capture_loop())

    async def stop_capturing(self):
        """Deregister a listener and stop capture loop when count reaches 0."""
        async with self._lock:
            self._active_listeners = max(0, self._active_listeners - 1)
            if self._active_listeners == 0 and self._capture_task and not self._capture_task.done():
                self._capture_task.cancel()
                self._is_capturing = False

    async def mjpeg_frame_generator(self) -> AsyncGenerator[bytes, None]:
        """Async generator streaming MJPEG multipart bytes to HTTP response."""
        await self.start_capturing()
        try:
            last_sent_time = 0.0
            while True:
                if self._latest_frame and self._last_frame_time > last_sent_time:
                    last_sent_time = self._last_frame_time
                    frame_bytes = self._latest_frame
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/png\r\n"
                        b"Content-Length: "
                        + str(len(frame_bytes)).encode()
                        + b"\r\n\r\n"
                        + frame_bytes
                        + b"\r\n"
                    )
                await asyncio.sleep(0.04)
        finally:
            await self.stop_capturing()


device_stream_service = DeviceStreamService()
