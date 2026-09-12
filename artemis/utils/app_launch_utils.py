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
#
# Portions of this file are derived from mobile-use (https://github.com/minitap-ai/mobile-use)
# Copyright 2025-2026 Minitap, Inc. Licensed under the Apache License 2.0.

"""Utilities for handling app locking and initial app launch logic."""

import asyncio
from dataclasses import dataclass, field
import re

from artemis.context import AppLaunchResult, ArtemisContext
from artemis.controllers.platform_specific_commands_controller import (
    get_adb_device,
    get_current_foreground_package_async,
)
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.data_engine.trace import TraceSpan
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


# ``ActivityRecord{<hash> u<user> <package>/<activity> t<task id>}`` as printed by
# ``dumpsys activity activities`` on every line that names an activity.
_ACTIVITY_RECORD_RE = re.compile(
    r"ActivityRecord\{[0-9a-f]+ u\d+ (?P<package>[\w.]+)/(?P<activity>[\w.$]+) t(?P<task>\d+)\}"
)
# ``* Task{<hash> #<id> type=... A=<uid>:<affinity> ... visible=true ...}`` task headers
# (``* TaskRecord{<hash> #<id> A=<affinity> ...}`` on Android 11 and older).
_TASK_HEADER_RE = re.compile(r"^\s*\* Task(?:Record)?\{[0-9a-f]+ #(?P<task>\d+)(?P<attrs>[^}]*)\}")
# Device-side pre-filter: the full dump runs to hundreds of KB on a busy device;
# only the task headers, history entries and resumed-activity lines are parsed.
_FOREGROUND_DUMP_CMD = (
    "dumpsys activity activities | grep -E"
    r" '^ *\* (Task|TaskRecord)\{|^ *\* Hist |ResumedActivity'"
)
# ``* Hist  #<index>: ActivityRecord{...}`` entries; index 0 is the task's base activity.
_HIST_RE = re.compile(r"^\s*\* Hist\s+#(?P<index>\d+): (?P<record>ActivityRecord\{[^}]*\})")
# ``topResumedActivity=ActivityRecord{...}`` (per task, top task first) and the global
# ``ResumedActivity: ActivityRecord{...}`` line; the first one seen is the foreground one.
_RESUMED_RE = re.compile(r"ResumedActivity[:=]\s*(?P<record>ActivityRecord\{[^}]*\})")


@dataclass(frozen=True)
class ForegroundTask:
    """What ``dumpsys activity activities`` says is on top of the display.

    ``resumed_package`` is the package of the resumed activity (what the user sees).
    ``base_package`` is the package of the task's root activity (``Hist #0``), and
    ``packages`` holds every package that has an activity in that task, so an app whose
    task currently shows a helper activity from another package (Settings search,
    permission dialogs, account pickers, system update pages, ...) is still
    recognised as the foreground app.
    """

    task_id: int | None = None
    affinity: str | None = None
    resumed_component: str | None = None
    resumed_package: str | None = None
    base_package: str | None = None
    packages: frozenset[str] = field(default_factory=frozenset)

    def owns(self, app_package: str) -> bool:
        """Whether ``app_package`` is the app the foreground task belongs to.

        The task affinity is the app's own claim on the task (by default the
        package name, or a package-prefixed name such as
        ``com.android.settings.root``); it stays with the task even when the
        app's root activity has been destroyed and only a companion activity
        (e.g. the Settings search UI) remains in the history."""
        affinity = self.affinity or ""
        return (
            app_package == self.resumed_package
            or app_package == self.base_package
            or app_package in self.packages
            or affinity == app_package
            or affinity.startswith(app_package + ".")
        )

    def describe(self) -> str:
        task = f"task #{self.task_id}" if self.task_id is not None else "task ?"
        details = [f"base={self.base_package}", f"resumed={self.resumed_component}"]
        if self.affinity:
            details.append(f"affinity={self.affinity}")
        others = sorted(self.packages - {self.base_package, self.resumed_package, None})
        if others:
            details.append(f"also={','.join(others)}")
        return f"{task} ({', '.join(details)})"


def parse_foreground_task(dump: str) -> ForegroundTask | None:
    """Parse ``dumpsys activity activities`` output into the foreground task summary.

    The foreground task is the one holding the resumed activity; when no activity is
    resumed (mid-transition) the first visible task with history entries is used.
    Returns None when the dump contains no task information at all.
    """
    task_attrs: dict[int, str] = {}
    task_order: list[int] = []
    task_activities: dict[int, list[tuple[int, str, str]]] = {}
    resumed: re.Match[str] | None = None

    for line in dump.splitlines():
        header = _TASK_HEADER_RE.match(line)
        if header:
            task_id = int(header.group("task"))
            if task_id not in task_attrs:
                task_attrs[task_id] = header.group("attrs")
                task_order.append(task_id)
            continue
        hist = _HIST_RE.match(line)
        if hist:
            record = _ACTIVITY_RECORD_RE.search(hist.group("record"))
            if record:
                task_activities.setdefault(int(record.group("task")), []).append(
                    (int(hist.group("index")), record.group("package"), record.group("activity"))
                )
            continue
        if resumed is None:
            match = _RESUMED_RE.search(line)
            if match:
                resumed = _ACTIVITY_RECORD_RE.search(match.group("record"))

    if not task_attrs and resumed is None:
        return None

    task_id: int | None = None
    if resumed is not None:
        task_id = int(resumed.group("task"))
    else:
        for candidate in task_order:
            if "visible=true" in task_attrs[candidate] and candidate in task_activities:
                task_id = candidate
                break

    activities = sorted(task_activities.get(task_id, []))
    affinity_match = re.search(r"\bA=(?:\d+:)?(\S+)", task_attrs.get(task_id, ""))
    return ForegroundTask(
        task_id=task_id,
        affinity=affinity_match.group(1) if affinity_match else None,
        resumed_component=(
            f"{resumed.group('package')}/{resumed.group('activity')}" if resumed else None
        ),
        resumed_package=resumed.group("package") if resumed else None,
        base_package=activities[0][1] if activities else None,
        packages=frozenset(package for _, package, _ in activities),
    )


def get_foreground_task(ctx: ArtemisContext) -> ForegroundTask | None:
    """Read the foreground task from ``dumpsys activity activities`` on the device.

    Blocking (a synchronous ADB shell round-trip); async callers go through
    :func:`get_foreground_task_async` so the event loop keeps serving.
    """
    try:
        device = get_adb_device(ctx)
        if device is None:
            return None
        dump = str(device.shell(_FOREGROUND_DUMP_CMD))
        if "Task" not in dump:
            # No grep on the device (or nothing matched): parse the full dump.
            dump = str(device.shell("dumpsys activity activities"))
        return parse_foreground_task(dump)
    except Exception as e:
        logger.debug(f"Failed to retrieve foreground task via dumpsys: {e}")
        return None


async def get_foreground_task_async(ctx: ArtemisContext) -> ForegroundTask | None:
    """:func:`get_foreground_task` off the event loop."""
    return await asyncio.to_thread(get_foreground_task, ctx)


async def _observe_foreground(
    ctx: ArtemisContext, app_package: str
) -> tuple[bool, str | None, ForegroundTask | None]:
    """Report whether ``app_package`` is in the foreground.

    Fast path: the focused window's package equals the target. Otherwise the app also
    counts as foreground when the top task belongs to it (see ``ForegroundTask``).
    A ``None`` focused package means the window manager is mid-transition and is
    reported as "not yet" without consulting the task stack.

    Returns (is_foreground, focused_package, foreground_task); the task is only read
    when the fast path fails on a non-null focused package.
    """
    current_package = await get_current_foreground_package_async(ctx)
    if current_package == app_package:
        return True, current_package, None
    if current_package is None:
        return False, None, None
    task = await get_foreground_task_async(ctx)
    return task is not None and task.owns(app_package), current_package, task


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
        ready, current_package, task = await _observe_foreground(ctx, app_package)

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

    current_package = await get_current_foreground_package_async(ctx)
    task = await get_foreground_task_async(ctx)
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
        already_foreground, current_package, _ = await _observe_foreground(ctx, locked_app_package)
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
