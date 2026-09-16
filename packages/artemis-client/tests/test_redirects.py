# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Redirect policy exercised through real loopback HTTP connections."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import threading
import unittest
import urllib.request

from artemis_client import ApiError, JsonTransport, ProtocolError
from artemis_client.transport import _SameOriginRedirectHandler


@contextmanager
def server():
    requests = []
    redirects = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.respond()

        def do_HEAD(self):
            self.respond()

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.respond()

        def respond(self):
            requests.append((self.command, self.path, dict(self.headers)))
            if self.path in redirects:
                status, target = redirects[self.path]
                self.send_response(status)
                self.send_header("Location", target)
                self.end_headers()
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(b'{"ok":true}')

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", requests, redirects
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
        if thread.is_alive():
            raise RuntimeError("Loopback server did not stop")


class RedirectTests(unittest.TestCase):
    def test_origin_comparison_and_rejected_response_cleanup(self):
        cases = (
            ("https://host.example/a", "http://host.example/b", False),
            ("http://host.example/a", "https://host.example/b", False),
            ("https://host.example/a", "https://other.example/b", False),
            ("https://host.example/a", "https://host.example:444/b", False),
            ("http://host.example:0/a", "http://host.example/b", False),
            ("https://host.example/a", "https://user@host.example/b", False),
            ("https://host.example/a", "https://host.example:bad/b", False),
            ("https://HOST.example/a", "https://host.example:443/b", True),
            ("http://host.example/a", "http://host.example:80/b", True),
            ("http://[::1]/a", "http://[::1]:80/b", True),
        )
        for source, target, allowed in cases:
            with self.subTest(source=source, target=target):
                body = io.BytesIO(b"redirect")
                request = urllib.request.Request(source)
                handler = _SameOriginRedirectHandler()
                if allowed:
                    redirected = handler.redirect_request(request, body, 302, "Found", {}, target)
                    self.assertEqual(redirected.full_url, target)
                else:
                    with self.assertRaises(ProtocolError):
                        handler.redirect_request(request, body, 302, "Found", {}, target)
                    self.assertTrue(body.closed)
                body.close()

    def test_cross_origin_redirect_never_reaches_destination(self):
        with server() as (base, sent, routes), server() as (target, received, _):
            for status in (301, 302, 303, 307, 308):
                for destination in (target, target.replace("127.0.0.1", "localhost")):
                    with self.subTest(status=status, destination=destination):
                        routes["/start"] = (status, destination + "/capture")
                        transport = JsonTransport(
                            base, token="synthetic", headers={"X-Api-Key": "synthetic"}
                        )
                        with self.assertRaises(ProtocolError):
                            transport.request("GET", "/start")
                        self.assertEqual(received, [])
            self.assertEqual(len(sent), 10)

    def test_same_origin_relative_get_retains_headers(self):
        with server() as (base, sent, routes):
            routes["/start"] = (302, "/finish")
            result = JsonTransport(base, headers={"Authorization": "synthetic"}).request(
                "GET", "/start"
            )
            self.assertEqual(result, {"ok": True})
            self.assertEqual([item[:2] for item in sent], [("GET", "/start"), ("GET", "/finish")])
            self.assertEqual(sent[-1][2]["Authorization"], "synthetic")

    def test_head_redirect_preserves_method(self):
        with server() as (base, sent, routes):
            routes["/start"] = (302, "/finish")
            self.assertIsNone(JsonTransport(base).request("HEAD", "/start"))
            self.assertEqual([item[0] for item in sent], ["HEAD", "HEAD"])

    def test_post_redirect_is_not_replayed_or_converted_to_get(self):
        with server() as (base, sent, routes):
            for status in (301, 302, 303, 307, 308):
                with self.subTest(status=status):
                    sent.clear()
                    routes["/api/run"] = (status, "/other")
                    with self.assertRaises(ProtocolError):
                        JsonTransport(base).request("POST", "/api/run", json_body={"goal": "test"})
                    self.assertEqual([item[:2] for item in sent], [("POST", "/api/run")])

    def test_redirect_loop_remains_bounded(self):
        with server() as (base, sent, routes):
            routes["/loop"] = (302, "/loop")
            with self.assertRaises(ApiError):
                JsonTransport(base).request("GET", "/loop")
            self.assertLessEqual(len(sent), 5)


if __name__ == "__main__":
    unittest.main()
