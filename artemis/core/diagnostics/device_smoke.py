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

"""End-to-end device observation smoke test.

The readiness ADB probe only proves that ``adb devices`` and ``dumpsys`` answer.
The most common "device shows Connected but ARTEMIS cannot observe it" failure
is UIAutomator not answering (screenshot / hierarchy dump hangs), which today
only surfaces as a failed task. :func:`smoke_test_device` performs exactly the
observation the ``mobile_get_device_state`` MCP tool performs -- build (or reuse)
the cached device controller through ``artemis.mcp.adb_server._get_controller``
and call ``controller.get_screen_data()`` -- and reports a structured verdict
with concrete repair guidance instead of raising.

Design notes
------------
* ``_get_controller`` is synchronous, caches one controller per serial for the
  life of the process and never touches ``DeviceExecutionLock``. Its first call
  for a serial blocks on ``UIAutomatorClient.get_screen_data()`` (three
  ``u2.connect`` attempts plus a screenshot and hierarchy dump), so it runs on a
  worker thread here.
* ``AdbDriver.get_screen_data`` invokes the synchronous UIAutomator client on
  the event-loop thread. A plain ``asyncio.wait_for`` around the coroutine can
  therefore never fire while UIAutomator hangs. The coroutine is executed on a
  dedicated daemon thread with its own event loop, and the deadline is enforced
  on the join. A thread abandoned by a timeout finishes on its own once the
  underlying adb / HTTP timeouts elapse; being a daemon it never delays
  interpreter shutdown.
* ``u2.connect`` restarts the on-device UIAutomator server when it believes the
  server is unhealthy. Running that against a device another ARTEMIS process is
  driving would break the running task's session, so a device with a live
  ``DeviceExecutionLock`` owner is reported as busy instead of probed.
* Nothing is left behind: no screenshot is written to disk, no lock is taken,
  and -- mirroring ``mobile_get_device_state`` -- the cached controller is kept
  for reuse rather than closed.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
import os
import threading
import time
from typing import Any, TypeVar

from artemis.runtime.device_lock import DeviceExecutionLock
from artemis.utils.logger import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

#: On-device packages installed by uiautomator2 (``python -m uiautomator2 init``).
UIAUTOMATOR_PACKAGES: tuple[str, ...] = (
    "com.github.uiautomator",
    "com.github.uiautomator.test",
)

#: A real JPEG screenshot of any phone screen is tens of kilobytes; the Android
#: driver substitutes a 1x1 PNG (~70 bytes) when every capture path failed.
_MIN_PLAUSIBLE_SCREENSHOT_BYTES = 512

_SERIAL_PLACEHOLDER = "<serial>"


def _serial_or_placeholder(serial: str | None) -> str:
    return serial or _SERIAL_PLACEHOLDER


def _uiautomator_fix(serial: str | None) -> list[str]:
    s = _serial_or_placeholder(serial)
    force_stops = ", then ".join(
        f"adb -s {s} shell am force-stop {pkg}" for pkg in UIAUTOMATOR_PACKAGES
    )
    return [
        "Unlock the phone and keep the screen on (UIAutomator cannot dump a locked or sleeping screen).",
        f"Restart the on-device UIAutomator server, then retry: {force_stops}",
        (
            "If it keeps failing, reinstall the UIAutomator server APKs: "
            f"python -m uiautomator2 purge --serial {s}, then python -m uiautomator2 init --serial {s} "
            "(run with the ARTEMIS virtualenv Python), or reboot the device."
        ),
    ]


def fix_for_error(error: str | None, serial: str | None) -> list[str]:
    """Map an error string to concrete repair steps (substring match, case-insensitive)."""
    if not error:
        return []
    text = error.lower()
    s = _serial_or_placeholder(serial)

    if "busy" in text or "another task" in text:
        return [
            "Wait for the running task to finish, or stop it with mobile_manage_task(action='stop', ...).",
            "Then rerun the smoke test.",
        ]
    if "unauthorized" in text:
        return [
            "Accept the 'Allow USB debugging' prompt on the phone (tick 'Always allow from this computer').",
            f"If no prompt appears, re-plug the USB cable and run: adb -s {s} devices",
        ]
    if "offline" in text:
        return [
            "Re-plug the USB cable (try another port or cable).",
            "Run: adb reconnect (or adb kill-server, then adb start-server) and check adb devices -l.",
        ]
    if "no android devices" in text or "no device" in text or "not found" in text:
        return [
            "Connect an Android device with USB debugging enabled, or start an emulator.",
            "mobile_diagnose(launch_avd='<avd name>') boots a named AVD; adb devices -l lists what is attached.",
        ]
    if (
        "timeout" in text
        or "timed out" in text
        or "uiautomator" in text
        or "not responding" in text
        or "did not respond" in text
        or "hierarchy" in text
        or "screenshot" in text
        or "http" in text
        or "deadobject" in text
    ):
        return _uiautomator_fix(serial)
    return [
        f"Check the raw hierarchy dump manually: adb -s {s} shell uiautomator dump, then adb -s {s} shell cat /sdcard/window_dump.xml",
        f"Check the raw screenshot path: adb -s {s} exec-out screencap -p > screen.png",
    ]


#: Exceptions the smoke test must never swallow.
_PASSTHROUGH_EXCEPTIONS: tuple[type[BaseException], ...] = (
    KeyboardInterrupt,
    SystemExit,
    asyncio.CancelledError,
)


def _describe_failure(exc: BaseException) -> str:
    """uiautomator2 errors derive from ``BaseException`` and often carry an empty message."""
    text = str(exc).strip()
    name = type(exc).__name__
    if not text:
        return name
    return text if name in ("Exception", "RuntimeError") else f"{name}: {text}"


def _resolve_requested_serial(device_serial: str | None) -> str | None:
    """Mirror ``_get_controller``'s serial resolution (explicit arg, then env)."""
    return (
        device_serial or os.environ.get("ARTEMIS_DEVICE_ID") or os.environ.get("ADB_DEVICE_SERIAL")
    )


def _find_busy_owner(target_serial: str | None) -> tuple[str, Any] | None:
    """Return ``(device_key, owner)`` when a live ARTEMIS process owns the target device.

    With no target serial ``_get_controller`` would pick the first attached device,
    which cannot be matched against lock records cheaply; any live owner is then
    treated as busy to stay on the safe side.
    """
    try:
        owners = DeviceExecutionLock.get_active_owners()
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.debug(f"Device lock inspection failed, skipping busy guard: {exc}")
        return None
    if not owners:
        return None
    if target_serial is None:
        key, owner = next(iter(owners.items()))
        return key, owner
    clean = DeviceExecutionLock._normalize_device_id(target_serial)
    for key, owner in owners.items():
        if key == clean or key.endswith(f"__{clean}"):
            return key, owner
    return None


async def _run_in_daemon_thread(fn: Callable[[], T], timeout: float, what: str) -> T:
    """Run ``fn`` on a daemon thread and wait at most ``timeout`` seconds.

    Raises :class:`TimeoutError` on deadline. The thread keeps running until
    ``fn`` returns on its own, but the caller is released immediately.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[T] = loop.create_future()

    def _deliver(setter: Callable[[], None]) -> None:
        try:
            loop.call_soon_threadsafe(setter)
        except RuntimeError:
            # Event loop already closed: the caller gave up on us.
            pass

    def _worker() -> None:
        try:
            value = fn()
        except BaseException as exc:  # pylint: disable=broad-exception-caught
            error: BaseException = exc

            def _fail() -> None:
                if not future.done():
                    future.set_exception(error)

            _deliver(_fail)
            return

        def _succeed() -> None:
            if not future.done():
                future.set_result(value)

        _deliver(_succeed)

    threading.Thread(target=_worker, name=f"artemis-smoke-{what}", daemon=True).start()
    try:
        return await asyncio.wait_for(future, timeout=timeout)
    except TimeoutError:
        logger.warning(f"Device smoke test: {what} did not finish within {timeout:.1f}s")
        raise


def _run_coroutine_blocking(coro_factory: Callable[[], Any]) -> Any:
    """Execute a coroutine on a fresh event loop owned by the current thread."""
    return asyncio.run(coro_factory())


def _count_elements(elements: Any) -> int | None:
    if elements is None:
        return None
    if isinstance(elements, str):
        return elements.count("<node")
    if isinstance(elements, dict):
        return 1
    try:
        return len(elements)
    except TypeError:
        return None


def _decoded_length(b64: Any) -> int | None:
    if not isinstance(b64, str) or not b64:
        return None
    try:
        return len(base64.b64decode(b64))
    except (ValueError, TypeError):
        return None


async def smoke_test_device(
    device_serial: str | None = None, timeout_seconds: float = 20.0
) -> dict[str, Any]:
    """Observe the device exactly like ``mobile_get_device_state`` and report a verdict.

    Never raises. Returns::

        {"ok": bool, "serial": str | None, "elapsed_seconds": float,
         "screenshot_bytes": int | None, "element_count": int | None,
         "error": str | None, "fix": list[str]}
    """
    started = time.monotonic()
    requested_serial = _resolve_requested_serial(device_serial)
    result: dict[str, Any] = {
        "ok": False,
        "serial": requested_serial,
        "elapsed_seconds": 0.0,
        "screenshot_bytes": None,
        "element_count": None,
        "error": None,
        "fix": [],
    }

    def _finish(error: str | None = None, cause: str | None = None) -> dict[str, Any]:
        """Record the verdict; ``cause`` is the raw failure text used to pick the fix."""
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        result["error"] = error
        result["ok"] = error is None
        result["fix"] = fix_for_error(cause if cause is not None else error, result["serial"])
        return result

    busy = _find_busy_owner(requested_serial)
    if busy is not None:
        key, owner = busy
        description = getattr(owner, "description", "") or "unknown task"
        pid = getattr(owner, "pid", "?")
        result["serial"] = requested_serial or getattr(owner, "device_id", None)
        return _finish(
            f"Device {key} is busy: another task is running on it ({description}, pid {pid});"
            " skipping the screen capture so the running task is not disturbed."
        )

    try:
        from artemis.mcp import adb_server

        controller = await _run_in_daemon_thread(
            lambda: adb_server._get_controller(device_serial=device_serial),
            timeout_seconds,
            "controller-init",
        )
    except TimeoutError:
        return _finish(
            f"UIAutomator/controller initialization did not respond within {timeout_seconds:g}s"
        )
    except _PASSTHROUGH_EXCEPTIONS:
        raise
    except BaseException as exc:  # pylint: disable=broad-exception-caught
        cause = _describe_failure(exc)
        return _finish(f"Failed to initialize Android device controller: {cause}", cause)

    controller_ctx = getattr(controller, "ctx", None)
    device = getattr(controller_ctx, "device", None)
    device_id = getattr(device, "device_id", None)
    if isinstance(device_id, str) and device_id:
        result["serial"] = device_id

    remaining = max(0.5, timeout_seconds - (time.monotonic() - started))
    try:
        device_data = await _run_in_daemon_thread(
            lambda: _run_coroutine_blocking(controller.get_screen_data),
            remaining,
            "screen-data",
        )
    except TimeoutError:
        return _finish(f"UIAutomator/screen capture did not respond within {timeout_seconds:g}s")
    except _PASSTHROUGH_EXCEPTIONS:
        raise
    except BaseException as exc:  # pylint: disable=broad-exception-caught
        cause = _describe_failure(exc)
        return _finish(f"Screen capture failed: {cause}", cause)

    result["screenshot_bytes"] = _decoded_length(getattr(device_data, "base64", None))
    result["element_count"] = _count_elements(getattr(device_data, "elements", None))

    if result["screenshot_bytes"] is None:
        return _finish("Screen capture returned no screenshot data")
    if result["screenshot_bytes"] < _MIN_PLAUSIBLE_SCREENSHOT_BYTES:
        return _finish(
            f"Screenshot capture failed (driver returned a {result['screenshot_bytes']}-byte placeholder image)"
        )
    if not result["element_count"]:
        return _finish(
            "UIAutomator hierarchy dump returned no UI elements (screenshot worked, hierarchy did not)"
        )
    return _finish(None)


__all__ = ["UIAUTOMATOR_PACKAGES", "fix_for_error", "smoke_test_device"]
