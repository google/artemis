# Copyright 2025-2026 Minitap, Inc.
# Modifications Copyright 2026 Google LLC
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
#
# Originally from mobile-use (https://github.com/minitap-ai/mobile-use).
# See third_party/mobile_use/METADATA for the upstream source and local modifications.

"""Utilities for handling app locking and initial app launch logic."""

import asyncio

from artemis.context import AppLaunchResult, ArtemisContext
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.data_engine.trace import TraceSpan
from artemis.utils.app_launch_utils import current_foreground, observe_foreground
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)


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
        ready, current_package, task = await observe_foreground(ctx, app_package)

        if ready:
            if current_package == app_package:
                logger.success(f"App {app_package} is ready (took ~{i * poll_interval:.1f}s)")
            else:
                logger.success(
                    f"App {app_package} is ready (focused window belongs to"
                    f" '{current_package}', but the foreground {task.describe()} is owned"
                    f" by {app_package}, took ~{i * poll_interval:.1f}s)"
                )
            return True, None

        if current_package is None:
            logger.debug(f"Poll {i + 1}/{polls}: App loading (mCurrentFocus=null)...")
        else:
            logger.debug(
                f"Poll {i + 1}/{polls}: Wrong app in foreground (expected"
                f" '{app_package}', got '{current_package}', foreground"
                f" {task.describe() if task else 'task unknown'}). Still waiting..."
            )

        if i < polls - 1:
            await asyncio.sleep(poll_interval)

    current_package, task = await current_foreground(ctx)
    error_msg = (
        f"Timeout waiting for {app_package} to load after {max_poll_seconds}s. "
        f"Current foreground: {current_package}; foreground"
        f" {task.describe() if task else 'task unknown'}"
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
        already_foreground, current_package, _ = await observe_foreground(ctx, locked_app_package)
        logger.info(f"Current foreground app: {current_package}")

        if already_foreground:
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
