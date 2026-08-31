# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Compatibility tests for the single, remote-only ArtemisClient implementation."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Any

import pytest

import artemis
from artemis import ArtemisClient, ConcurrencyMode, Task, TaskResult
from artemis_client import ArtemisClient as ThinArtemisClient
from artemis_client import TaskResult as ThinTaskResult
from artemis_client.errors import NotFoundError


class FakeTransport:
    def __init__(self) -> None:
        self.responses: dict[tuple[str, str], list[Any]] = defaultdict(list)
        self.calls: list[tuple[str, str, Mapping[str, Any] | None]] = []

    def add(self, method: str, path: str, *responses: Any) -> None:
        self.responses[(method, path)].extend(responses)

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
    ) -> Any:
        self.calls.append((method, path, json_body))
        queued = self.responses[(method, path)]
        if not queued:
            raise AssertionError(f"Unexpected request: {method} {path}")
        response = queued.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def test_full_runtime_reexports_thin_client_types():
    assert ArtemisClient is ThinArtemisClient
    assert TaskResult is ThinTaskResult
    assert artemis.ArtemisClient is ThinArtemisClient


def test_legacy_constructor_settings_are_remote_defaults():
    client = ArtemisClient(
        device_serial="emulator-5554",
        default_profile="pro",
        concurrency_mode=ConcurrencyMode.PER_DEVICE,
        transport=FakeTransport(),
    )

    assert client.base_url == "http://127.0.0.1:8000"
    assert client.device_id == "emulator-5554"
    assert client.default_profile == "pro"
    assert client.concurrency_mode == "per_device"
    assert client.set_device("pixel-10") is client
    assert client.device_serial == "pixel-10"


@pytest.mark.asyncio
async def test_run_uses_remote_api_and_returns_thin_result():
    task_id = "00000000-0000-4000-8000-000000000001"
    transport = FakeTransport()
    transport.add(
        "POST",
        "/api/run",
        {"status": "started", "tasks": [{"session_id": task_id, "status": "pending"}]},
    )
    transport.add(
        "GET",
        f"/api/sessions/{task_id}",
        NotFoundError(404, "not ready"),
        {"session_id": task_id, "status": "completed", "current_turn": 3},
    )
    transport.add(
        "GET",
        "/api/status",
        {"status": "running", "queue": [{"session_id": task_id, "status": "pending"}]},
    )
    client = ArtemisClient(
        device_serial="pixel-10",
        default_profile="flash",
        poll_interval=0.001,
        transport=transport,
    )

    result = await client.run("Open Settings", task_id=task_id, timeout=1)

    assert isinstance(result, ThinTaskResult)
    assert result.succeeded
    request_body = transport.calls[0][2]
    assert request_body is not None
    assert request_body["device_serial"] == "pixel-10"
    assert request_body["profile"] == "flash"


@pytest.mark.asyncio
async def test_legacy_task_object_is_forwarded_to_remote_api():
    task_id = "00000000-0000-4000-8000-000000000002"
    transport = FakeTransport()
    transport.add(
        "POST",
        "/api/run",
        {"status": "started", "tasks": [{"session_id": task_id, "status": "pending"}]},
    )
    transport.add(
        "GET",
        f"/api/sessions/{task_id}",
        {"session_id": task_id, "status": "success"},
    )
    client = ArtemisClient(transport=transport)
    task = Task(
        goal="Verify search functionality",
        profile="flash",
        device_serial="pixel-10",
        locked_package="com.android.settings",
    )

    result = await client.run_task(task, task_id=task_id, timeout=1)

    assert result.succeeded
    request_body = transport.calls[0][2]
    assert request_body is not None
    assert request_body["locked_app_package"] == "com.android.settings"
