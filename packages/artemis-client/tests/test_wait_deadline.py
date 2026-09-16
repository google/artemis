# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

from __future__ import annotations

import asyncio
import threading
import unittest
from collections.abc import Mapping
from contextlib import ExitStack
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from artemis_client import ArtemisClient, NetworkError, NotFoundError, TaskTimeoutError
from test_client import FakeTransport


class PollClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.oversleep = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay + self.oversleep


class WaitDeadlineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.clock = PollClock()
        self.transport = FakeTransport()
        self.client = ArtemisClient(transport=self.transport)
        contexts = ExitStack()
        self.addCleanup(contexts.close)
        contexts.enter_context(
            patch("artemis_client.client.time", SimpleNamespace(monotonic=self.clock.monotonic))
        )
        contexts.enter_context(
            patch(
                "artemis_client.client.asyncio",
                SimpleNamespace(sleep=self.clock.sleep, to_thread=asyncio.to_thread),
            )
        )

    async def test_first_terminal_observation_returns_without_sleep(self) -> None:
        self.transport.add("GET", "/api/sessions/task", {"status": "completed"})

        result = await self.client.wait_for_task("task", timeout=1)

        self.assertTrue(result.succeeded)
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(self.transport.calls, [("GET", "/api/sessions/task", None)])

    async def test_terminal_observation_before_deadline_returns(self) -> None:
        self.transport.add(
            "GET", "/api/sessions/task", {"status": "running"}, {"status": "completed"}
        )

        result = await self.client.wait_for_task("task", timeout=2, poll_interval=0.5)

        self.assertTrue(result.succeeded)
        self.assertEqual(self.clock.sleeps, [0.5])
        self.assertEqual(len(self.transport.calls), 2)

    async def test_sleep_reaching_deadline_does_not_start_another_poll(self) -> None:
        for oversleep in (0.0, 0.25):
            with self.subTest(oversleep=oversleep):
                self.clock.now = 0.0
                self.clock.oversleep = oversleep
                self.clock.sleeps.clear()
                transport = FakeTransport()
                transport.add(
                    "GET", "/api/sessions/task", {"status": "running"}, {"status": "completed"}
                )
                client = ArtemisClient(transport=transport)

                with self.assertRaises(TaskTimeoutError):
                    await client.wait_for_task("task", timeout=0.5, poll_interval=2)

                self.assertEqual(self.clock.sleeps, [0.5])
                self.assertEqual(transport.calls, [("GET", "/api/sessions/task", None)])

    async def test_session_404_uses_queue_and_running_fallback_before_terminal(self) -> None:
        self.transport.add(
            "GET",
            "/api/sessions/task",
            NotFoundError(404, "pending"),
            NotFoundError(404, "running"),
            {"status": "completed"},
        )
        self.transport.add(
            "GET",
            "/api/status",
            {"queue": [{"session_id": "task", "status": "pending"}]},
            {"active_tasks": [{"session_id": "task", "status": "running"}]},
        )

        result = await self.client.wait_for_task("task", timeout=3)

        self.assertTrue(result.succeeded)
        self.assertEqual(self.clock.sleeps, [1.0, 1.0])
        self.assertEqual(
            self.transport.calls,
            [
                ("GET", "/api/sessions/task", None),
                ("GET", "/api/status", None),
                ("GET", "/api/sessions/task", None),
                ("GET", "/api/status", None),
                ("GET", "/api/sessions/task", None),
            ],
        )

    async def test_cancellation_during_sleep_does_not_poll_or_stop_remote_task(self) -> None:
        sleeping = asyncio.Event()
        self.transport.add("GET", "/api/sessions/task", {"status": "running"})

        async def sleep_until_cancelled(delay: float) -> None:
            sleeping.set()
            await asyncio.Event().wait()

        with patch("artemis_client.client.asyncio.sleep", sleep_until_cancelled):
            waiter = asyncio.create_task(self.client.wait_for_task("task", timeout=1))
            try:
                await asyncio.wait_for(sleeping.wait(), timeout=5)
                waiter.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await waiter
            finally:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)

        self.assertEqual(self.transport.calls, [("GET", "/api/sessions/task", None)])

    async def test_request_in_flight_at_expiry_preserves_result_and_error_contract(self) -> None:
        for response in ({"status": "completed"}, {"status": "running"}, NetworkError("offline")):
            with self.subTest(response=response):
                self.clock.now = 0.0
                self.clock.sleeps.clear()
                transport = BlockingTransport(response)
                client = ArtemisClient(transport=transport)
                waiter = asyncio.create_task(client.wait_for_task("task", timeout=1))
                try:
                    await asyncio.wait_for(transport.started.wait(), timeout=5)
                    self.clock.now = 2.0
                    self.assertFalse(waiter.done())
                    self.assertFalse(transport.finished.is_set())
                    transport.release.set()
                    if isinstance(response, NetworkError):
                        with self.assertRaisesRegex(NetworkError, "offline"):
                            await waiter
                    elif response["status"] == "running":
                        with self.assertRaises(TaskTimeoutError):
                            await waiter
                    else:
                        self.assertTrue((await waiter).succeeded)
                finally:
                    transport.release.set()
                    await asyncio.gather(waiter, return_exceptions=True)
                    await asyncio.wait_for(transport.finished.wait(), timeout=5)

                self.assertEqual(self.clock.sleeps, [])
                self.assertEqual(transport.calls, [("GET", "/api/sessions/task", None)])

    async def test_cancellation_does_not_stop_in_flight_transport_thread(self) -> None:
        transport = BlockingTransport({"status": "completed"})
        client = ArtemisClient(transport=transport)
        waiter = asyncio.create_task(client.wait_for_task("task", timeout=1))
        try:
            await asyncio.wait_for(transport.started.wait(), timeout=5)
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            self.assertFalse(transport.finished.is_set())
        finally:
            transport.release.set()
            await asyncio.gather(waiter, return_exceptions=True)
            await asyncio.wait_for(transport.finished.wait(), timeout=5)

        self.assertEqual(transport.calls, [("GET", "/api/sessions/task", None)])


class BlockingTransport(FakeTransport):
    def __init__(self, response: dict[str, str] | NetworkError) -> None:
        super().__init__()
        self.add("GET", "/api/sessions/task", response)
        self.loop = asyncio.get_running_loop()
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.release = threading.Event()

    def request(self, method: str, path: str, *, json_body: Mapping[str, Any] | None = None) -> Any:
        self.loop.call_soon_threadsafe(self.started.set)
        try:
            if not self.release.wait(timeout=5):
                raise AssertionError("Test did not release the transport thread")
            return super().request(method, path, json_body=json_body)
        finally:
            self.loop.call_soon_threadsafe(self.finished.set)


if __name__ == "__main__":
    unittest.main()
