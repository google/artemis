# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Opt-in live contract test for the remote-only Python SDK.

Run with:
    ARTEMIS_TEST_DEVICE_SERIAL=<serial> pytest tests/integration/test_thin_sdk_live.py
"""

from __future__ import annotations

import os
import asyncio
import time
import uuid

import pytest

from artemis_client import ArtemisClient, TaskRejectedError


def _live_client() -> tuple[ArtemisClient, str]:
    serial = os.environ.get("ARTEMIS_TEST_DEVICE_SERIAL")
    if not serial:
        pytest.skip("ARTEMIS_TEST_DEVICE_SERIAL is required for the live SDK test")
    return (
        ArtemisClient(
            os.environ.get("ARTEMIS_BASE_URL", "http://127.0.0.1:8000"),
            device_serial=serial,
            default_profile="flash",
            poll_interval=0.5,
        ),
        serial,
    )


@pytest.mark.android
@pytest.mark.asyncio
async def test_thin_sdk_remote_contract_on_real_device():
    client, serial = _live_client()

    readiness = await client.health()
    assert readiness

    capabilities = await client.capabilities()
    assert capabilities.supports("tasks.submit")

    devices = await client.list_devices()
    assert any(device.serial == serial and device.state == "device" for device in devices)

    task_id = str(uuid.uuid4())
    handle = await client.submit(
        "Open Android Settings, navigate to About phone, and verify that the visible model is Pixel 10 "
        "and the Android version is 16. Do not change any setting. Stop after both values are observed.",
        task_id=task_id,
        locked_app_package="com.android.settings",
    )
    assert handle.task_id == task_id

    duplicate = await client.submit(
        "Open Android Settings, navigate to About phone, and verify that the visible model is Pixel 10 "
        "and the Android version is 16. Do not change any setting. Stop after both values are observed.",
        task_id=task_id,
        locked_app_package="com.android.settings",
    )
    assert duplicate.task_id == task_id

    result = await client.wait_for_task(task_id, timeout=600)
    assert result.succeeded, result.error or result.raw

    cleanup_deadline = time.monotonic() + 30
    while time.monotonic() < cleanup_deadline:
        scheduler = await client.health()
        if scheduler.get("status") == "idle" and not scheduler.get("active_tasks"):
            break
        await asyncio.sleep(0.5)
    else:
        pytest.fail("Artemis scheduler did not release the device after task completion")


@pytest.mark.android
@pytest.mark.asyncio
async def test_thin_sdk_rejects_unknown_real_device_serial():
    client, _ = _live_client()

    with pytest.raises(TaskRejectedError):
        await client.submit(
            "This task must never reach a device",
            device_serial="artemis-nonexistent-device",
        )


@pytest.mark.android
@pytest.mark.asyncio
async def test_thin_sdk_can_cancel_remote_task_and_release_device():
    client, _ = _live_client()
    task_id = str(uuid.uuid4())
    await client.submit(
        "Open Android Settings and inspect the top-level categories without changing anything.",
        task_id=task_id,
        locked_app_package="com.android.settings",
    )

    assert await client.stop(task_id)

    cleanup_deadline = time.monotonic() + 30
    while time.monotonic() < cleanup_deadline:
        scheduler = await client.health()
        active_ids = {
            str(item.get("session_id"))
            for item in scheduler.get("active_tasks", [])
            if isinstance(item, dict)
        }
        queued_ids = {
            str(item.get("session_id"))
            for item in scheduler.get("queue", [])
            if isinstance(item, dict)
        }
        if task_id not in active_ids | queued_ids:
            break
        await asyncio.sleep(0.5)
    else:
        pytest.fail("Cancelled task still owns a scheduler slot or device lock")
