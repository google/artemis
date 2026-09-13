# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Asynchronous, remote-only Artemis client."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import Mapping
from typing import Any, Literal, Protocol

from artemis_client.errors import NotFoundError, ProtocolError, TaskRejectedError, TaskTimeoutError
from artemis_client.models import Capabilities, Device, TaskHandle, TaskResult
from artemis_client.transport import JsonTransport


class _Transport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
    ) -> Any: ...


#: Pro-profile Checker presets accepted by ``/api/run`` (``verification_level``).
VerificationLevel = Literal["off", "final", "checkpoints", "strict"]
VERIFICATION_LEVELS: tuple[str, ...] = ("off", "final", "checkpoints", "strict")
#: Pro-profile Explorer perception versions accepted by ``/api/run`` (``explorer_mode``).
ExplorerMode = Literal["flash", "pro", "ultra"]
EXPLORER_MODES: tuple[str, ...] = ("flash", "pro", "ultra")


def _normalize_choice(value: str | None, name: str, choices: tuple[str, ...]) -> str | None:
    """Strip + lower-case an enumerated option, rejecting unknown values early."""
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    if normalized not in choices:
        raise ValueError(f"{name} must be one of {', '.join(choices)}; got {value!r}")
    return normalized


class ArtemisClient:
    """Thin client for an Artemis daemon running on another host.

    This class never imports ADB, an Artemis agent, an LLM provider, or a
    device driver. All execution is delegated to ``base_url``.
    """

    _LEGACY_FEATURES = frozenset(
        {
            "tasks.submit",
            "tasks.get",
            "tasks.stop",
            "devices.list",
            "system.readiness",
        }
    )

    def __init__(
        self,
        base_url: str | None = None,
        *,
        token: str | None = None,
        request_timeout: float = 30.0,
        poll_interval: float = 1.0,
        headers: Mapping[str, str] | None = None,
        transport: _Transport | None = None,
        device_id: str | None = None,
        device_serial: str | None = None,
        default_profile: Literal["flash", "pro"] = "flash",
        concurrency_mode: str = "per_device",
        max_concurrency: int | None = None,
        standalone: bool = False,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be greater than zero")
        resolved_base_url = base_url or os.environ.get("ARTEMIS_BASE_URL")
        if not resolved_base_url:
            daemon_host = os.environ.get("ARTEMIS_DAEMON_HOST", "127.0.0.1")
            daemon_port = os.environ.get("ARTEMIS_DAEMON_PORT", "8000")
            resolved_base_url = f"http://{daemon_host}:{daemon_port}"

        self.base_url = resolved_base_url.rstrip("/")
        self.poll_interval = float(poll_interval)
        self._device_serial = device_serial or device_id
        self.default_profile = default_profile
        self.concurrency_mode = str(concurrency_mode).strip().lower()
        self.max_concurrency = max_concurrency
        self.standalone = standalone
        self._transport = transport or JsonTransport(
            resolved_base_url,
            token=token,
            timeout=request_timeout,
            headers=headers,
        )

    @property
    def device_id(self) -> str | None:
        """Default remote device serial (legacy-compatible alias)."""
        return self._device_serial

    @device_id.setter
    def device_id(self, value: str | None) -> None:
        self._device_serial = value

    @property
    def device_serial(self) -> str | None:
        return self._device_serial

    @device_serial.setter
    def device_serial(self, value: str | None) -> None:
        self._device_serial = value

    def set_device(self, device_serial: str) -> ArtemisClient:
        """Set the default remote device and return this client."""
        self._device_serial = device_serial
        return self

    def set_concurrency_mode(self, mode: str) -> ArtemisClient:
        """Retain the legacy setting for callers migrating to server-side queues."""
        self.concurrency_mode = str(mode).strip().lower()
        return self

    async def health(self) -> Mapping[str, Any]:
        """Perform a fast liveness check against the scheduler status API."""
        payload = await self._request("GET", "/api/status")
        return self._mapping(payload, endpoint="/api/status")

    async def readiness(self) -> Mapping[str, Any]:
        """Run the host's full device and toolchain readiness diagnostics.

        This endpoint can take substantially longer than :meth:`health`; set
        a larger ``request_timeout`` when constructing the client if needed.
        """
        payload = await self._request("GET", "/api/system/readiness")
        return self._mapping(payload, endpoint="/api/system/readiness")

    async def capabilities(self) -> Capabilities:
        """Discover server features, with a baseline for today's legacy API."""
        try:
            payload = await self._request("GET", "/api/v1/capabilities")
        except NotFoundError:
            return Capabilities(api_version="legacy", features=self._LEGACY_FEATURES)
        return Capabilities.from_payload(self._mapping(payload, endpoint="/api/v1/capabilities"))

    async def list_devices(self) -> tuple[Device, ...]:
        """List Android devices visible to the remote Artemis host."""
        payload = await self._request("GET", "/api/devices")
        if isinstance(payload, Mapping):
            raw_devices = payload.get("devices", [])
        else:
            raw_devices = payload
        if not isinstance(raw_devices, list):
            raise ProtocolError("/api/devices response must contain a device list")
        devices: list[Device] = []
        for item in raw_devices:
            if not isinstance(item, Mapping):
                raise ProtocolError("/api/devices contained a non-object device entry")
            devices.append(Device.from_payload(item))
        return tuple(devices)

    async def submit(
        self,
        goal: str,
        *,
        profile: Literal["flash", "pro"] | None = None,
        device_serial: str | None = None,
        expected_output: str | None = None,
        enable_outputter: bool | None = None,
        locked_app_package: str | None = None,
        app_path: str | None = None,
        conversation_id: str | None = None,
        task_id: str | None = None,
        verification_level: VerificationLevel | None = None,
        explorer_mode: ExplorerMode | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> TaskHandle:
        """Submit one task and return immediately after scheduler admission.

        ``task_id`` is generated client-side and sent as the legacy
        ``session_id``. Reusing it makes submission retries idempotent.
        ``verification_level`` (``off`` | ``final`` | ``checkpoints`` |
        ``strict``: how much the Checker audits a Pro run) and
        ``explorer_mode`` (``flash`` | ``pro`` | ``ultra``: the Pro Operator's
        perception depth) are Pro-only tuning knobs; the Flash profile ignores
        them. Experimental, forward-compatible fields belong in ``options``.
        """
        normalized_goal = goal.strip()
        if not normalized_goal:
            raise ValueError("goal must not be empty")
        resolved_level = _normalize_choice(
            verification_level, "verification_level", VERIFICATION_LEVELS
        )
        resolved_mode = _normalize_choice(explorer_mode, "explorer_mode", EXPLORER_MODES)
        if task_id is None:
            resolved_task_id = str(uuid.uuid4())
        else:
            try:
                resolved_task_id = str(uuid.UUID(task_id))
            except (AttributeError, TypeError, ValueError) as exc:
                raise ValueError("task_id must be a valid UUID string") from exc
        resolved_profile = profile or self.default_profile
        resolved_device = device_serial or self.device_serial
        payload: dict[str, Any] = {
            "goal": normalized_goal,
            "profile": resolved_profile,
            "session_id": resolved_task_id,
            "ingress": "python_sdk",
        }
        optional_values = {
            "device_serial": resolved_device,
            "expected_output": expected_output,
            "enable_outputter": enable_outputter,
            "locked_app_package": locked_app_package,
            "app_path": app_path,
            "conversation_id": conversation_id,
            "verification_level": resolved_level,
            "explorer_mode": resolved_mode,
            "options": dict(options) if options is not None else None,
        }
        payload.update({key: value for key, value in optional_values.items() if value is not None})

        response = self._mapping(
            await self._request("POST", "/api/run", json_body=payload),
            endpoint="/api/run",
        )
        status = str(response.get("status") or "unknown").lower()
        tasks = response.get("tasks")
        if status == "rejected" or (isinstance(tasks, list) and not tasks):
            detail = str(response.get("error") or "Artemis host rejected the task")
            raise TaskRejectedError(detail)
        if not isinstance(tasks, list) or not tasks or not isinstance(tasks[0], Mapping):
            raise ProtocolError("/api/run response did not contain an admitted task")

        task_payload = dict(tasks[0])
        task_payload.setdefault("session_id", resolved_task_id)
        task_payload.setdefault("status", status)
        return TaskHandle.from_payload(task_payload)

    async def get_task(self, task_id: str) -> TaskResult:
        """Get a task from session storage or the live scheduler queue."""
        try:
            payload = await self._request("GET", f"/api/sessions/{task_id}")
            return TaskResult.from_payload(
                self._mapping(payload, endpoint=f"/api/sessions/{task_id}"),
                task_id=task_id,
            )
        except NotFoundError:
            status_payload = self._mapping(
                await self._request("GET", "/api/status"),
                endpoint="/api/status",
            )
            live_task = self._find_live_task(status_payload, task_id)
            if live_task is None:
                return TaskResult(task_id=task_id, status="launching")
            return TaskResult.from_payload(live_task, task_id=task_id)

    async def wait_for_task(
        self,
        task_id: str,
        *,
        timeout: float = 1800.0,
        poll_interval: float | None = None,
    ) -> TaskResult:
        """Wait until a task reaches a terminal state."""
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        interval = self.poll_interval if poll_interval is None else float(poll_interval)
        if interval <= 0:
            raise ValueError("poll_interval must be greater than zero")

        started = time.monotonic()
        while True:
            result = await self.get_task(task_id)
            if result.done:
                return result
            elapsed = time.monotonic() - started
            if elapsed >= timeout:
                raise TaskTimeoutError(task_id, timeout)
            await asyncio.sleep(min(interval, max(0.0, timeout - elapsed)))

    async def run(
        self,
        goal: str,
        *,
        profile: Literal["flash", "pro"] | None = None,
        device_serial: str | None = None,
        expected_output: str | None = None,
        enable_outputter: bool | None = None,
        locked_app_package: str | None = None,
        app_path: str | None = None,
        conversation_id: str | None = None,
        task_id: str | None = None,
        verification_level: VerificationLevel | None = None,
        explorer_mode: ExplorerMode | None = None,
        options: Mapping[str, Any] | None = None,
        timeout: float = 1800.0,
        poll_interval: float | None = None,
    ) -> TaskResult:
        """Submit a task and wait for its terminal result (see :meth:`submit`)."""
        handle = await self.submit(
            goal,
            profile=profile,
            device_serial=device_serial,
            expected_output=expected_output,
            enable_outputter=enable_outputter,
            locked_app_package=locked_app_package,
            app_path=app_path,
            conversation_id=conversation_id,
            task_id=task_id,
            verification_level=verification_level,
            explorer_mode=explorer_mode,
            options=options,
        )
        return await self.wait_for_task(
            handle.task_id,
            timeout=timeout,
            poll_interval=poll_interval,
        )

    async def run_task(self, task: Any, **overrides: Any) -> TaskResult:
        """Run a task-like object while delegating all execution remotely."""
        goal = getattr(task, "goal", None)
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("task must expose a non-empty goal attribute")
        values: dict[str, Any] = {
            "profile": getattr(task, "profile", None),
            "device_serial": getattr(task, "device_serial", None)
            or getattr(task, "device_id", None),
            "locked_app_package": getattr(task, "locked_package", None),
        }
        values.update(overrides)
        return await self.run(goal, **values)

    async def stop(self, task_id: str) -> bool:
        """Request cancellation of one remote task."""
        payload = self._mapping(
            await self._request(
                "POST",
                "/api/stop",
                json_body={"session_id": task_id},
            ),
            endpoint="/api/stop",
        )
        return str(payload.get("status") or "").lower() == "stopped"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
    ) -> Any:
        return await asyncio.to_thread(
            self._transport.request,
            method,
            path,
            json_body=json_body,
        )

    @staticmethod
    def _mapping(payload: Any, *, endpoint: str) -> Mapping[str, Any]:
        if not isinstance(payload, Mapping):
            raise ProtocolError(f"{endpoint} response must be a JSON object")
        return payload

    @staticmethod
    def _find_live_task(
        scheduler: Mapping[str, Any],
        task_id: str,
    ) -> Mapping[str, Any] | None:
        for collection_name in ("queue", "active_tasks"):
            collection = scheduler.get(collection_name) or []
            if not isinstance(collection, list):
                continue
            for item in collection:
                if not isinstance(item, Mapping):
                    continue
                item_id = item.get("task_id") or item.get("session_id")
                if str(item_id) == task_id:
                    return item

        active_id = scheduler.get("task_id") or scheduler.get("session_id")
        if active_id is not None and str(active_id) == task_id:
            synthesized = dict(scheduler)
            synthesized.setdefault("session_id", task_id)
            return synthesized
        return None
