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

"""HTTP client for the Artemis Accessibility Helper running on a device.

A lightweight, conflict-free alternative to UIAutomator2: it never takes the
singleton ``UiAutomationConnection``, so it runs alongside Mobly, Appium and
Espresso. This class only speaks the helper's HTTP API. Installing, enabling,
port forwarding, the session token and hot-plug recovery live in
:mod:`artemis.runtime.helper_manager`; this client asks the manager for a
session lazily and hands transport failures back to it once.

The screen-data contract is byte-for-byte the one :class:`UIAutomatorClient`
exposes (``UIAutomatorScreenData`` with the element dictionaries produced by
``_parse_hierarchy_xml_to_elements``), so every consumer downstream
(``filter_ui_hierarchy``, the Explorer index, the MCP actuators that compare
``clickable == "true"``) sees identical shapes whichever backend is active.

One observation is one request: ``/snapshot?fields=xml`` returns the XML and,
on Android 11+, the screenshot taken at the same instant with the bitmap's own
dimensions. On older Android the same answer carries the XML and the client
only adds an adb screencap; the hierarchy is never dumped twice.
"""

from __future__ import annotations

from io import BytesIO
import json
import subprocess
from typing import Any
import urllib.error
import urllib.request

from PIL import Image

from artemis.clients.ui_automator_client import (
    UIAutomatorScreenData,
    _parse_hierarchy_xml_to_elements,
    _pil_to_base64,
)
from artemis.runtime.adb_endpoint import adb_command
from artemis.runtime.awake_service import ensure_device_awake
from artemis.runtime.helper_manager import (
    DEVICE_PORT,
    PACKAGE_NAME,
    SERVICE_NAME,
    AccessibilityHelperManager,
    HelperSession,
    HelperUnavailable,
    ProvisionEvent,
    helper_manager,
)
from artemis.utils.logger import get_logger

logger = get_logger(__name__)

# Kept for callers that imported these from here.
DEFAULT_PORT = DEVICE_PORT
TOKEN_HEADER = "X-Artemis-Token"
__all__ = [
    "AccessibilityClient",
    "DEFAULT_PORT",
    "HelperEmptyHierarchy",
    "HelperRequestError",
    "HelperUnavailable",
    "PACKAGE_NAME",
    "SERVICE_NAME",
    "TOKEN_HEADER",
    "normalize_helper_elements",
]

_BOOL_ATTRIBUTES = (
    "checkable",
    "checked",
    "clickable",
    "enabled",
    "focusable",
    "focused",
    "scrollable",
    "long-clickable",
    "password",
    "selected",
    "visible-to-user",
)
_GLOBAL_KEYS = ("back", "home", "recents", "notifications", "quick_settings")


class HelperEmptyHierarchy(RuntimeError):
    """The helper answered but found no window to dump.

    Raised instead of returning a screen with zero elements, so ``auto`` mode
    can try UIAutomator2 and ``helper`` mode reports the real cause instead of
    showing the agent an empty screen.
    """


class HelperRequestError(RuntimeError):
    """The helper answered with an HTTP error that a new tunnel cannot fix."""

    def __init__(self, status: int, path: str, body: str):
        self.status = status
        self.path = path
        self.body = body
        super().__init__(f"accessibility helper {path} answered HTTP {status}: {body[:200]}")


def normalize_helper_elements(elements: Any) -> list[dict[str, Any]]:
    """Coerce the helper's JSON element list into the UIAutomator element shape.

    Used only when a dump carries elements but no XML. The JSON encodes flags
    as booleans and omits ``accessibilityText``; the XML parser emits string
    flags and mirrors ``content-desc`` into ``accessibilityText``, and several
    consumers compare against the string ``"true"``.
    """
    if isinstance(elements, dict):
        elements = [elements]
    if not isinstance(elements, list):
        return []
    normalized: list[dict[str, Any]] = []
    for raw in elements:
        if not isinstance(raw, dict):
            continue
        element = dict(raw)
        for key in _BOOL_ATTRIBUTES:
            if key in element and isinstance(element[key], bool):
                element[key] = "true" if element[key] else "false"
        desc = element.get("content-desc")
        if desc is not None and "accessibilityText" not in element:
            element["accessibilityText"] = desc
        bounds = element.get("parsed_bounds")
        if isinstance(bounds, dict):
            element["parsed_bounds"] = {
                side: int(bounds.get(side, 0)) for side in ("left", "top", "right", "bottom")
            }
        element.pop("children", None)
        normalized.append(element)
    return normalized


class AccessibilityClient:
    """Screen-data client over the helper's loopback HTTP API."""

    backend_name = "helper"

    def __init__(
        self,
        device_id: str,
        manager: AccessibilityHelperManager | None = None,
        *,
        provision_on_connect: bool = True,
        request_timeout: float = 6.0,
    ) -> None:
        self._device_id = device_id
        self._manager = manager or helper_manager
        self._provision_on_connect = provision_on_connect
        self._request_timeout = request_timeout
        self._session: HelperSession | None = None
        self._awake_strategy: str | None = None

    # ------------------------------------------------------------------ #
    # Session lifecycle
    # ------------------------------------------------------------------ #

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def session(self) -> HelperSession | None:
        return self._session

    @property
    def active_backend(self) -> str | None:
        return self.backend_name if self._session is not None else None

    def connect(self, on_event: ProvisionEvent | None = None) -> None:
        """Task-path entry: provision (install / upgrade / enable) and attach.

        ``on_event`` receives ``installing`` / ``upgrading`` before the slow step.
        """
        self._session = self._manager.attach(
            self._device_id, provision=self._provision_on_connect, on_event=on_event
        )
        if self._awake_strategy is None:
            self._awake_strategy = ensure_device_awake(self._device_id)

    def _ensure_session(self) -> HelperSession:
        """Observer-path entry: attach to a helper that is already running."""
        if self._session is None:
            self._session = self._manager.attach(self._device_id, provision=False)
            if self._awake_strategy is None:
                self._awake_strategy = ensure_device_awake(self._device_id)
        return self._session

    def disconnect(self) -> None:
        self._manager.detach(self._device_id)
        self._session = None
        self._awake_strategy = None

    def ping(self) -> bool:
        try:
            session = self._ensure_session()
        except HelperUnavailable:
            return False
        return self._manager.ping(session.local_port) is not None

    # ------------------------------------------------------------------ #
    # Transport
    # ------------------------------------------------------------------ #

    def _http(
        self, path: str, payload: dict[str, Any] | None = None, timeout: float | None = None
    ) -> bytes:
        """One request with two single-shot repairs.

        * HTTP 401: the helper lost its token (re-bound service, reboot): push it
          again and retry once.
        * Transport failure on a read: rebuild the tunnel once and retry.
          Actions are not replayed because they may already have executed.

        Any other HTTP status is a :class:`HelperRequestError`; a new tunnel
        would not change the answer.
        """
        session = self._ensure_session()
        try:
            return self._http_once(session, path, payload, timeout)
        except urllib.error.HTTPError as exc:
            body = _read_error_body(exc)
            if exc.code != 401:
                raise HelperRequestError(exc.code, path, body) from exc
            logger.warning(
                f"Accessibility helper on {self._device_id} rejected the session token "
                f"({body[:120]}); pushing it again."
            )
            self._manager.push_token(self._device_id)
            try:
                return self._http_once(session, path, payload, timeout)
            except urllib.error.HTTPError as again:
                raise HelperRequestError(again.code, path, _read_error_body(again)) from again
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            if payload is not None:
                raise
            logger.warning(
                f"Accessibility helper request {path} on {self._device_id} failed ({exc}); "
                "rebuilding the tunnel once."
            )
            self._session = self._manager.reattach(self._device_id)
            try:
                return self._http_once(self._session, path, payload, timeout)
            except urllib.error.HTTPError as again:
                raise HelperRequestError(again.code, path, _read_error_body(again)) from again

    def _http_once(
        self,
        session: HelperSession,
        path: str,
        payload: dict[str, Any] | None,
        timeout: float | None,
    ) -> bytes:
        data = (
            json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        )
        headers: dict[str, str] = {}
        token = session.token or self._manager.host_token()
        if token:
            headers[TOKEN_HEADER] = token
        if data is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(f"{session.base_url}{path}", data=data, headers=headers)
        with urllib.request.urlopen(request, timeout=timeout or self._request_timeout) as resp:
            return resp.read()

    def _rpc(self, cmd: str, params: dict[str, Any] | None = None) -> bool:
        payload = {"cmd": cmd, **(params or {})}
        try:
            data = json.loads(self._http("/action", payload).decode("utf-8"))
        except (
            HelperUnavailable,
            HelperRequestError,
            urllib.error.URLError,
            OSError,
            ValueError,
            TimeoutError,
        ) as exc:
            logger.warning(f"Accessibility helper command '{cmd}' failed: {exc}")
            return False
        return bool(data.get("success", False))

    # ------------------------------------------------------------------ #
    # Screen data
    # ------------------------------------------------------------------ #

    def get_hierarchy(self) -> str:
        """Raw UIAutomator-format XML, the same contract as ``UIAutomatorClient``."""
        return self._http("/dump_xml").decode("utf-8")

    def get_hierarchy_json(self, *, include_invisible: bool = False) -> dict[str, Any]:
        """The helper's JSON dump (XML plus its own element list); diagnostics only."""
        query = "fields=xml,elements" + ("&include_invisible=1" if include_invisible else "")
        return json.loads(self._http(f"/dump?{query}").decode("utf-8"))

    def _snapshot(self) -> dict[str, Any]:
        """``/snapshot?fields=xml``: hierarchy plus, on Android 11+, the screenshot."""
        data = json.loads(self._http("/snapshot?fields=xml", timeout=8.0).decode("utf-8"))
        if not isinstance(data, dict):
            raise HelperRequestError(200, "/snapshot", "non-object answer")
        if not data.get("success"):
            raise HelperEmptyHierarchy(
                f"accessibility helper on {self._device_id} returned no hierarchy: "
                f"{data.get('error') or 'unknown reason'}"
            )
        return data

    def get_atomic_snapshot(self) -> dict[str, Any] | None:
        """Screenshot + hierarchy captured in one call on the device (Android 11+), else None."""
        data = self._snapshot()
        if data.get("has_screenshot") and data.get("screenshot_base64"):
            return data
        return None

    def get_screenshot(self) -> Image.Image | None:
        try:
            result = subprocess.run(
                adb_command(["-s", self._device_id, "exec-out", "screencap", "-p"]),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
                check=True,
            )
            return Image.open(BytesIO(result.stdout))
        except (OSError, subprocess.SubprocessError) as exc:
            logger.error(f"Failed to capture screenshot via adb: {exc}")
            return None

    def get_screenshot_base64(self) -> str | None:
        screenshot = self.get_screenshot()
        return None if screenshot is None else _pil_to_base64(screenshot, format="JPEG")

    @staticmethod
    def _elements_from_dump(dump: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        xml = dump.get("xml") or ""
        if isinstance(xml, str) and xml.strip():
            return xml, _parse_hierarchy_xml_to_elements(xml)
        return "", normalize_helper_elements(dump.get("elements"))

    def get_screen_data(self) -> UIAutomatorScreenData:
        snapshot = self._snapshot()
        xml, elements = self._elements_from_dump(snapshot)
        if snapshot.get("has_screenshot") and snapshot.get("screenshot_base64"):
            width, height = _dimensions(snapshot)
            return UIAutomatorScreenData(
                base64=str(snapshot["screenshot_base64"]),
                hierarchy_xml=xml,
                elements=elements,
                width=width,
                height=height,
            )

        # Android 10 and below (or a rate-limited capture that did not recover):
        # keep the hierarchy we already have and only add an adb screencap.
        reason = snapshot.get("screenshot_error")
        if reason:
            logger.debug(f"Helper snapshot without screenshot ({reason}); using adb screencap")
        screenshot = self.get_screenshot()
        if screenshot is None:
            raise RuntimeError("Failed to capture screenshot")
        return UIAutomatorScreenData(
            base64=_pil_to_base64(screenshot, format="JPEG"),
            hierarchy_xml=xml,
            elements=elements,
            width=screenshot.width,
            height=screenshot.height,
        )

    # ------------------------------------------------------------------ #
    # Input
    # ------------------------------------------------------------------ #

    def set_clipboard(self, text: str) -> bool:
        """Put ``text`` on the device clipboard so the driver can paste it."""
        return self._rpc("clipboard", {"text": text})

    def send_text(self, text: str) -> bool:
        """Append ``text`` to the focused field, the way typing through an IME does."""
        return self._rpc("type", {"text": text, "append": True})

    def clear_text(self) -> bool:
        return self._rpc("clear", {})

    def press_key(self, key: str) -> bool:
        key_lower = str(key).lower()
        if key_lower in _GLOBAL_KEYS:
            return self._rpc("global", {"action": key_lower})
        result = subprocess.run(
            adb_command(
                [
                    "-s",
                    self._device_id,
                    "shell",
                    "input",
                    "keyevent",
                    f"KEYCODE_{key_lower.upper()}",
                ]
            ),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return result.returncode == 0

    def tap(self, x: float, y: float) -> bool:
        return self._rpc("tap", {"x": x, "y": y})

    def swipe(self, x1: float, y1: float, x2: float, y2: float, duration_ms: int = 300) -> bool:
        return self._rpc("swipe", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "duration": duration_ms})


def _read_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", "replace")
    except OSError:
        return str(exc)


def _dimensions(snapshot: dict[str, Any]) -> tuple[int, int]:
    """The screenshot's own size; a snapshot without it is a broken answer, not a 1080x2400 one."""
    try:
        width = int(snapshot.get("width") or 0)
        height = int(snapshot.get("height") or 0)
    except (TypeError, ValueError):
        width = height = 0
    if width <= 0 or height <= 0:
        raise HelperRequestError(
            200, "/snapshot", f"screenshot without dimensions (width={width}, height={height})"
        )
    return width, height
