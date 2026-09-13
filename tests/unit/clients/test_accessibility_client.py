"""AccessibilityClient: HTTP surface, token handling, element-shape parity, repairs."""

from __future__ import annotations

import base64
from io import BytesIO
import json
import urllib.error
from unittest.mock import MagicMock, patch

from PIL import Image
import pytest

from artemis.clients.accessibility_client import (
    TOKEN_HEADER,
    AccessibilityClient,
    HelperEmptyHierarchy,
    HelperRequestError,
    HelperUnavailable,
    normalize_helper_elements,
)
from artemis.clients.ui_automator_client import _parse_hierarchy_xml_to_elements
from artemis.runtime.helper_manager import HelperSession

XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<hierarchy rotation="0">'
    '<node index="0" text="OK" resource-id="app:id/ok" class="android.widget.Button" '
    'package="app" content-desc="Confirm" checkable="false" checked="false" clickable="true" '
    'enabled="true" focusable="true" focused="false" scrollable="false" long-clickable="false" '
    'password="false" selected="false" visible-to-user="true" bounds="[10,20][110,70]" '
    'state-description="on" />'
    "</hierarchy>"
)
TOKEN = "a" * 48


class FakeManager:
    def __init__(self):
        self.session = self._session(41000)
        self.attach_calls: list[bool] = []
        self.reattach_calls = 0
        self.pushed_tokens = 0
        self.detached = False
        self.fail_reattach = False

    @staticmethod
    def _session(port: int) -> HelperSession:
        return HelperSession(
            serial="dev",
            local_port=port,
            transport_id="3",
            version_code=6,
            version_name="1.2.0",
            owns_forward=True,
            token=TOKEN,
            protocol_version=2,
        )

    def attach(self, serial, *, provision=True, on_event=None):
        self.attach_calls.append(provision)
        if on_event is not None:
            on_event("installing", {"serial": serial})
        return self.session

    def reattach(self, serial):
        self.reattach_calls += 1
        if self.fail_reattach:
            raise HelperUnavailable("dead")
        self.session = self._session(41001)
        return self.session

    def push_token(self, serial):
        self.pushed_tokens += 1
        return True

    def host_token(self):
        return TOKEN

    def detach(self, serial):
        self.detached = True

    def ping(self, port):
        return {"success": True}


def _response(body: bytes):
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def _http_error(code: int, body: bytes = b'{"success": false, "error": "no"}'):
    return urllib.error.HTTPError("http://x", code, "err", {}, BytesIO(body))


def _urlopen():
    return patch("artemis.clients.accessibility_client.urllib.request.urlopen")


@pytest.fixture
def client():
    manager = FakeManager()
    with patch("artemis.clients.accessibility_client.ensure_device_awake", return_value=None):
        yield AccessibilityClient("dev", manager=manager), manager


# --------------------------------------------------------------------------- #
# Session and transport
# --------------------------------------------------------------------------- #


def test_connect_provisions_and_lazy_path_does_not(client):
    c, manager = client
    events = []
    c.connect(on_event=lambda name, details: events.append(name))
    assert manager.attach_calls == [True]
    assert events == ["installing"]
    assert c.active_backend == "helper"

    lazy = AccessibilityClient("dev", manager=FakeManager())
    with (
        patch("artemis.clients.accessibility_client.ensure_device_awake", return_value=None),
        _urlopen() as urlopen,
    ):
        urlopen.return_value = _response(XML.encode())
        assert lazy.get_hierarchy() == XML
    assert lazy._manager.attach_calls == [False]


def test_every_request_carries_the_session_token(client):
    c, _ = client
    with _urlopen() as urlopen:
        urlopen.return_value = _response(XML.encode())
        assert c.get_hierarchy() == XML
    request = urlopen.call_args.args[0]
    assert request.full_url == "http://127.0.0.1:41000/dump_xml"
    # urllib stores header names capitalized: "X-artemis-token".
    assert request.get_header(TOKEN_HEADER.capitalize()) == TOKEN


def test_401_pushes_the_token_again_and_retries_once(client):
    c, manager = client
    with _urlopen() as urlopen:
        urlopen.side_effect = [_http_error(401), _response(XML.encode())]
        assert c.get_hierarchy() == XML
    assert manager.pushed_tokens == 1
    assert manager.reattach_calls == 0  # a tunnel rebuild would not fix a token problem

    with _urlopen() as urlopen:
        urlopen.side_effect = [_http_error(401), _http_error(401)]
        with pytest.raises(HelperRequestError) as info:
            c.get_hierarchy()
    assert info.value.status == 401
    assert manager.pushed_tokens == 2


def test_other_http_errors_surface_without_repairs(client):
    c, manager = client
    with _urlopen() as urlopen:
        urlopen.side_effect = _http_error(404, b"Unknown endpoint")
        with pytest.raises(HelperRequestError, match="HTTP 404"):
            c.get_hierarchy()
    assert manager.reattach_calls == 0 and manager.pushed_tokens == 0


def test_transport_failure_reattaches_once_and_retries(client):
    c, manager = client
    with _urlopen() as urlopen:
        urlopen.side_effect = [urllib.error.URLError("connection refused"), _response(XML.encode())]
        assert c.get_hierarchy() == XML
    assert manager.reattach_calls == 1
    assert urlopen.call_args.args[0].full_url == "http://127.0.0.1:41001/dump_xml"


def test_transport_failure_after_reattach_propagates(client):
    c, manager = client
    manager.fail_reattach = True
    with _urlopen() as urlopen:
        urlopen.side_effect = urllib.error.URLError("connection refused")
        with pytest.raises(HelperUnavailable):
            c.get_hierarchy()
    assert manager.reattach_calls == 1


def test_action_timeout_does_not_repeat_text_entry(client):
    c, manager = client
    with _urlopen() as urlopen:
        urlopen.side_effect = TimeoutError("response lost after action")
        assert c.send_text("hello") is False
    assert urlopen.call_count == 1
    assert manager.reattach_calls == 0


# --------------------------------------------------------------------------- #
# Screen data: one /snapshot request, UIAutomator element shapes
# --------------------------------------------------------------------------- #


def test_screen_data_is_one_snapshot_request_with_xml_only(client):
    c, _ = client
    snapshot = {
        "success": True,
        "has_screenshot": True,
        "screenshot_base64": "QUJD",
        "xml": XML,
        "width": 1080,
        "height": 2424,
    }
    with _urlopen() as urlopen:
        urlopen.return_value = _response(json.dumps(snapshot).encode())
        data = c.get_screen_data()
    assert urlopen.call_count == 1
    assert urlopen.call_args.args[0].full_url == "http://127.0.0.1:41000/snapshot?fields=xml"

    assert data.base64 == "QUJD" and data.width == 1080 and data.height == 2424
    assert data.hierarchy_xml == XML
    assert data.elements == _parse_hierarchy_xml_to_elements(XML)
    element = data.elements[1]
    assert element["clickable"] == "true"
    assert element["visible-to-user"] == "true"
    assert element["accessibilityText"] == "Confirm"
    assert element["parsed_bounds"] == {"left": 10, "top": 20, "right": 110, "bottom": 70}
    assert element["state-description"] == "on"


def test_screen_data_without_screenshot_reuses_the_hierarchy_and_adds_screencap(client):
    c, _ = client
    no_shot = {
        "success": True,
        "has_screenshot": False,
        "screenshot_error": "takeScreenshot not supported on Android < 11",
        "xml": XML,
        "width": 1080,
        "height": 2400,
    }
    img = Image.new("RGB", (4, 6), "white")
    with (
        _urlopen() as urlopen,
        patch.object(AccessibilityClient, "get_screenshot", return_value=img),
    ):
        urlopen.return_value = _response(json.dumps(no_shot).encode())
        data = c.get_screen_data()
    assert urlopen.call_count == 1  # the hierarchy is not dumped a second time
    assert data.width == 4 and data.height == 6
    assert Image.open(BytesIO(base64.b64decode(data.base64))).size == (4, 6)
    assert data.elements == _parse_hierarchy_xml_to_elements(XML)


def test_screen_data_falls_back_to_normalized_json_without_xml(client):
    c, _ = client
    snapshot = {
        "success": True,
        "has_screenshot": True,
        "screenshot_base64": "QUJD",
        "xml": "",
        "elements": [
            {
                "text": "",
                "content-desc": "Menu",
                "clickable": True,
                "focused": False,
                "visible-to-user": True,
                "parsed_bounds": {"left": 1.0, "top": 2, "right": 3, "bottom": 4},
                "children": [{"text": "x"}],
            }
        ],
        "width": 1080,
        "height": 2400,
    }
    with _urlopen() as urlopen:
        urlopen.return_value = _response(json.dumps(snapshot).encode())
        data = c.get_screen_data()
    assert data.elements == [
        {
            "text": "",
            "content-desc": "Menu",
            "accessibilityText": "Menu",
            "clickable": "true",
            "focused": "false",
            "visible-to-user": "true",
            "parsed_bounds": {"left": 1, "top": 2, "right": 3, "bottom": 4},
        }
    ]


def test_empty_hierarchy_is_an_error_not_a_blank_screen(client):
    c, _ = client
    answer = {"success": False, "error": "No active window or root node found", "xml": ""}
    with _urlopen() as urlopen:
        urlopen.return_value = _response(json.dumps(answer).encode())
        with pytest.raises(HelperEmptyHierarchy, match="No active window"):
            c.get_screen_data()


def test_screenshot_without_dimensions_is_rejected(client):
    c, _ = client
    answer = {"success": True, "has_screenshot": True, "screenshot_base64": "QUJD", "xml": XML}
    with _urlopen() as urlopen:
        urlopen.return_value = _response(json.dumps(answer).encode())
        with pytest.raises(HelperRequestError, match="without dimensions"):
            c.get_screen_data()


# --------------------------------------------------------------------------- #
# Input
# --------------------------------------------------------------------------- #


def test_set_clipboard_sends_raw_utf8(client):
    c, _ = client
    with _urlopen() as urlopen:
        urlopen.return_value = _response(b'{"success": true}')
        assert c.set_clipboard("héllo\n你好") is True
    request = urlopen.call_args.args[0]
    assert request.full_url == "http://127.0.0.1:41000/action"
    assert request.data == '{"cmd": "clipboard", "text": "héllo\\n你好"}'.encode()
    assert request.get_header("Content-type") == "application/json; charset=utf-8"


def test_send_text_appends_like_an_ime(client):
    c, _ = client
    with _urlopen() as urlopen:
        urlopen.return_value = _response(b'{"success": true}')
        assert c.send_text("abc") is True
    assert json.loads(urlopen.call_args.args[0].data) == {
        "cmd": "type",
        "text": "abc",
        "append": True,
    }


def test_rpc_failure_returns_false_instead_of_raising(client):
    c, manager = client
    manager.fail_reattach = True
    with _urlopen() as urlopen:
        urlopen.side_effect = urllib.error.URLError("refused")
        assert c.clear_text() is False
    with _urlopen() as urlopen:
        urlopen.side_effect = _http_error(500)
        assert c.clear_text() is False


def test_press_key_uses_global_action_for_navigation_keys(client):
    c, _ = client
    with _urlopen() as urlopen:
        urlopen.return_value = _response(b'{"success": true}')
        assert c.press_key("BACK") is True
    assert json.loads(urlopen.call_args.args[0].data) == {"cmd": "global", "action": "back"}


def test_disconnect_detaches(client):
    c, manager = client
    c.connect()
    c.disconnect()
    assert manager.detached and c.active_backend is None


def test_normalize_helper_elements_tolerates_bad_input():
    assert normalize_helper_elements(None) == []
    assert normalize_helper_elements("x") == []
    assert normalize_helper_elements({"clickable": True}) == [{"clickable": "true"}]
