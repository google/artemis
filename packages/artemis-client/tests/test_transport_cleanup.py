# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Regression controls for ownership of urllib HTTP error response bodies."""

from __future__ import annotations

import io
import unittest
import urllib.error
from email.message import Message
from unittest.mock import patch

from artemis_client import (
    ApiError,
    AuthenticationError,
    ConflictError,
    JsonTransport,
    NotFoundError,
)


class BrokenBody(io.BytesIO):
    def read(self, *args, **kwargs):
        raise OSError("read failed")


class UnexpectedFailureBody(io.BytesIO):
    def read(self, *args, **kwargs):
        raise RuntimeError("unexpected decoder failure")


class ErrorResponseOwnershipTests(unittest.TestCase):
    def test_translated_http_error_closes_body_and_preserves_details(self):
        for status, error_type in (
            (401, AuthenticationError),
            (403, AuthenticationError),
            (404, NotFoundError),
            (409, ConflictError),
            (503, ApiError),
        ):
            with self.subTest(status=status):
                body = io.BytesIO(b'{"detail":"denied"}')
                http_error = urllib.error.HTTPError(
                    "https://host.example/api/status", status, "fallback", Message(), body
                )
                with patch("urllib.request.urlopen", side_effect=http_error):
                    with self.assertRaises(error_type) as caught:
                        JsonTransport("https://host.example").request("GET", "/api/status")
                self.assertEqual(caught.exception.status_code, status)
                self.assertEqual(caught.exception.payload, {"detail": "denied"})
                self.assertIn("denied", str(caught.exception))
                self.assertIs(caught.exception.__cause__, http_error)
                self.assertTrue(body.closed)

    def test_text_and_empty_error_bodies_are_closed(self):
        for payload, detail in ((b"plain failure", "plain failure"), (b"", "fallback")):
            with self.subTest(payload=payload):
                body = io.BytesIO(payload)
                http_error = urllib.error.HTTPError(
                    "https://host.example/api/status", 503, "fallback", Message(), body
                )
                with patch("urllib.request.urlopen", side_effect=http_error):
                    with self.assertRaisesRegex(ApiError, detail):
                        JsonTransport("https://host.example").request("GET", "/api/status")
                self.assertTrue(body.closed)

    def test_read_error_keeps_http_status_and_closes_body(self):
        body = BrokenBody()
        http_error = urllib.error.HTTPError(
            "https://host.example/api/status", 503, "fallback", Message(), body
        )
        with patch("urllib.request.urlopen", side_effect=http_error):
            with self.assertRaises(ApiError) as caught:
                JsonTransport("https://host.example").request("GET", "/api/status")
        self.assertEqual(caught.exception.status_code, 503)
        self.assertIsNone(caught.exception.payload)
        self.assertIs(caught.exception.__cause__, http_error)
        self.assertTrue(body.closed)

    def test_unexpected_read_failure_is_not_swallowed_but_body_is_closed(self):
        body = UnexpectedFailureBody()
        http_error = urllib.error.HTTPError(
            "https://host.example/api/status", 503, "fallback", Message(), body
        )
        with patch("urllib.request.urlopen", side_effect=http_error):
            with self.assertRaisesRegex(RuntimeError, "unexpected decoder failure"):
                JsonTransport("https://host.example").request("GET", "/api/status")
        self.assertTrue(body.closed)


if __name__ == "__main__":
    unittest.main()
