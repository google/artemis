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

"""Select the UI-hierarchy backend for a device and build its screen client.

Three backends, chosen by ``ARTEMIS_HIERARCHY_BACKEND`` (environment or
``.env``):

* ``auto`` (default): the Accessibility Helper, with UIAutomator2 as a
  per-call fallback. UIAutomator2 is only constructed when the helper fails,
  so a healthy helper never triggers ``u2.connect`` (which pushes its own APKs
  and uninstalls Maestro).
* ``helper``: the helper only. Failures surface as errors; use it in CI to
  prove the helper is really what serves the hierarchy.
* ``uiautomator``: the pre-helper path, unchanged.

Every entrypoint that used to build ``UIAutomatorClient`` directly (the SDK
agent, the MCP device controller, and through it the device smoke test) goes
through :func:`create_screen_client`, so the switch applies everywhere.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum
import os
import subprocess
import time
from typing import Any

from PIL import Image

from artemis.clients.accessibility_client import AccessibilityClient, HelperUnavailable
from artemis.clients.ui_automator_client import UIAutomatorClient, UIAutomatorScreenData
from artemis.config.constants import ENV_ARTEMIS_HIERARCHY_BACKEND
from artemis.runtime.adb_endpoint import adb_command
from artemis.runtime.helper_manager import ProvisionEvent
from artemis.utils.logger import get_logger

logger = get_logger(__name__)

#: ``(previous_backend, new_backend, reason)``; ``previous_backend`` is None on first use.
BackendChangeListener = Callable[[str | None, str, str | None], None]


class DeviceOfflineError(RuntimeError):
    """The device is not attached / authorized; no backend can serve it.

    Raised instead of trying UIAutomator2 when the helper fails because the
    phone itself is gone, so the failure names the real cause and does not
    burn UIAutomator2's own multi-attempt connect timeout.
    """

    def __init__(self, device_id: str, state: str | None):
        self.device_id = device_id
        self.state = state
        detail = f"adb reports state '{state}'" if state else "adb does not list it"
        super().__init__(
            f"Device {device_id} is not available ({detail}). Reconnect the device or "
            "accept the USB debugging prompt, then retry."
        )


def device_state(device_id: str, timeout: float = 5.0) -> str | None:
    """``adb get-state`` for one serial: 'device', 'offline', 'unauthorized', or None."""
    try:
        result = subprocess.run(
            adb_command(["-s", device_id, "get-state"]),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (result.stdout or "").strip()
    if result.returncode != 0 or not text:
        # "error: device unauthorized" / "device offline" arrive on stderr.
        err = (result.stderr or "").lower()
        for known in ("unauthorized", "offline"):
            if known in err:
                return known
        return None
    return text


class HierarchyBackend(str, Enum):
    AUTO = "auto"
    HELPER = "helper"
    UIAUTOMATOR = "uiautomator"


def resolve_backend(value: str | HierarchyBackend | None = None) -> HierarchyBackend:
    """Backend from an explicit value, else the environment, else settings, else ``auto``."""
    if isinstance(value, HierarchyBackend):
        return value
    text = value
    if text is None:
        text = os.environ.get(ENV_ARTEMIS_HIERARCHY_BACKEND)
    if text is None:
        try:
            from artemis.config import settings

            text = getattr(settings, "ARTEMIS_HIERARCHY_BACKEND", None)
        except (ImportError, ValueError):
            text = None
    normalized = (text or "auto").strip().lower()
    try:
        return HierarchyBackend(normalized)
    except ValueError:
        logger.warning(
            f"Unknown hierarchy backend {text!r}; valid values are "
            f"{', '.join(b.value for b in HierarchyBackend)}. Using 'auto'."
        )
        return HierarchyBackend.AUTO


class FallbackScreenClient:
    """Helper first, UIAutomator2 on failure, with a retry window for the helper.

    After a helper failure the client serves from UIAutomator2 for
    ``retry_after`` seconds before trying the helper again, so one dead tunnel
    does not cost a 5-second timeout on every step.
    """

    def __init__(
        self,
        device_id: str,
        helper: AccessibilityClient | None = None,
        uiautomator_factory: Callable[[], UIAutomatorClient] | None = None,
        *,
        retry_after: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
        state_probe: Callable[[str], str | None] = device_state,
    ) -> None:
        self._device_id = device_id
        self._helper = helper or AccessibilityClient(device_id)
        self._uiautomator_factory = uiautomator_factory or (lambda: UIAutomatorClient(device_id))
        self._uiautomator: UIAutomatorClient | None = None
        self._retry_after = retry_after
        self._clock = clock
        self._state_probe = state_probe
        self._helper_down_until: float | None = None
        self._active_backend: str | None = None
        self._last_failure: str | None = None
        #: Every backend switch, oldest first: {time, backend, reason}.
        self.backend_history: list[dict[str, Any]] = []
        self._listeners: list[BackendChangeListener] = []

    # ------------------------------------------------------------------ #

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def helper(self) -> AccessibilityClient:
        return self._helper

    @property
    def active_backend(self) -> str | None:
        """Backend that served the last call: ``"helper"``, ``"uiautomator"`` or ``None``."""
        return self._active_backend

    @property
    def uiautomator(self) -> UIAutomatorClient:
        if self._uiautomator is None:
            self._uiautomator = self._uiautomator_factory()
        return self._uiautomator

    def add_backend_listener(self, listener: BackendChangeListener) -> None:
        """Be told whenever the serving backend changes (first use included)."""
        self._listeners.append(listener)

    def remove_backend_listener(self, listener: BackendChangeListener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _set_active(self, backend: str, reason: str | None = None) -> None:
        previous = self._active_backend
        if previous == backend:
            return
        self._active_backend = backend
        self.backend_history.append({"time": time.time(), "backend": backend, "reason": reason})
        for listener in list(self._listeners):
            try:
                listener(previous, backend, reason)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.debug(f"Backend listener failed: {exc}")

    def _ensure_device_online(self) -> None:
        """Fail fast with the real cause when the phone itself is gone."""
        state = self._state_probe(self._device_id)
        if state != "device":
            raise DeviceOfflineError(self._device_id, state)

    def _helper_allowed(self) -> bool:
        if self._helper_down_until is None:
            return True
        if self._clock() >= self._helper_down_until:
            self._helper_down_until = None
            return True
        return False

    def _degrade(self, reason: BaseException) -> None:
        # Never fall back to UIAutomator2 for a phone that is simply not there.
        self._ensure_device_online()
        self._last_failure = f"{reason.__class__.__name__}: {reason}"
        if self._helper_down_until is None:
            logger.warning(
                f"Accessibility helper unavailable on {self._device_id} ({reason}); "
                f"serving from UIAutomator2 and retrying the helper in {self._retry_after:g}s."
            )
        self._helper_down_until = self._clock() + self._retry_after

    def _release_uiautomation(self) -> None:
        """Stop our UIAutomator2 server before giving the helper another chance.

        Its UiAutomation connection keeps every accessibility service unbound,
        so while it lives the helper cannot answer and the retry would be a
        guaranteed failure. UIAutomator2 restarts its server by itself on the
        next call if the helper turns out to be still dead.
        """
        if self._uiautomator is not None and self._active_backend == "uiautomator":
            self._uiautomator.stop_server()

    def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        if self._helper_allowed():
            self._release_uiautomation()
            try:
                result = getattr(self._helper, name)(*args, **kwargs)
            except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
                self._degrade(exc)
            else:
                self._set_active("helper", "helper recovered" if self._last_failure else None)
                self._last_failure = None
                return result
        result = getattr(self.uiautomator, name)(*args, **kwargs)
        self._set_active("uiautomator", self._last_failure)
        return result

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def connect(self, on_event: ProvisionEvent | None = None) -> None:
        self._release_uiautomation()
        self.backend_history.clear()
        self._active_backend = None
        if self._helper_allowed():
            self._release_uiautomation()
            try:
                self._helper.connect(on_event=on_event)
            except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
                self._degrade(exc)
            else:
                self._set_active("helper")
                return
        self.uiautomator.connect()
        self._set_active("uiautomator", self._last_failure)

    def disconnect(self) -> None:
        try:
            self._helper.disconnect()
        finally:
            if self._uiautomator is not None:
                # Release UiAutomation so the helper is back for the next task
                # on this device (possibly served by another process).
                self._uiautomator.disconnect(stop_server=True)
            self._active_backend = None

    # ------------------------------------------------------------------ #
    # Screen data and input, same surface as UIAutomatorClient
    # ------------------------------------------------------------------ #

    def get_screen_data(self) -> UIAutomatorScreenData:
        return self._call("get_screen_data")

    def get_hierarchy(self) -> str:
        return self._call("get_hierarchy")

    def get_screenshot(self) -> Image.Image | None:
        return self._call("get_screenshot")

    def get_screenshot_base64(self) -> str | None:
        return self._call("get_screenshot_base64")

    def set_clipboard(self, text: str) -> bool:
        return self._call("set_clipboard", text)

    def send_text(self, text: str) -> Any:
        return self._call("send_text", text)

    def clear_text(self) -> bool:
        return self._call("clear_text")

    def press_key(self, key: str) -> Any:
        return self._call("press_key", key)


ScreenClient = AccessibilityClient | UIAutomatorClient | FallbackScreenClient


def create_screen_client(
    device_id: str, backend: str | HierarchyBackend | None = None
) -> ScreenClient:
    """Build the screen client for ``device_id`` according to the configured backend."""
    resolved = resolve_backend(backend)
    if resolved is HierarchyBackend.UIAUTOMATOR:
        return UIAutomatorClient(device_id=device_id)
    if resolved is HierarchyBackend.HELPER:
        return AccessibilityClient(device_id)
    return FallbackScreenClient(device_id)


def describe_backend(client: Any) -> str | None:
    """Best-effort name of the backend a client is serving from, for diagnostics."""
    active = getattr(client, "active_backend", None)
    if active:
        return str(active)
    if isinstance(client, UIAutomatorClient):
        return "uiautomator"
    if isinstance(client, AccessibilityClient):
        return "helper"
    return None


def helper_version(client: Any) -> str | None:
    """Version name of the helper a client is attached to, if any."""
    helper = getattr(client, "helper", None) or client
    session = getattr(helper, "session", None)
    name = getattr(session, "version_name", None)
    return str(name) if name else None


def hierarchy_backend_summary(client: Any) -> dict[str, Any] | None:
    """Machine-readable account of which backend served a run, for reports."""
    backend = describe_backend(client)
    if backend is None:
        return None
    history = list(getattr(client, "backend_history", None) or [])
    return {
        "backend": backend,
        "helper_version": helper_version(client),
        "switches": history[1:] if len(history) > 1 else [],
    }


_BACKEND_LABEL = {"helper": "the Artemis accessibility helper", "uiautomator": "UIAutomator2"}


def hierarchy_backend_sentence(
    client: Any, relative_time: Callable[[float], str] | None = None
) -> str | None:
    """One plain sentence for reports: what served the hierarchy and whether it changed."""
    summary = hierarchy_backend_summary(client)
    if summary is None:
        return None
    history = getattr(client, "backend_history", None) or []
    first = (history[0].get("backend") if history else None) or summary["backend"]
    text = f"UI hierarchy source: {_BACKEND_LABEL.get(first, first)}"
    if first == "helper" and summary.get("helper_version"):
        text += f" v{summary['helper_version']}"
    for switch in summary["switches"]:
        when = relative_time(switch["time"]) if relative_time else None
        text += f"; switched to {_BACKEND_LABEL.get(switch['backend'], switch['backend'])}"
        if when:
            text += f" at {when}"
        if switch.get("reason"):
            text += f" because {str(switch['reason']).rstrip('. ')}"
    return text + "."


__all__ = [
    "BackendChangeListener",
    "DeviceOfflineError",
    "FallbackScreenClient",
    "HelperUnavailable",
    "HierarchyBackend",
    "ScreenClient",
    "create_screen_client",
    "describe_backend",
    "device_state",
    "helper_version",
    "hierarchy_backend_sentence",
    "hierarchy_backend_summary",
    "resolve_backend",
]
