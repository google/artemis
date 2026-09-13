# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

from __future__ import annotations

import unittest
from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from artemis_client import (
    ArtemisClient,
    NotFoundError,
    ProtocolError,
    TaskRejectedError,
    TaskTimeoutError,
)


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


class ArtemisClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.transport = FakeTransport()
        self.client = ArtemisClient(
            "https://artemis.example.test",
            poll_interval=0.001,
            transport=self.transport,
        )

    async def test_submit_sends_idempotent_session_id(self) -> None:
        task_id = "00000000-0000-4000-8000-000000000123"
        self.transport.add(
            "POST",
            "/api/run",
            {
                "status": "started",
                "tasks": [
                    {
                        "session_id": task_id,
                        "status": "pending",
                        "device_serial": "pixel-8",
                    }
                ],
            },
        )

        handle = await self.client.submit(
            "Open Settings",
            task_id=task_id,
            device_serial="pixel-8",
            options={"record_video": True},
        )

        self.assertEqual(handle.task_id, task_id)
        self.assertEqual(handle.device_serial, "pixel-8")
        body = self.transport.calls[0][2]
        assert body is not None
        self.assertEqual(body["session_id"], task_id)
        self.assertEqual(body["ingress"], "python_sdk")
        self.assertEqual(body["options"], {"record_video": True})

    async def test_submit_forwards_pro_tuning_knobs_normalised(self) -> None:
        task_id = "00000000-0000-4000-8000-000000000321"
        self.transport.add(
            "POST",
            "/api/run",
            {"status": "started", "tasks": [{"session_id": task_id, "status": "pending"}]},
        )

        await self.client.submit(
            "Audit checkout",
            profile="pro",
            task_id=task_id,
            verification_level=" Strict ",
            explorer_mode="ULTRA",
        )

        body = self.transport.calls[0][2]
        assert body is not None
        self.assertEqual(body["verification_level"], "strict")
        self.assertEqual(body["explorer_mode"], "ultra")

    async def test_submit_omits_pro_tuning_knobs_when_unset(self) -> None:
        task_id = "00000000-0000-4000-8000-000000000322"
        self.transport.add(
            "POST",
            "/api/run",
            {"status": "started", "tasks": [{"session_id": task_id, "status": "pending"}]},
        )

        await self.client.submit("Open Settings", task_id=task_id, explorer_mode="  ")

        body = self.transport.calls[0][2]
        assert body is not None
        self.assertNotIn("verification_level", body)
        self.assertNotIn("explorer_mode", body)

    async def test_submit_rejects_unknown_pro_tuning_values_before_any_request(self) -> None:
        with self.assertRaisesRegex(ValueError, "verification_level"):
            await self.client.submit("Open Settings", verification_level="paranoid")
        with self.assertRaisesRegex(ValueError, "explorer_mode"):
            await self.client.submit("Open Settings", explorer_mode="turbo")
        self.assertEqual(self.transport.calls, [])

    async def test_submit_rejected_task_raises_specific_error(self) -> None:
        self.transport.add(
            "POST",
            "/api/run",
            {
                "status": "rejected",
                "error": "Device is offline",
                "tasks": [],
            },
        )

        with self.assertRaisesRegex(TaskRejectedError, "Device is offline"):
            await self.client.submit("Open Settings")

    async def test_submit_rejects_non_uuid_task_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "valid UUID"):
            await self.client.submit("Open Settings", task_id="not-a-uuid")

    async def test_run_finds_queued_task_then_reads_terminal_session(self) -> None:
        task_id = "00000000-0000-4000-8000-000000000124"
        self.transport.add(
            "POST",
            "/api/run",
            {
                "status": "started",
                "tasks": [{"session_id": task_id, "status": "pending"}],
            },
        )
        self.transport.add(
            "GET",
            f"/api/sessions/{task_id}",
            NotFoundError(404, "not created yet"),
            {
                "session_id": task_id,
                "status": "completed",
                "goal": "Open Settings",
                "current_turn": 4,
                "summary": "Battery page opened",
            },
        )
        self.transport.add(
            "GET",
            "/api/status",
            {
                "status": "running",
                "queue": [{"session_id": task_id, "status": "pending"}],
            },
        )

        result = await self.client.run("Open Settings", task_id=task_id, timeout=1)

        self.assertTrue(result.done)
        self.assertTrue(result.succeeded)
        self.assertEqual(result.turns, 4)
        self.assertEqual(result.output, "Battery page opened")

    async def test_get_task_returns_launching_when_not_visible_yet(self) -> None:
        self.transport.add(
            "GET",
            "/api/sessions/new-task",
            NotFoundError(404, "missing"),
        )
        self.transport.add("GET", "/api/status", {"status": "idle", "queue": []})

        result = await self.client.get_task("new-task")

        self.assertEqual(result.status, "launching")
        self.assertFalse(result.done)

    async def test_wait_for_task_times_out(self) -> None:
        class LaunchingClient(ArtemisClient):
            async def get_task(self, task_id: str):  # type: ignore[override]
                from artemis_client import TaskResult

                return TaskResult(task_id=task_id, status="running")

        client = LaunchingClient(
            "https://artemis.example.test",
            poll_interval=0.001,
            transport=self.transport,
        )
        with self.assertRaises(TaskTimeoutError):
            await client.wait_for_task("slow-task", timeout=0.003)

    async def test_list_devices_accepts_legacy_shape(self) -> None:
        self.transport.add(
            "GET",
            "/api/devices",
            {
                "devices": [
                    {
                        "serial": "emulator-5554",
                        "state": "device",
                        "model": "Pixel_8",
                        "busy": True,
                    }
                ]
            },
        )

        devices = await self.client.list_devices()

        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].serial, "emulator-5554")
        self.assertTrue(devices[0].busy)

    async def test_capabilities_falls_back_for_legacy_server(self) -> None:
        self.transport.add(
            "GET",
            "/api/v1/capabilities",
            NotFoundError(404, "not implemented"),
        )

        capabilities = await self.client.capabilities()

        self.assertEqual(capabilities.api_version, "legacy")
        self.assertTrue(capabilities.supports("tasks.submit"))

    async def test_health_uses_fast_scheduler_endpoint(self) -> None:
        self.transport.add("GET", "/api/status", {"status": "idle"})

        health = await self.client.health()

        self.assertEqual(health["status"], "idle")

    async def test_readiness_uses_full_diagnostics_endpoint(self) -> None:
        self.transport.add(
            "GET",
            "/api/system/readiness",
            {"overall_status": "ready"},
        )

        readiness = await self.client.readiness()

        self.assertEqual(readiness["overall_status"], "ready")

    async def test_invalid_devices_payload_is_rejected(self) -> None:
        self.transport.add("GET", "/api/devices", {"devices": "not-a-list"})

        with self.assertRaises(ProtocolError):
            await self.client.list_devices()

    async def test_stop_targets_one_session(self) -> None:
        self.transport.add("POST", "/api/stop", {"status": "stopped"})

        stopped = await self.client.stop("task-stop")

        self.assertTrue(stopped)
        self.assertEqual(
            self.transport.calls[0],
            ("POST", "/api/stop", {"session_id": "task-stop"}),
        )


if __name__ == "__main__":
    unittest.main()
