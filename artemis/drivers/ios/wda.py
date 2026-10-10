# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""WebDriverAgent HTTP client for physical iOS UI automation.

Xcode's ``DeviceInteraction*`` MCP tools accept simulators only, so physical
devices are driven through WebDriverAgent — the same XCUITest bridge Appium
uses. The client speaks plain HTTP to the WDA server running on the device
and has no third-party dependencies; every call is offloaded to a thread so
the driver stays fully async.

Reaching the device-side server needs one of:

- ``ARTEMIS_IOS_WDA_URL``: an explicit endpoint such as
  ``http://127.0.0.1:8100`` for ``iproxy``/``pymobiledevice3`` forwards or
  ``http://<device-ip>:8100`` when the phone shares the LAN.
- The CoreDevice tunnel address from ``devicectl device info details`` —
  paired devices already hold a managed IPv6 tunnel that routes TCP.
- ``ARTEMIS_IOS_WDA_HOST``: just a host/IP; port 8100 is assumed.
"""

import asyncio
import base64
import http.client
import ipaddress
import json
import os
from typing import Any
import urllib.error
import urllib.request

from artemis.drivers.ios.hierarchy import pixel_element
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

WDA_URL_ENV = "ARTEMIS_IOS_WDA_URL"
WDA_HOST_ENV = "ARTEMIS_IOS_WDA_HOST"
WDA_DEFAULT_PORT = 8100
WDA_REQUEST_TIMEOUT = 30.0

#: Server-side session invalidation markers — WDA answers these once the
#: session's target app died, so the request provably did not execute and is
#: safe to retry after rebinding.
_SESSION_LOST_MARKERS = (
    "invalid session id",
    "no such session",
    "stale session",
    "session does not exist",
    "session id is not valid",
    # Anchor-app death — WDA reports "invalid element state: The application
    # under test with bundle id ... is not running, possibly crashed" rather
    # than a session error. Match the message body, not the generic error
    # code, which also covers legitimately unhittable elements.
    "application under test",
    "possibly crashed",
    "is not running",
)


class WdaUnavailableError(RuntimeError):
    """The WebDriverAgent server could not be reached or did not respond."""


class WdaClient:
    """Minimal WebDriverAgent client covering Artemis's interaction surface."""

    def __init__(self, base_url: str, timeout: float = WDA_REQUEST_TIMEOUT):
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._session_id: str | None = None
        self._reopen_lock = asyncio.Lock()

    @property
    def base_url(self) -> str:
        return self._base

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def connected(self) -> bool:
        return self._session_id is not None

    def _sync_request(
        self, method: str, path: str, payload: dict[str, Any] | None, timeout: float
    ) -> Any:
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"{self._base}{path}",
            data=body,
            method=method,
            headers={"Content-Type": "application/json"} if body else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")[:500]
            raise RuntimeError(f"WebDriverAgent {method} {path} failed: HTTP {error.code} {detail}")
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as error:
            # HTTPException covers garbage services answering probed ports —
            # BadStatusLine is not an OSError, so it needs mapping here.
            raise WdaUnavailableError(
                f"WebDriverAgent at {self._base} is unreachable: {error}. "
                "Check the device connection, the WDA runner process, and any "
                "port forwarding (iproxy or 'pymobiledevice3 remote')."
            )
        try:
            payload_out = json.loads(raw)
        except ValueError:
            return raw
        if isinstance(payload_out, dict) and "value" in payload_out:
            value = payload_out["value"]
            if isinstance(value, dict) and value.get("error"):
                # Keep both fields in the message — recovery matching keys off
                # the error code ("invalid session id") while humans need the
                # readable message.
                message = value.get("message") or ""
                error_code = value["error"]
                detail = f"{error_code}: {message}" if message else str(error_code)
                raise RuntimeError(f"WebDriverAgent {method} {path} failed: {detail}")
            # WDA reports the live session in the outer /status envelope, not
            # inside value — surface it so a foreign session is detectable.
            if (
                path == "/status"
                and isinstance(value, dict)
                and isinstance(payload_out.get("sessionId"), str)
                and payload_out["sessionId"]
            ):
                value["sessionId"] = payload_out["sessionId"]
            return value
        return payload_out

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
        _recovered: bool = False,
    ) -> Any:
        request_task = asyncio.create_task(
            asyncio.to_thread(self._sync_request, method, path, payload, timeout or self._timeout)
        )
        try:
            return await asyncio.shield(request_task)
        except asyncio.CancelledError:
            # The blocking urllib call keeps running on its thread; drain it
            # before propagating so a cancelled input cannot still land on the
            # device after the caller (and its device lease) moved on.
            try:
                await request_task
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError, RuntimeError, TimeoutError) as drain_error:
                logger.debug(f"WDA request drain failed while cancelling: {drain_error}")
            raise
        except RuntimeError as error:
            if (
                _recovered
                or self._is_session_admin(method, path)
                or not self._is_session_loss(error)
            ):
                raise
            # The dead session never ran the command, so any method is safe
            # to replay after rebinding.
            path = await self._reopen_session_with_path(path)
            return await self._request(method, path, payload, timeout, _recovered=True)
        except WdaUnavailableError:
            if _recovered or method != "GET":
                raise
            # A timed-out write may have executed device-side, so only reads
            # recover transparently; writes surface the error and the next
            # observation rebinds if the session really is gone.
            path = await self._reopen_session_with_path(path, suppress_errors=True)
            return await self._request(method, path, payload, timeout, _recovered=True)

    async def _reopen_session_with_path(
        self, path: str, suppress_errors: bool = False
    ) -> str:
        """Rebind the session and rewrite the old session id inside ``path``."""
        old_id = self._session_id
        await self._reopen_session(suppress_errors=suppress_errors)
        if old_id and self._session_id and old_id != self._session_id:
            path = path.replace(old_id, self._session_id)
        return path

    def _is_session_loss(self, error: RuntimeError) -> bool:
        message = str(error).casefold()
        return any(marker in message for marker in _SESSION_LOST_MARKERS)

    @staticmethod
    def _is_session_admin(method: str, path: str) -> bool:
        # Session create/delete must not trigger recovery: creating during
        # recovery recurses, and deleting a dead session should fail quietly.
        return (method == "POST" and path == "/session") or (
            method == "DELETE" and path.startswith("/session/")
        )

    async def _reopen_session(self, suppress_errors: bool = False) -> None:
        """Rebind a dead WDA session, anchored to the current foreground app.

        Binding to the foreground bundle keeps the device's visible app
        unchanged (a Preferences anchor would pull Settings to the front
        mid-task); SpringBoard is not activatable as a session target, so a
        home-screen foreground falls back to Preferences.
        """
        async with self._reopen_lock:
            anchor = await self._foreground_bundle()
            if not anchor or anchor == "com.apple.springboard":
                anchor = "com.apple.Preferences"
            # Drop the zombie first so the preflight does not see it as a
            # foreign session and refuse the replacement.
            old_id = self._session_id
            if old_id:
                self._session_id = None
                try:
                    await asyncio.to_thread(
                        self._sync_request, "DELETE", f"/session/{old_id}", None, 5.0
                    )
                except (RuntimeError, WdaUnavailableError, OSError):
                    pass
            try:
                self._session_id = await self._create_owned_session(
                    adopt_existing=False, bundle_id=anchor
                )
                logger.info(
                    f"WebDriverAgent session rebound to {self._session_id} "
                    f"(anchor {anchor}) on {self._base}"
                )
            except Exception as error:
                self._session_id = None
                if not suppress_errors:
                    raise WdaUnavailableError(
                        f"WebDriverAgent session recovery failed on {self._base}: {error}"
                    ) from error
                logger.debug(f"WDA session rebound attempt failed: {error}")

    async def _foreground_bundle(self) -> str | None:
        """Session-free probe of the device's foreground app bundle id."""
        try:
            value = await asyncio.to_thread(
                self._sync_request, "GET", "/wda/activeAppInfo", None, 5.0
            )
        except (RuntimeError, WdaUnavailableError, OSError):
            return None
        if isinstance(value, dict):
            bundle = value.get("bundleId") or value.get("bundleIdentifier")
            return bundle if isinstance(bundle, str) and bundle else None
        return None

    # --- Session lifecycle ---

    async def status(self, timeout: float = 5.0) -> dict[str, Any] | None:
        try:
            value = await self._request("GET", "/status", timeout=timeout)
        except (RuntimeError, WdaUnavailableError, OSError) as error:
            logger.debug(f"WDA status probe failed for {self._base}: {error}")
            return None
        # Reject non-JSON/foreign bodies instead of wrapping them: a proxy or
        # unrelated service answering on this port is not WebDriverAgent.
        return value if isinstance(value, dict) else None

    async def device_info(self, timeout: float = 10.0) -> dict[str, Any]:
        """GET /wda/device/info — available without a session.

        ``uuid`` here is ``identifierForVendor``, not the device UDID, so
        identity checks must rely on ``name``/``isSimulator`` instead.
        """
        value = await self._request("GET", "/wda/device/info", timeout=timeout)
        if not isinstance(value, dict):
            raise RuntimeError(
                f"WebDriverAgent /wda/device/info returned a non-JSON response: {value!r:.300}"
            )
        return value

    async def _create_owned_session(
        self, adopt_existing: bool, bundle_id: str = "com.apple.Preferences"
    ) -> str:
        """Status preflight, POST /session, and ID assignment as one unit."""
        if self._session_id:
            return self._session_id
        status = await self.status()
        if status is None:
            raise WdaUnavailableError(f"WebDriverAgent at {self._base} did not answer /status.")
        active = status.get("sessionId")
        if isinstance(active, str) and active:
            if adopt_existing:
                # The driver launched this runner itself, so its auto-created
                # session is ours to reuse — not a foreign client to protect.
                self._session_id = active
                return active
            raise RuntimeError(
                "Refusing to replace the active WebDriverAgent session "
                f"({active}), which this client does not own — POST /session "
                "would kill it. Point ARTEMIS_IOS_WDA_URL at a dedicated WDA "
                "server or close the existing session first."
            )
        payload = {
            "capabilities": {
                # A bare session binds to an ephemeral pid.0 application that
                # dies instantly ("stale element reference" on first command).
                # com.apple.springboard cannot be activated as an app target;
                # the anchor must be an installed, activatable app so the
                # session binds to a real process.
                "alwaysMatch": {
                    "platformName": "iOS",
                    "bundleId": bundle_id,
                },
                "firstMatch": [{}],
            }
        }
        value = await self._request("POST", "/session", payload, timeout=60.0)
        session_id = None
        if isinstance(value, dict):
            session_id = value.get("sessionId")
            if session_id is None and isinstance(value.get("capabilities"), dict):
                session_id = value.get("capabilities", {}).get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise RuntimeError(
                f"WebDriverAgent did not return a session id (response: {value!r:.300})."
            )
        self._session_id = session_id
        return session_id

    async def open_session(self, adopt_existing: bool = False) -> str:
        """Create a WDA session, refusing to take over a foreign one.

        ``adopt_existing=True`` reuses the active session reported by
        ``/status``; only safe when the caller owns the WDA runner process
        (auto-launched runners create a session on startup). Foreign or
        user-provisioned endpoints must keep the default refusal.

        Cancellation-safe: when the caller is cancelled while creation is in
        flight, the request is drained and any session it produced is closed
        before the CancelledError propagates.
        """
        create_task = asyncio.ensure_future(self._create_owned_session(adopt_existing))
        try:
            return await asyncio.shield(create_task)
        except asyncio.CancelledError:
            session_id = None
            try:
                session_id = await create_task
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError, RuntimeError, TimeoutError) as drain_error:
                logger.debug(f"WDA session-create drain failed while cancelling: {drain_error}")
            if session_id:
                try:
                    await self.close_session()
                except (OSError, ValueError, RuntimeError, TimeoutError) as close_error:
                    # Best-effort teardown of the orphaned session.
                    logger.debug(
                        f"WDA session close after cancelled open_session failed: {close_error}"
                    )
            raise

    async def close_session(self) -> None:
        session_id, self._session_id = self._session_id, None
        if session_id is None:
            return
        try:
            await self._request("DELETE", f"/session/{session_id}", timeout=10.0)
        except (RuntimeError, WdaUnavailableError, OSError) as error:
            logger.debug(f"WDA session delete failed: {error}")

    # --- Observation ---

    async def screenshot_png(self) -> bytes:
        value = await self._request("GET", "/screenshot")
        if not isinstance(value, str) or not value:
            raise RuntimeError("WebDriverAgent returned no screenshot data.")
        return base64.b64decode(value)

    async def source_json(self) -> dict[str, Any]:
        value = await self._request("GET", "/source?format=json")
        if not isinstance(value, dict):
            raise RuntimeError("WebDriverAgent returned a non-JSON hierarchy.")
        return value

    async def window_size(self) -> tuple[float, float]:
        path = f"/session/{self._session_id}/window/size" if self._session_id else "/window/size"
        value = await self._request("GET", path)
        if isinstance(value, dict) and "width" in value and "height" in value:
            return float(value["width"]), float(value["height"])
        raise RuntimeError(f"WebDriverAgent returned an unexpected window size: {value!r}")

    async def active_app(self) -> str | None:
        path = "/wda/activeAppInfo"
        if self._session_id:
            path = f"/session/{self._session_id}/wda/activeAppInfo"
        try:
            value = await self._request("GET", path)
        except (RuntimeError, WdaUnavailableError):
            return None
        if isinstance(value, dict):
            bundle = value.get("bundleId") or value.get("bundleIdentifier")
            return bundle if isinstance(bundle, str) and bundle else None
        return None

    # --- Input ---

    async def _actions(self, pointer_actions: list[dict[str, Any]]) -> None:
        session = self._require_session()
        payload = {
            "actions": [
                {
                    "type": "pointer",
                    "id": "artemis-finger",
                    "parameters": {"pointerType": "touch"},
                    "actions": pointer_actions,
                }
            ]
        }
        await self._request("POST", f"/session/{session}/actions", payload)

    def _require_session(self) -> str:
        if not self._session_id:
            raise RuntimeError("WebDriverAgent session is not open.")
        return self._session_id

    async def tap(self, x: float, y: float, hold_ms: int = 0) -> None:
        actions: list[dict[str, Any]] = [
            {"type": "pointerMove", "duration": 0, "x": x, "y": y},
            {"type": "pointerDown", "button": 0},
        ]
        if hold_ms > 0:
            actions.append({"type": "pause", "duration": hold_ms})
        actions.append({"type": "pointerUp", "button": 0})
        await self._actions(actions)

    async def swipe(self, sx: float, sy: float, ex: float, ey: float, duration_ms: int) -> None:
        await self._actions(
            [
                {"type": "pointerMove", "duration": 0, "x": sx, "y": sy},
                {"type": "pointerDown", "button": 0},
                {"type": "pause", "duration": 50},
                {"type": "pointerMove", "duration": duration_ms, "x": ex, "y": ey},
                {"type": "pointerUp", "button": 0},
            ]
        )

    async def type_text(self, text: str) -> None:
        session = self._require_session()
        # ``value`` is a list of Unicode code points — newlines and non-ASCII
        # text carry their literal values to the focused field.
        payload = {"value": list(text)}
        await self._request("POST", f"/session/{session}/wda/keys", payload)

    async def press_button(self, name: str) -> bool:
        """WDA hardware buttons: home, volumeUp, volumeDown, power."""
        session = self._require_session()
        try:
            await self._request("POST", f"/session/{session}/wda/pressButton", {"name": name})
            return True
        except WdaUnavailableError:
            # The transport died — propagate so callers don't misreport an
            # outage as an unsupported button.
            raise
        except RuntimeError:
            return False

    async def homescreen(self) -> None:
        await self._request("POST", "/wda/homescreen")

    async def lock(self) -> None:
        await self._request("POST", "/wda/lock")

    async def unlock(self) -> None:
        await self._request("POST", "/wda/unlock")


def normalize_wda_url(raw: str) -> str:
    """Accept bare hosts, IPv6 literals, or full URLs and return a base URL."""
    value = raw.strip()
    if not value:
        return value
    if "://" not in value:
        # Bare IPv6 literals need brackets once a port is attached. A bare
        # "v6:port" string is ambiguous — treat the last group as a port only
        # when the address part parses as a real IPv6 literal.
        if value.count(":") > 1 and not value.startswith("["):
            try:
                ipaddress.IPv6Address(value)
                value = f"[{value}]"
            except ValueError:
                address, _, port = value.rpartition(":")
                try:
                    ipaddress.IPv6Address(address)
                    value = f"[{address}]:{port}"
                except ValueError:
                    value = f"[{value}]"
        value = f"http://{value}"
    value = value.rstrip("/")
    scheme, _, remainder = value.partition("://")
    host_port, _, path = remainder.partition("/")
    # A port is present when ':' follows the host (or the IPv6 ']' bracket).
    has_port = (
        host_port.rsplit("]", 1)[-1].startswith(":") if "]" in host_port else ":" in host_port
    )
    if has_port:
        return value
    # The default port belongs to the authority, ahead of any path suffix.
    suffix = f"/{path}" if path else ""
    return f"{scheme}://{host_port}:{WDA_DEFAULT_PORT}{suffix}"


def wda_url_candidates(
    env_url: str | None = None,
    env_host: str | None = None,
    tunnel_ip: str | None = None,
) -> list[str]:
    """Ordered endpoints to probe for a running WebDriverAgent server."""
    candidates: list[str] = []
    for raw in (
        env_url if env_url is not None else os.environ.get(WDA_URL_ENV),
        env_host if env_host is not None else os.environ.get(WDA_HOST_ENV),
        (
            f"[{tunnel_ip}]:{WDA_DEFAULT_PORT}"
            if ":" in tunnel_ip
            else f"{tunnel_ip}:{WDA_DEFAULT_PORT}"
        )
        if tunnel_ip
        else None,
        f"127.0.0.1:{WDA_DEFAULT_PORT}",
    ):
        if not raw:
            continue
        url = normalize_wda_url(raw)
        if url and url not in candidates:
            candidates.append(url)
    return candidates


async def probe_wda(candidates: list[str], timeout: float = 5.0) -> WdaClient | None:
    """Return a client bound to the first endpoint that answers ``/status``."""
    for url in candidates:
        client = WdaClient(url)
        status = await client.status(timeout=timeout)
        if status is not None:
            return client
    return None


_ELEMENT_TYPE_PREFIX = "XCUIElementType"


def parse_wda_elements(
    node: dict[str, Any],
    scale: tuple[float, float],
    width: int,
    height: int,
) -> list[dict[str, Any]]:
    """Flatten a WDA ``/source?format=json`` tree into Artemis ui_elements.

    Mirrors ``parse_hierarchy``'s output: ``text``, ``resource_id``, ``class``,
    ``bounds``, ``parsed_bounds``, and ``hit_point`` in screenshot pixels.
    WDA's ``isVisible`` is advisory; elements with usable geometry are kept so
    downstream consumers see everything XCTest reports.
    """
    elements: list[dict[str, Any]] = []

    def visit(entry: dict[str, Any]) -> None:
        rect = entry.get("rect")
        if not isinstance(rect, dict):
            rect = {}
        try:
            x = float(rect.get("x") or 0.0)
            y = float(rect.get("y") or 0.0)
            w = float(rect.get("width") or 0.0)
            h = float(rect.get("height") or 0.0)
        except (TypeError, ValueError, AttributeError):
            x = y = w = h = 0.0
        if w > 0 and h > 0:
            left, top = round(x * scale[0]), round(y * scale[1])
            right, bottom = round((x + w) * scale[0]), round((y + h) * scale[1])
            if right > 0 and bottom > 0 and left < width and top < height:
                label = entry.get("label")
                value = entry.get("value")
                name = entry.get("name")
                text = ""
                if isinstance(label, str) and label:
                    text = label
                elif value is not None and str(value):
                    text = str(value)
                element_type = str(entry.get("type") or "")
                element = pixel_element(
                    text=text,
                    resource_id=name if isinstance(name, str) else "",
                    class_name=element_type.removeprefix(_ELEMENT_TYPE_PREFIX),
                    left=left,
                    top=top,
                    right=right,
                    bottom=bottom,
                )
                element["hit_point"] = [(left + right) // 2, (top + bottom) // 2]
                element["visible"] = bool(entry.get("isVisible", True))
                if value is not None and str(value) != text:
                    element["value"] = str(value)
                elements.append(element)
        children = entry.get("children")
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    visit(child)

    visit(node)
    return elements
