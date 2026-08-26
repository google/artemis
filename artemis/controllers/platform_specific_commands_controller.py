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

import os
from shutil import which
import time
from typing import Any

from artemis.context import ArtemisContext, DevicePlatform
from artemis.utils.logger import ArtemisLogger, get_logger
from artemis.utils.shell_utils import run_shell_command_on_host

logger = get_logger(__name__)


def get_adb_device(ctx: ArtemisContext) -> Any:
    """Retrieve the ADB device instance (AdbDevice locally or RemoteAdbDevice in Cloud mode)."""
    if ctx.adb_client is None:
        if os.environ.get("ARTEMIS_CLOUD_MODE") == "1":
            from cloud_service.virtualization import RemoteAdbClient

            ctx.adb_client = RemoteAdbClient()
            return ctx.adb_client.device(serial=ctx.device.device_id)
        return None

    if ctx.device.mobile_platform != DevicePlatform.ANDROID:
        return None

    adb = ctx.get_adb_client()
    return adb.device(serial=ctx.device.device_id)


def get_first_device(
    logger: ArtemisLogger | None = None,
) -> tuple[str | None, DevicePlatform | None, None]:
    """Gets the first available device, prioritizing idle and unassigned devices."""
    try:
        from artemis.runtime import device_pool

        chosen = device_pool.select_device()
        if chosen:
            return chosen, DevicePlatform.ANDROID, None
    except Exception as exc:
        if logger:
            logger.debug(f"Device pool selection fallback: {exc}")

    if which("adb"):
        try:
            android_output = run_shell_command_on_host("adb devices")
            lines = android_output.strip().split("\n")
            for line in lines:
                if "device" in line and not line.startswith("List of devices"):
                    return line.split()[0], DevicePlatform.ANDROID, None
        except RuntimeError as e:
            if logger:
                logger.error(f"ADB command failed: {e}")

    return None, None, None


def get_device_date(ctx: ArtemisContext) -> str:
    device = get_adb_device(ctx)
    if not device:
        return time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        return str(device.shell("date")).strip()
    except Exception as e:
        logger.debug(f"Failed to get device date: {e}")
        return time.strftime("%Y-%m-%d %H:%M:%S")


def list_packages(ctx: ArtemisContext) -> str:
    """List installed packages dynamically from the physical/remote device."""
    device = get_adb_device(ctx)
    if not device:
        return ""

    try:
        cmd = ["pm", "list", "packages", "-f"]
        raw_output = str(device.shell(" ".join(cmd)))
    except Exception as e:
        logger.error(f"Failed to query package list from device: {e}")
        return ""

    lines = raw_output.strip().split("\n")
    packages = []
    for line in lines:
        if "=" in line:
            package_name = line.split("=")[-1].strip()
            if package_name:
                packages.append(package_name)
        elif line.startswith("package:"):
            package_name = line.split("package:")[-1].strip()
            if package_name:
                packages.append(package_name)

    return "\n".join(sorted(packages))


async def list_packages_async(ctx: ArtemisContext) -> str:
    return list_packages(ctx)


def get_current_foreground_package(ctx: ArtemisContext) -> str | None:
    """Get the package name of the currently focused/foreground app dynamically."""
    device = get_adb_device(ctx)
    if not device:
        return None

    try:
        app_info = device.current_app()
        if app_info and app_info.package:
            return app_info.package
    except Exception as e:
        logger.debug(f"device.current_app failed: {e}. Falling back to dumpsys.")

    try:
        output = str(device.shell("dumpsys window | grep mCurrentFocus"))
        if "mCurrentFocus=" in output:
            segment = output.split("mCurrentFocus=")[-1]
            if "/" in segment:
                tokens = segment.split()
                for token in tokens:
                    if "." in token and not token.startswith("Window"):
                        package = token.split("/")[0]
                        package = package.rstrip("}")
                        if package and "." in package:
                            return package
    except Exception as e:
        logger.debug(f"Fallback dumpsys parsing failed: {e}")

    return None


async def get_current_foreground_package_async(ctx: ArtemisContext) -> str | None:
    return get_current_foreground_package(ctx)
