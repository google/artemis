# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Small JSON-over-HTTP transport implemented with the Python standard library."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from typing import Any, Mapping
from urllib.parse import urlsplit

from artemis_client.errors import (
    ApiError,
    AuthenticationError,
    ConflictError,
    NetworkError,
    NotFoundError,
    ProtocolError,
)


class JsonTransport:
    """Synchronous transport used internally by the async client.

    Calls are moved to worker threads by :class:`ArtemisClient`, keeping the
    public API asynchronous without adding an HTTP dependency.
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = 30.0,
        headers: Mapping[str, str] | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        normalized = base_url.rstrip("/")
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute http:// or https:// URL")
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")

        self.base_url = normalized
        self.timeout = float(timeout)
        self.ssl_context = ssl_context
        self.headers = {
            "Accept": "application/json",
            "User-Agent": "artemis-client/0.1",
            **dict(headers or {}),
        }
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
    ) -> Any:
        if not path.startswith("/"):
            path = f"/{path}"
        url = f"{self.base_url}{path}"
        body = None
        headers = dict(self.headers)
        if json_body is not None:
            body = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method=method.upper(),
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout,
                context=self.ssl_context,
            ) as response:
                data = response.read()
                if not data:
                    return None
                try:
                    return json.loads(data.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ProtocolError(f"Artemis host returned invalid JSON from {path}") from exc
        except urllib.error.HTTPError as exc:
            payload = self._decode_error_payload(exc)
            detail = self._error_detail(payload, exc.reason)
            error_type = {
                401: AuthenticationError,
                403: AuthenticationError,
                404: NotFoundError,
                409: ConflictError,
            }.get(exc.code, ApiError)
            raise error_type(exc.code, detail, payload=payload) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise NetworkError(f"Could not reach Artemis host at {url}: {reason}") from exc

    @staticmethod
    def _decode_error_payload(error: urllib.error.HTTPError) -> Any:
        try:
            data = error.read()
        except OSError:
            return None
        if not data:
            return None
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return data.decode("utf-8", errors="replace")

    @staticmethod
    def _error_detail(payload: Any, fallback: Any) -> str:
        if isinstance(payload, Mapping):
            detail = payload.get("detail") or payload.get("error") or payload.get("message")
            if detail is not None:
                return str(detail)
        if isinstance(payload, str) and payload.strip():
            return payload.strip()
        return str(fallback or "request failed")
