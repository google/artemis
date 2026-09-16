# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

from __future__ import annotations

import io
import json
import unittest
import urllib.error
from email.message import Message
from unittest.mock import patch

from artemis_client import AuthenticationError, JsonTransport, NetworkError, ProtocolError


class FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


class JsonTransportTests(unittest.TestCase):
    @patch("urllib.request.urlopen")
    def test_rejects_invalid_base_urls_before_network(self, urlopen) -> None:
        invalid_urls = (
            "file:///tmp/artemis",
            "http://",
            "http://:8000",
            "http://host.example?query",
            "http://host.example?",
            "http://host.example#fragment",
            "http://host.example#",
            "http://user:secret@host.example",
            "http://host.example:invalid-port",
            "http://host.example:65536",
            "http://[::1",
        )

        for base_url in invalid_urls:
            with self.subTest(base_url=base_url):
                with self.assertRaises(ValueError):
                    JsonTransport(base_url)

        urlopen.assert_not_called()

    @patch("urllib.request.urlopen")
    def test_preserves_valid_path_prefixes_trailing_slashes_and_ipv6(self, urlopen) -> None:
        urlopen.return_value = FakeResponse(b"{}")
        controls = (
            ("http://host.example/api/", "http://host.example/api/status"),
            ("https://[::1]:8000/artemis/", "https://[::1]:8000/artemis/status"),
            ("http://localhost:8000/", "http://localhost:8000/status"),
        )

        for base_url, expected_url in controls:
            with self.subTest(base_url=base_url):
                JsonTransport(base_url).request("GET", "status")
                request = urlopen.call_args.args[0]
                self.assertEqual(request.full_url, expected_url)

    @patch("urllib.request.urlopen")
    def test_sends_json_and_bearer_token(self, urlopen) -> None:
        urlopen.return_value = FakeResponse(json.dumps({"ok": True}).encode())
        transport = JsonTransport("https://host.example/", token="secret")

        result = transport.request("POST", "/api/run", json_body={"goal": "test"})

        self.assertEqual(result, {"ok": True})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://host.example/api/run")
        self.assertEqual(request.headers["Authorization"], "Bearer secret")
        self.assertEqual(json.loads(request.data), {"goal": "test"})

    @patch("urllib.request.urlopen")
    def test_maps_unauthorized_response(self, urlopen) -> None:
        urlopen.side_effect = urllib.error.HTTPError(
            "https://host.example/api/run",
            401,
            "Unauthorized",
            Message(),
            io.BytesIO(b'{"detail":"bad token"}'),
        )
        transport = JsonTransport("https://host.example")

        with self.assertRaisesRegex(AuthenticationError, "bad token"):
            transport.request("GET", "/api/run")

    @patch("urllib.request.urlopen")
    def test_invalid_json_raises_protocol_error(self, urlopen) -> None:
        urlopen.return_value = FakeResponse(b"not-json")
        transport = JsonTransport("https://host.example")

        with self.assertRaises(ProtocolError):
            transport.request("GET", "/api/status")

    @patch("urllib.request.urlopen")
    def test_network_failure_is_wrapped(self, urlopen) -> None:
        urlopen.side_effect = urllib.error.URLError("connection refused")
        transport = JsonTransport("https://host.example")

        with self.assertRaisesRegex(NetworkError, "connection refused"):
            transport.request("GET", "/api/status")


if __name__ == "__main__":
    unittest.main()
