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

"""Android Emulator Launch & Boot Progress Lifecycle Manager."""

import asyncio
from collections import deque
from enum import Enum
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from typing import Any

from pydantic import BaseModel, Field

from artemis.platform import OSType, platform
from artemis.toolchain import toolchain
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


class EmulatorLaunchStage(str, Enum):
    """Lifecycle stages of launching an Android Virtual Device."""

    IDLE = "idle"
    STARTING = "starting"  # Process spawned, verifying PID
    WAITING_FOR_ADB = "waiting_for_adb"  # Process alive, waiting for emulator to appear in ADB
    BOOTING = "booting"  # Visible in ADB, waiting for sys.boot_completed=1
    READY = "ready"  # Boot complete and ready for automation
    FAILED = "failed"  # Process crashed, lock error, or timeout
    STOPPED = "stopped"  # Stopped by user


class EmulatorLaunchState(BaseModel):
    """State schema for real-time emulator launch tracking."""

    avd_name: str | None = None
    status: EmulatorLaunchStage = EmulatorLaunchStage.IDLE
    pid: int | None = None
    serial: str | None = None
    stage_message: str = "Ready to launch"
    progress_percent: int = 0
    started_at: float | None = None
    elapsed_seconds: int = 0
    error: str | None = None
    logs: list[str] = Field(default_factory=list)
    can_retry: bool = True


class EmulatorManager:
    """Manages background emulator execution, stdout/stderr stream capture, and boot completion polling."""

    def __init__(self):
        self._current_state = EmulatorLaunchState()
        self._proc: subprocess.Popen | None = None
        self._log_buffer: deque[str] = deque(maxlen=100)
        self._track_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._reader_thread: threading.Thread | None = None

    @staticmethod
    def _subprocess_creation_kwargs() -> dict[str, Any]:
        """Isolate the emulator from the UI server's console signals.

        ``start_new_session`` is POSIX-only and is ignored by Python's Windows
        subprocess implementation. Windows therefore needs explicit creation
        flags so Ctrl+C or shutdown signals sent to the UI server do not also
        terminate the emulator. The Linux/macOS behavior remains unchanged.
        """
        if sys.platform == "win32":
            return {
                "creationflags": (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
            }
        return {"start_new_session": True}

    def _locate_emulator(self) -> str | None:
        """Find the emulator binary path from PATH or standard SDK environments."""
        resolved = toolchain.resolve("emulator")
        if resolved:
            return resolved

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

    def _locate_adb(self) -> str:
        """Find the adb binary path."""
        resolved = toolchain.resolve("adb")
        if resolved:
            return resolved
        return shutil.which("adb") or "adb"

    def get_status(self) -> EmulatorLaunchState:
        """Return current launch progress state snapshot."""
        state = self._current_state.model_copy()
        if state.started_at and state.status in (
            EmulatorLaunchStage.STARTING,
            EmulatorLaunchStage.WAITING_FOR_ADB,
            EmulatorLaunchStage.BOOTING,
        ):
            state.elapsed_seconds = int(time.time() - state.started_at)
        state.logs = list(self._log_buffer)
        return state

    def _stream_logs(self, proc: subprocess.Popen):
        """Continuously read process stdout/stderr in a background thread."""
        try:
            if proc.stdout:
                for line in iter(proc.stdout.readline, ""):
                    if not line:
                        break
                    clean_line = line.strip()
                    if clean_line:
                        self._log_buffer.append(clean_line)
        except Exception as e:
            logger.debug(f"Log streaming finished: {e}")
        finally:
            if proc.stdout:
                proc.stdout.close()

    async def launch(self, avd_name: str) -> EmulatorLaunchState:
        """Initiate background emulator launch and start asynchronous boot monitoring."""
        clean_avd = avd_name.strip()
        if not clean_avd:
            self._current_state = EmulatorLaunchState(
                status=EmulatorLaunchStage.FAILED,
                error="AVD name cannot be empty.",
                stage_message="Launch failed: AVD name cannot be empty.",
            )
            return self.get_status()

        async with self._lock:
            # If already launching the same AVD and process is alive
            if (
                self._current_state.status
                in (
                    EmulatorLaunchStage.STARTING,
                    EmulatorLaunchStage.WAITING_FOR_ADB,
                    EmulatorLaunchStage.BOOTING,
                )
                and self._current_state.avd_name == clean_avd
                and self._proc is not None
                and self._proc.poll() is None
            ):
                logger.info(f"AVD '{clean_avd}' is already in progress of launching.")
                return self.get_status()

            emu_path = self._locate_emulator()
            if not emu_path:
                self._current_state = EmulatorLaunchState(
                    avd_name=clean_avd,
                    status=EmulatorLaunchStage.FAILED,
                    error="Android emulator executable not found in PATH or Android SDK. Please install Android SDK platform tools and emulator.",
                    stage_message="Emulator binary not found.",
                )
                return self.get_status()

            self._log_buffer.clear()
            self._log_buffer.append(f"Starting emulator '{clean_avd}' using binary: {emu_path}")

            try:
                # Cancel previous tracker if any
                if self._track_task and not self._track_task.done():
                    self._track_task.cancel()

                # Spawn emulator process capturing stdout & stderr
                proc = subprocess.Popen(
                    [emu_path, "-avd", clean_avd],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    **self._subprocess_creation_kwargs(),
                )
                self._proc = proc
                now = time.time()

                # Start reader thread for stdout/stderr
                self._reader_thread = threading.Thread(
                    target=self._stream_logs, args=(proc,), daemon=True
                )
                self._reader_thread.start()

                self._current_state = EmulatorLaunchState(
                    avd_name=clean_avd,
                    status=EmulatorLaunchStage.STARTING,
                    pid=proc.pid,
                    stage_message=f"Process spawned (PID: {proc.pid}). Initializing QEMU virtualization...",
                    progress_percent=15,
                    started_at=now,
                    elapsed_seconds=0,
                    error=None,
                    logs=list(self._log_buffer),
                )

                # Launch async boot tracker task
                self._track_task = asyncio.create_task(
                    self._track_boot_lifecycle(clean_avd, proc, now)
                )

                return self.get_status()

            except Exception as e:
                logger.error(f"Failed to spawn emulator process: {e}")
                self._current_state = EmulatorLaunchState(
                    avd_name=clean_avd,
                    status=EmulatorLaunchStage.FAILED,
                    error=f"Failed to spawn emulator process: {e}",
                    stage_message="Failed to spawn emulator process.",
                    logs=list(self._log_buffer),
                )
                return self.get_status()

    async def _track_boot_lifecycle(self, avd_name: str, proc: subprocess.Popen, started_at: float):
        """Monitor emulator lifecycle from process execution to ADB connection and OS boot completion."""
        adb_path = self._locate_adb()
        detected_serial: str | None = None
        max_wait_seconds = 180  # 3 minutes maximum boot timeout

        try:
            # Phase 1: Early crash detection (first 5 seconds)
            for _ in range(5):
                await asyncio.sleep(1)
                poll_res = proc.poll()
                if poll_res is not None:
                    logs_str = "\n".join(list(self._log_buffer)[-10:])
                    error_msg = f"Emulator process exited immediately with code {poll_res}."
                    if "already running" in logs_str.lower() or "lock" in logs_str.lower():
                        error_msg = f"AVD '{avd_name}' is locked by another running emulator instance. Try stopping existing instances or restart ADB."
                    elif "panic" in logs_str.lower():
                        error_msg = f"Emulator panic: {logs_str}"

                    logger.error(f"[EmulatorManager] Early crash: {error_msg}")
                    self._current_state = EmulatorLaunchState(
                        avd_name=avd_name,
                        status=EmulatorLaunchStage.FAILED,
                        pid=proc.pid,
                        error=error_msg,
                        stage_message=f"Process exited prematurely (Exit code {poll_res})",
                        started_at=started_at,
                        elapsed_seconds=int(time.time() - started_at),
                        logs=list(self._log_buffer),
                    )
                    return

            # Phase 2: Waiting for ADB handshake
            self._current_state.status = EmulatorLaunchStage.WAITING_FOR_ADB
            self._current_state.progress_percent = 35
            self._current_state.stage_message = (
                "QEMU hypervisor started. Waiting for ADB connection..."
            )

            while time.time() - started_at < max_wait_seconds:
                await asyncio.sleep(1.5)

                # Check if process died
                poll_res = proc.poll()
                if poll_res is not None:
                    logs_str = "\n".join(list(self._log_buffer)[-10:])
                    self._current_state = EmulatorLaunchState(
                        avd_name=avd_name,
                        status=EmulatorLaunchStage.FAILED,
                        pid=proc.pid,
                        error=f"Emulator process terminated unexpectedly (exit code {poll_res}): {logs_str}",
                        stage_message="Process terminated unexpectedly",
                        started_at=started_at,
                        elapsed_seconds=int(time.time() - started_at),
                        logs=list(self._log_buffer),
                    )
                    return

                # Check adb devices for emulator serial
                try:
                    p = await asyncio.create_subprocess_exec(
                        adb_path,
                        "devices",
                        "-l",
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    stdout, _ = await p.communicate()
                    out_text = stdout.decode(errors="replace")

                    # Scan for emulator-*
                    for line in out_text.splitlines():
                        line = line.strip()
                        if line.startswith("emulator-"):
                            parts = line.split()
                            if parts:
                                detected_serial = parts[0]
                                break
                except Exception as e:
                    logger.debug(f"ADB query error during boot tracking: {e}")

                if detected_serial:
                    break

            if not detected_serial:
                self._current_state = EmulatorLaunchState(
                    avd_name=avd_name,
                    status=EmulatorLaunchStage.FAILED,
                    pid=proc.pid,
                    error="Timeout waiting for emulator to connect to ADB.",
                    stage_message="ADB handshake timed out.",
                    started_at=started_at,
                    elapsed_seconds=int(time.time() - started_at),
                    logs=list(self._log_buffer),
                )
                return

            # Phase 3: Android OS Booting (Polling sys.boot_completed)
            self._current_state.status = EmulatorLaunchStage.BOOTING
            self._current_state.serial = detected_serial
            self._current_state.progress_percent = 65
            self._current_state.stage_message = (
                f"Connected to ADB ({detected_serial}). Android OS is booting up..."
            )

            while time.time() - started_at < max_wait_seconds:
                await asyncio.sleep(2)

                # Check if process died
                poll_res = proc.poll()
                if poll_res is not None:
                    self._current_state = EmulatorLaunchState(
                        avd_name=avd_name,
                        status=EmulatorLaunchStage.FAILED,
                        pid=proc.pid,
                        error=f"Emulator process crashed during OS boot (exit code {poll_res}).",
                        stage_message="Process crashed during OS boot",
                        started_at=started_at,
                        elapsed_seconds=int(time.time() - started_at),
                        logs=list(self._log_buffer),
                    )
                    return

                # Check sys.boot_completed
                try:
                    p = await asyncio.create_subprocess_exec(
                        adb_path,
                        "-s",
                        detected_serial,
                        "shell",
                        "getprop sys.boot_completed",
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    stdout, _ = await p.communicate()
                    boot_completed = stdout.decode(errors="replace").strip()

                    if boot_completed == "1":
                        logger.info(
                            f"[EmulatorManager] Android system boot completed for {detected_serial} ({avd_name})"
                        )
                        self._current_state = EmulatorLaunchState(
                            avd_name=avd_name,
                            status=EmulatorLaunchStage.READY,
                            pid=proc.pid,
                            serial=detected_serial,
                            stage_message=f"Android emulator '{avd_name}' is fully booted and ready!",
                            progress_percent=100,
                            started_at=started_at,
                            elapsed_seconds=int(time.time() - started_at),
                            error=None,
                            logs=list(self._log_buffer),
                        )
                        try:
                            from artemis.core.diagnostics.engine import readiness_engine

                            # Focus the readiness report on the freshly booted
                            # emulator; task routing still resolves its own
                            # target from the request or the device pool.
                            readiness_engine.set_probe_target_serial(detected_serial)
                        except Exception as e:
                            logger.debug(f"Failed to focus probes on the new emulator: {e}")
                        return
                    else:
                        # Gradual progress visual indicator
                        elapsed = time.time() - started_at
                        calc_progress = min(95, int(65 + (elapsed / 60) * 30))
                        self._current_state.progress_percent = calc_progress
                        self._current_state.stage_message = f"Android OS booting ({int(elapsed)}s)... Initializing system services..."
                except Exception as e:
                    logger.debug(f"Error querying boot_completed: {e}")

            # Timeout after max_wait_seconds
            self._current_state = EmulatorLaunchState(
                avd_name=avd_name,
                status=EmulatorLaunchStage.FAILED,
                pid=proc.pid,
                serial=detected_serial,
                error=f"Android OS boot did not finish within {max_wait_seconds}s timeout.",
                stage_message="Boot process timed out.",
                started_at=started_at,
                elapsed_seconds=int(time.time() - started_at),
                logs=list(self._log_buffer),
            )

        except asyncio.CancelledError:
            logger.info(f"[EmulatorManager] Boot tracker cancelled for '{avd_name}'")
        except Exception as exc:
            logger.error(f"[EmulatorManager] Unexpected exception in boot tracker: {exc}")
            self._current_state = EmulatorLaunchState(
                avd_name=avd_name,
                status=EmulatorLaunchStage.FAILED,
                pid=proc.pid if proc else None,
                error=f"Unexpected error: {exc}",
                stage_message="Unexpected error during boot tracking.",
                started_at=started_at,
                elapsed_seconds=int(time.time() - started_at),
                logs=list(self._log_buffer),
            )

    async def stop(self) -> dict[str, Any]:
        """Terminate the active emulator process."""
        async with self._lock:
            if self._track_task and not self._track_task.done():
                self._track_task.cancel()

            serial = self._current_state.serial
            pid = self._current_state.pid
            avd = self._current_state.avd_name

            if serial:
                try:
                    adb_path = self._locate_adb()
                    subprocess.run(
                        [adb_path, "-s", serial, "emu", "kill"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                    )
                except (OSError, subprocess.SubprocessError):
                    # Graceful `adb emu kill` failed or timed out; the direct
                    # process terminate/kill below still stops the emulator.
                    pass

            if self._proc and self._proc.poll() is None:
                try:
                    self._proc.terminate()
                    try:
                        self._proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self._proc.kill()
                except Exception as e:
                    logger.error(f"Error terminating emulator process {pid}: {e}")

            self._current_state = EmulatorLaunchState(
                avd_name=avd,
                status=EmulatorLaunchStage.STOPPED,
                stage_message="Emulator process stopped by user.",
                logs=list(self._log_buffer),
            )
            return {"success": True, "message": f"Emulator '{avd}' stopped"}

    def dismiss(self) -> dict[str, Any]:
        """Reset emulator launch state back to idle."""
        self._current_state = EmulatorLaunchState()
        self._log_buffer.clear()
        return {"success": True, "message": "State reset"}


# Global singleton instance
emulator_manager = EmulatorManager()
