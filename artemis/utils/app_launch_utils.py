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

"""Utilities for handling app locking and initial app launch logic."""

import asyncio

from artemis.context import AppLaunchResult, ArtemisContext
from artemis.controllers.platform_specific_commands_controller import (
    get_adb_device,
    get_current_foreground_package_async,
)
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.data_engine.trace import TraceSpan
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


SYSTEM_OVERLAYS_VALID_FOR_LAUNCH = {
    "com.google.android.permissioncontroller",
    "com.android.permissioncontroller",
    "com.google.android.packageinstaller",
    "com.android.packageinstaller",
}


def get_focused_task_package(ctx: ArtemisContext) -> str | None:
    """Inspect the active/focused task stack to find the package affinity of the top task.

    Uses adb shell dumpsys activity activities to extract this info.
    """
    try:
        device = get_adb_device(ctx)
        output = str(device.shell("dumpsys activity activities | grep topDisplayFocusedRootTask"))
        if "topDisplayFocusedRootTask=" in output:
            segment = output.split("topDisplayFocusedRootTask=")[-1]
            if "A=" in segment:
                affinity = segment.split("A=")[-1].split("}")[0].split()[0].strip()
                if ":" in affinity:
                    return affinity.split(":")[-1]
                return affinity
            if "{" in segment and "}" in segment:
                braces_content = segment.split("{")[-1].split("}")[0]
                tokens = braces_content.split()
                for token in tokens:
                    if "." in token and not token.startswith("#") and not token.isdigit():
                        if ":" in token:
                            return token.split(":")[-1]
                        return token
        return None
    except Exception as e:
        logger.debug(f"Failed to retrieve focused task package via dumpsys: {e}")
        return None


async def _poll_for_app_ready(
    ctx: ArtemisContext,
    app_package: str,
    max_poll_seconds: int = 15,
    poll_interval: float = 1.0,
) -> tuple[bool, str | None]:
    """Poll for app to be ready after launch.

    Treats mCurrentFocus=null as a loading state and keeps polling.
    Only fails if we get a different (non-null) package or timeout.

    Args:
        ctx: Mobile use context
        app_package: Expected package name
        max_poll_seconds: Maximum time to poll (default: 15s)
        poll_interval: Time between polls (default: 1s)

    Returns:
        Tuple of (success: bool, error_message: str | None)
    """
    polls = int(max_poll_seconds / poll_interval)

    for i in range(polls):
        current_package = await get_current_foreground_package_async(ctx)

        if current_package == app_package:
            logger.success(f"App {app_package} is ready (took ~{i * poll_interval:.1f}s)")
            return True, None

        if current_package in SYSTEM_OVERLAYS_VALID_FOR_LAUNCH:
            task_package = get_focused_task_package(ctx)
            if task_package == app_package:
                logger.success(
                    f"App {app_package} launch succeeded (system overlay"
                    f" '{current_package}' focused, but top task stack affinity"
                    f" matches expected app package '{task_package}', took"
                    f" ~{i * poll_interval:.1f}s)"
                )
                return True, None

        if current_package is None:
            logger.debug(f"Poll {i + 1}/{polls}: App loading (mCurrentFocus=null)...")
        else:
            logger.debug(
                f"Poll {i + 1}/{polls}: Wrong app in foreground (expected"
                f" '{app_package}', got '{current_package}'). Still waiting..."
            )

        if i < polls - 1:
            await asyncio.sleep(poll_interval)

    current_package = await get_current_foreground_package_async(ctx)
    error_msg = (
        f"Timeout waiting for {app_package} to load after {max_poll_seconds}s. "
        f"Current foreground: {current_package}"
    )
    logger.error(error_msg)
    return False, error_msg


async def launch_app_with_retries(
    ctx: ArtemisContext,
    app_package: str,
    max_retries: int = 3,
    max_poll_seconds: int = 15,
) -> tuple[bool, str | None]:
    """Launch an app with retry logic and smart polling.

    Args:
        ctx: Mobile use context
        app_package: Package name (Android) to launch
        max_retries: Maximum number of launch attempts (default: 3)
        max_poll_seconds: Maximum time to wait for app to load per attempt
          (default: 15s)

    Returns:
        Tuple of (success: bool, error_message: str | None)
    """

    for attempt in range(1, max_retries + 1):
        logger.info(f"Launch attempt {attempt}/{max_retries} for app {app_package}")

        with TraceSpan(
            name=f"Launch Attempt {attempt}",
            trace_type="span",
            ctx=ctx,
        ) as span:
            span.payload = {"attempt": attempt, "app_package": app_package}

            controller = UnifiedMobileController(ctx)
            if attempt > 1:
                logger.warning(
                    f"Attempt {attempt - 1} failed. Force stopping"
                    f" '{app_package}' to clear frozen state before retrying..."
                )
                await controller.terminate_app(app_package)
                await asyncio.sleep(1.0)

            launch_success = await controller.launch_app(app_package)
            if not launch_success:
                error_msg = f"Failed to execute launch command for {app_package}"
                logger.error(error_msg)
                span.status = "failed"
                span.error = error_msg
                if attempt == max_retries:
                    return False, error_msg
                await asyncio.sleep(2)
                continue

            await asyncio.sleep(1)

            success, error_msg = await _poll_for_app_ready(ctx, app_package, max_poll_seconds)

            if success:
                span.status = "success"
                span.result = "App is ready"
                return True, None

            span.status = "failed"
            span.error = error_msg

            if attempt < max_retries:
                logger.warning(f"Attempt {attempt} failed: {error_msg}. Retrying...")
                await asyncio.sleep(1)

    error_msg = f"Failed to launch {app_package} after {max_retries} attempts"
    logger.error(error_msg)
    return False, error_msg


async def _handle_initial_app_launch(
    ctx: ArtemisContext,
    locked_app_package: str,
) -> AppLaunchResult:
    """Handle initial app launch verification and launching if needed.

    If locked_app_package is set:
    1. Check if the app is already in the foreground
    2. If not, attempt to launch it (with retries)
    3. Return status with success/error information

    Args:
        ctx: Mobile use context
        locked_app_package: Package name (Android) to lock to

    Returns:
        AppLaunchResult with launch status and error information
    """
    if not locked_app_package:
        error_msg = f"Invalid locked_app_package: '{locked_app_package}'"
        logger.error(error_msg)
        return AppLaunchResult(
            locked_app_package=locked_app_package,
            locked_app_initial_launch_success=False,
            locked_app_initial_launch_error=error_msg,
        )

    logger.info(f"Starting initial app launch for package: {locked_app_package}")

    try:
        current_package = await get_current_foreground_package_async(ctx)
        logger.info(f"Current foreground app: {current_package}")

        if current_package == locked_app_package:
            logger.info(f"App {locked_app_package} is already in foreground")
            return AppLaunchResult(
                locked_app_package=locked_app_package,
                locked_app_initial_launch_success=True,
                locked_app_initial_launch_error=None,
            )

        logger.info(f"App {locked_app_package} not in foreground, attempting to launch")
        success, error_msg = await launch_app_with_retries(ctx, locked_app_package)

        return AppLaunchResult(
            locked_app_package=locked_app_package,
            locked_app_initial_launch_success=success,
            locked_app_initial_launch_error=error_msg,
        )

    except Exception as e:
        error_msg = f"Exception during initial app launch: {str(e)}"
        logger.error(error_msg)
        return AppLaunchResult(
            locked_app_package=locked_app_package,
            locked_app_initial_launch_success=False,
            locked_app_initial_launch_error=error_msg,
        )
