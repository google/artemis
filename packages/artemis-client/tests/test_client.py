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
    Device,
    NotFoundError,
    ProtocolError,
    TaskHandle,
    TaskRejectedError,
    TaskResult,
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


class IosPlatformSubmissionTests(unittest.IsolatedAsyncioTestCase):
    """Platform/workspace forwarding and iOS capability preflight."""

    CAPABLE = {
        "api_version": "1",
        "features": ["tasks.submit", "platform.ios"],
    }

    def _client(self, **kwargs) -> ArtemisClient:
        self.transport = FakeTransport()
        return ArtemisClient(
            "https://artemis.example.test",
            poll_interval=0.001,
            transport=self.transport,
            **kwargs,
        )

    @staticmethod
    def _admitted(platform=None):
        task = {"session_id": "task-1", "status": "pending"}
        if platform:
            task["platform"] = platform
        return {"status": "started", "tasks": [task]}

    async def test_ios_submit_preflights_capabilities_then_posts_platform(self):
        client = self._client(device_serial="SIM-UDID", platform="ios")
        self.transport.add("GET", "/api/v1/capabilities", self.CAPABLE)
        self.transport.add("POST", "/api/run", self._admitted("ios"))

        handle = await client.submit("Open Settings")

        get_call, post_call = self.transport.calls
        self.assertEqual(get_call[0], "GET")
        self.assertEqual(post_call[0], "POST")
        body = post_call[2]
        self.assertEqual(body["platform"], "ios")
        self.assertNotIn("ios_workspace", body)
        self.assertEqual(handle.platform, "ios")

    async def test_android_submit_sends_no_platform_and_no_preflight(self):
        client = self._client()
        self.transport.add("POST", "/api/run", self._admitted())

        await client.submit("Open Settings")

        self.assertEqual(len(self.transport.calls), 1)
        method, path, body = self.transport.calls[0]
        self.assertEqual((method, path), ("POST", "/api/run"))
        self.assertNotIn("platform", body)
        self.assertNotIn("ios_workspace", body)

    async def test_ios_workspace_is_forwarded_only_for_ios(self):
        client = self._client(platform="ios", ios_workspace="/host/MyApp.xcworkspace")
        self.transport.add("GET", "/api/v1/capabilities", self.CAPABLE)
        self.transport.add("POST", "/api/run", self._admitted("ios"))

        await client.submit("Open Settings")

        body = self.transport.calls[1][2]
        self.assertEqual(body["platform"], "ios")
        self.assertEqual(body["ios_workspace"], "/host/MyApp.xcworkspace")

        # A blank per-call token must inherit, never erase, the ctor choice.
        self.transport.add("GET", "/api/v1/capabilities", self.CAPABLE)
        self.transport.add("POST", "/api/run", self._admitted("ios"))
        await client.submit("Open Settings", platform="  ")
        self.assertEqual(self.transport.calls[3][2]["platform"], "ios")

    def test_constructor_workspace_requires_ios_platform(self):
        with self.assertRaises(ValueError):
            self._client(ios_workspace="/host/MyApp.xcworkspace")
        with self.assertRaises(ValueError):
            self._client(platform="android", ios_workspace="/host/MyApp.xcworkspace")

    async def test_workspace_on_non_ios_call_rejects_before_transport(self):
        client = self._client()
        with self.assertRaises(ValueError):
            await client.submit("goal", ios_workspace="/host/ws.xcworkspace")
        self.assertEqual(self.transport.calls, [])

    async def test_invalid_platform_rejects_before_transport(self):
        client = self._client()
        with self.assertRaises(ValueError):
            await client.submit("goal", platform="tvos")
        self.assertEqual(self.transport.calls, [])

    async def test_legacy_server_blocks_ios_before_posting(self):
        client = self._client(platform="ios")
        self.transport.add("GET", "/api/v1/capabilities", NotFoundError(404, "not implemented"))

        with self.assertRaises(TaskRejectedError):
            await client.submit("Open Settings")

        methods = [call[0] for call in self.transport.calls]
        self.assertEqual(methods, ["GET"])  # no POST /api/run attempted

    async def test_per_call_platform_override_beats_constructor_default(self):
        client = self._client(platform="android")
        self.transport.add("GET", "/api/v1/capabilities", self.CAPABLE)
        self.transport.add("POST", "/api/run", self._admitted("ios"))

        await client.submit("goal", platform="ios")

        self.assertEqual(self.transport.calls[1][2]["platform"], "ios")

    async def test_run_and_run_task_forward_platform(self):
        task_id = "00000000-0000-4000-8000-000000000199"
        client = self._client()
        self.transport.add("GET", "/api/v1/capabilities", self.CAPABLE)
        self.transport.add(
            "POST",
            "/api/run",
            {
                "status": "started",
                "tasks": [{"session_id": task_id, "status": "pending", "platform": "ios"}],
            },
        )
        self.transport.add(
            "GET",
            f"/api/sessions/{task_id}",
            {"session_id": task_id, "status": "completed"},
        )
        task = type(
            "T",
            (),
            {
                "goal": "Do it",
                "platform": "ios",
                "ios_workspace": "/host/ws.xcworkspace",
            },
        )()
        result = await client.run_task(task, task_id=task_id)

        self.assertTrue(result.done)
        body = self.transport.calls[1][2]
        self.assertEqual(body["platform"], "ios")
        self.assertEqual(body["ios_workspace"], "/host/ws.xcworkspace")

    async def test_platform_defaults_to_android_everywhere(self):
        client = self._client()
        self.transport.add("POST", "/api/run", self._admitted())
        handle = await client.submit("goal")
        self.assertEqual(handle.platform, "android")

        self.transport.add(
            "GET", "/api/sessions/task-1", {"session_id": "task-1", "status": "completed"}
        )
        result = await client.get_task("task-1")
        self.assertEqual(result.platform, "android")

        self.transport.add("GET", "/api/devices", {"devices": [{"serial": "x", "state": "device"}]})
        (device,) = await client.list_devices()
        self.assertEqual(device.platform, "android")

    def test_models_read_platform_from_top_level_or_device_info(self):
        self.assertEqual(
            TaskHandle.from_payload({"task_id": "t", "platform": " iOS "}).platform,
            "ios",
        )
        self.assertEqual(
            TaskResult.from_payload(
                {
                    "task_id": "t",
                    "status": "completed",
                    "device_info": {"mobile_platform": "IOS"},
                }
            ).platform,
            "ios",
        )
        self.assertEqual(
            Device.from_payload(
                {
                    "serial": "s",
                    "device_info": '{"mobile_platform": "ios"}',
                }
            ).platform,
            "ios",
        )
        self.assertEqual(Device.from_payload({"serial": "s", "platform": "ios"}).platform, "ios")


if __name__ == "__main__":
    unittest.main()
