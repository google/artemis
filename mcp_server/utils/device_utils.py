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

"""Device discovery, ADB resolution, and emulator management for MCP workflows."""

import os
import shutil
import subprocess
import sys
import time


def resolve_adb_path() -> str:
    """Resolves the absolute path to the adb binary across platforms."""
    # 1. Check if adb is in PATH
    which_adb = shutil.which("adb")
    if which_adb:
        return which_adb

    # 2. Check ANDROID_HOME / ANDROID_SDK_ROOT
    for env_var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        sdk_root = os.getenv(env_var)
        if sdk_root:
            candidate = os.path.join(
                sdk_root, "platform-tools", "adb.exe" if sys.platform == "win32" else "adb"
            )
            if os.path.exists(candidate):
                return candidate

    # 3. Check standard OS-specific SDK locations
    candidates = []
    if sys.platform == "win32":
        local_appdata = os.getenv("LOCALAPPDATA", "")
        if local_appdata:
            candidates.append(
                os.path.join(local_appdata, "Android", "Sdk", "platform-tools", "adb.exe")
            )
    elif sys.platform == "darwin":
        candidates.extend(
            [
                os.path.expanduser("~/Library/Android/sdk/platform-tools/adb"),
                "/opt/homebrew/bin/adb",
                "/usr/local/bin/adb",
            ]
        )
    else:  # Linux
        candidates.extend(
            [
                "/usr/bin/adb",
                os.path.expanduser("~/Android/Sdk/platform-tools/adb"),
                "/usr/local/bin/adb",
            ]
        )

    for cand in candidates:
        if os.path.exists(cand):
            return cand

    return "adb"


def resolve_emulator_path() -> str | None:
    """Resolves the path to the Android emulator executable."""
    which_emulator = shutil.which("emulator")
    if which_emulator:
        return which_emulator

    candidates = [
        os.path.expanduser("~/Android/Sdk/emulator/emulator"),
        os.path.expanduser("~/Library/Android/sdk/emulator/emulator"),
    ]
    if sys.platform == "win32":
        local_appdata = os.getenv("LOCALAPPDATA", "")
        if local_appdata:
            candidates.append(
                os.path.join(local_appdata, "Android", "Sdk", "emulator", "emulator.exe")
            )

    for cand in candidates:
        if os.path.exists(cand):
            return cand
    return None


def get_connected_devices(adb_path: str | None = None) -> list[str]:
    """Returns a list of connected and authorized Android device serials."""
    adb = adb_path or resolve_adb_path()
    try:
        res = subprocess.run(
            [adb, "devices"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        devices: list[str] = []
        for line in res.stdout.splitlines():
            line_str = line.strip()
            if line_str and not line_str.startswith("List of devices"):
                parts = line_str.split()
                if len(parts) >= 2 and parts[1] == "device":
                    devices.append(parts[0])
        return devices
    except Exception:
        return []


def is_emulator_running(adb_path: str | None = None) -> bool:
    """Checks if an Android emulator (e.g. emulator-5554) is running and accessible."""
    adb = adb_path or resolve_adb_path()
    devices = get_connected_devices(adb)
    return any(d.startswith("emulator-") for d in devices)


def ensure_emulator(
    avd_name: str = "AndroidWorldAvd",
    adb_path: str | None = None,
    emulator_path: str | None = None,
    timeout_seconds: int = 90,
) -> bool:
    """Ensures an emulator is running. If not, boots the specified AVD in the background."""
    adb = adb_path or resolve_adb_path()
    if is_emulator_running(adb):
        return True

    emu_exe = emulator_path or resolve_emulator_path()
    if not emu_exe or not os.path.exists(emu_exe):
        return False

    popen_kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
        )
    else:
        popen_kwargs["start_new_session"] = True

    try:
        subprocess.Popen(
            [emu_exe, "-avd", avd_name, "-no-snapshot", "-grpc", "8554"],
            **popen_kwargs,
        )
    except Exception:
        return False

    start_time = time.time()
    while time.time() - start_time < timeout_seconds:
        try:
            res = subprocess.run(
                [adb, "-s", "emulator-5554", "shell", "getprop", "sys.boot_completed"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if "1" in res.stdout:
                time.sleep(2)  # Stabilization
                return True
        except Exception:
            pass
        time.sleep(2)

    return False
