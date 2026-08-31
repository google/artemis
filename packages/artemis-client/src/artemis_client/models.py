# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Dependency-free data models for the Artemis remote API."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping

TERMINAL_TASK_STATUSES = frozenset(
    {"completed", "success", "failed", "cancelled", "canceled", "rejected"}
)
SUCCESS_TASK_STATUSES = frozenset({"completed", "success"})


def _string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _integer(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _device_from_payload(payload: Mapping[str, Any]) -> str | None:
    direct = _string(payload.get("device_serial") or payload.get("device_id"))
    if direct:
        return direct

    device_info = payload.get("device_info")
    if isinstance(device_info, str):
        try:
            device_info = json.loads(device_info)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    if isinstance(device_info, Mapping):
        return _string(device_info.get("device_serial") or device_info.get("device_id"))
    return None


@dataclass(frozen=True, slots=True)
class TaskHandle:
    """A task accepted by the remote Artemis scheduler."""

    task_id: str
    status: str
    device_serial: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def session_id(self) -> str:
        """Compatibility alias for servers that call a task a session."""
        return self.task_id

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> TaskHandle:
        task_id = _string(payload.get("task_id") or payload.get("session_id") or payload.get("id"))
        if not task_id:
            from artemis_client.errors import ProtocolError

            raise ProtocolError("Task admission response did not contain a task/session ID")
        return cls(
            task_id=task_id,
            status=(_string(payload.get("status")) or "queued").lower(),
            device_serial=_device_from_payload(payload),
            raw=dict(payload),
        )


@dataclass(frozen=True, slots=True)
class TaskResult:
    """Current or terminal state of a remote Artemis task."""

    task_id: str
    status: str
    goal: str | None = None
    profile: str | None = None
    device_serial: str | None = None
    output: Any = None
    error: str | None = None
    turns: int | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def session_id(self) -> str:
        return self.task_id

    @property
    def trace_id(self) -> str:
        return self.task_id

    @property
    def done(self) -> bool:
        return self.status in TERMINAL_TASK_STATUSES

    @property
    def succeeded(self) -> bool:
        return self.status in SUCCESS_TASK_STATUSES

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        task_id: str | None = None,
    ) -> TaskResult:
        resolved_id = _string(
            payload.get("task_id")
            or payload.get("session_id")
            or payload.get("trace_id")
            or payload.get("id")
            or task_id
        )
        if not resolved_id:
            from artemis_client.errors import ProtocolError

            raise ProtocolError("Task response did not contain a task/session ID")

        output = payload.get("output")
        if output is None:
            output = payload.get("result")
        if output is None:
            output = payload.get("summary")

        return cls(
            task_id=resolved_id,
            status=(_string(payload.get("status")) or "unknown").lower(),
            goal=_string(payload.get("goal") or payload.get("initial_goal")),
            profile=_string(payload.get("profile")),
            device_serial=_device_from_payload(payload),
            output=output,
            error=_string(payload.get("error") or payload.get("error_message")),
            turns=_integer(payload.get("turns") or payload.get("current_turn")),
            raw=dict(payload),
        )


@dataclass(frozen=True, slots=True)
class Device:
    """A device reported by the remote Artemis host."""

    serial: str
    state: str
    model: str | None = None
    product: str | None = None
    busy: bool = False
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Device:
        serial = _string(
            payload.get("serial") or payload.get("device_serial") or payload.get("device_id")
        )
        if not serial:
            from artemis_client.errors import ProtocolError

            raise ProtocolError("Device response did not contain a serial number")
        state = (_string(payload.get("state") or payload.get("status")) or "unknown").lower()
        busy_value = payload.get("busy")
        if busy_value is None:
            busy_value = payload.get("is_busy")
        return cls(
            serial=serial,
            state=state,
            model=_string(payload.get("model")),
            product=_string(payload.get("product")),
            busy=bool(busy_value) or state in {"busy", "running", "locked"},
            raw=dict(payload),
        )


@dataclass(frozen=True, slots=True)
class Capabilities:
    """Features advertised by an Artemis host."""

    api_version: str
    features: frozenset[str]
    server_version: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def supports(self, feature: str) -> bool:
        return feature in self.features

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Capabilities:
        raw_features = payload.get("features") or []
        if isinstance(raw_features, Mapping):
            features = frozenset(str(key) for key, enabled in raw_features.items() if enabled)
        elif isinstance(raw_features, (list, tuple, set, frozenset)):
            features = frozenset(str(item) for item in raw_features)
        else:
            features = frozenset()
        return cls(
            api_version=_string(payload.get("api_version")) or "unknown",
            server_version=_string(payload.get("server_version") or payload.get("version")),
            features=features,
            raw=dict(payload),
        )
