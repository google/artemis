# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Cross-platform task-target primitives and platform vocabulary.

The selected endpoint is a user preference; an :class:`AdbTarget` or
:class:`IosTarget` is an immutable execution snapshot. Keeping those concepts
separate prevents a queued or running task from silently moving to another
ADB server or simulator when the preference changes in the Admin Console.

ADB-server addressing itself lives in :mod:`artemis.runtime.adb_endpoint`;
this module sits above it and fans out per platform.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, MutableMapping

from artemis.runtime.adb_endpoint import (
    ADB_ENDPOINT_ID_ENV,
    AdbEndpoint,
    current_adb_endpoint,
)


# User-facing platform vocabulary shared by CLI, console, daemon, and MCP.
SUPPORTED_PLATFORMS: tuple[str, ...] = ("android", "ios")
DEFAULT_PLATFORM = "android"


def normalize_device_platform(
    value: object,
    *,
    default: str = DEFAULT_PLATFORM,
    strict: bool = True,
) -> str:
    """Canonicalize a platform token to ``"android"`` or ``"ios"``.

    Strips whitespace and case-folds. ``strict=True`` (entry points) raises
    ``ValueError`` on anything else; ``strict=False`` (fail-open internals)
    falls back to *default*. Surfaces adapt the ``ValueError`` to their own
    error type (HTTP 400, Typer error, blocked verdict, ...).
    """
    text = str(value).strip().lower() if value is not None else ""
    if not text:
        return default
    if text in SUPPORTED_PLATFORMS:
        return text
    if strict:
        raise ValueError(f"Unsupported platform '{value}'. Expected 'android' or 'ios'.")
    return default


def device_pool_for(platform: object):
    """The device pool for a platform token (lazy imports avoid a cycle).

    Both pools expose the same admission surface (``validate_explicit_serial``,
    ``validate_explicit_serial_async``, ``select_device_async``,
    ``list_devices_async``).
    """
    if normalize_device_platform(platform, strict=False) == "ios":
        from artemis.runtime.ios_device_pool import ios_device_pool

        return ios_device_pool
    from artemis.runtime.device_pool import device_pool

    return device_pool


def target_for_platform(
    platform: object,
    serial: str | None = None,
    endpoint: AdbEndpoint | None = None,
) -> AdbTarget | IosTarget:
    """Build the execution target for a platform; ``lock_scope`` comes with it."""
    if normalize_device_platform(platform, strict=False) == "ios":
        return IosTarget(serial=serial)
    return AdbTarget(endpoint=endpoint or current_adb_endpoint(), serial=serial)


@dataclass(frozen=True, slots=True)
class AdbTarget:
    """A device serial bound to the ADB endpoint that discovered it."""

    endpoint: AdbEndpoint
    serial: str | None = None

    @property
    def platform(self) -> str:
        return "android"

    @property
    def lock_scope(self) -> str:
        return self.endpoint.identity

    @property
    def lock_key(self) -> str:
        return f"{self.lock_scope}/{self.serial or 'pending'}"

    def apply_to_environment(
        self,
        environment: MutableMapping[str, str] | None = None,
    ) -> MutableMapping[str, str]:
        return self.endpoint.apply_to_environment(environment)

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "endpoint": self.endpoint.to_dict(),
            "serial": self.serial,
        }


# Execution lock scope shared by every iOS device task. The device UDID
# distinguishes devices inside the scope; "ios" keeps an iOS lock file
# namespaced away from any Android serial of the same text.
IOS_LOCK_SCOPE = "ios"


@dataclass(frozen=True, slots=True)
class IosTarget:
    """An iOS device UDID (simulator or paired physical device)."""

    serial: str | None = None

    @property
    def platform(self) -> str:
        return "ios"

    @property
    def lock_scope(self) -> str:
        return IOS_LOCK_SCOPE

    @property
    def lock_key(self) -> str:
        return f"{self.lock_scope}/{self.serial or 'pending'}"

    def apply_to_environment(
        self,
        environment: MutableMapping[str, str] | None = None,
    ) -> MutableMapping[str, str]:
        target = environment if environment is not None else os.environ
        # ADB_ENDPOINT_ID_ENV names the execution scope generically: the ADB
        # endpoint identity for Android, the platform tag for iOS devices.
        target[ADB_ENDPOINT_ID_ENV] = self.lock_scope
        # An iOS worker never touches ADB: a stale serial must not leak in.
        target.pop("ADB_DEVICE_SERIAL", None)
        # The worker's device id comes from the queue target, not a parent env.
        target.pop("ARTEMIS_DEVICE_ID", None)
        return target

    def to_dict(self) -> dict[str, Any]:
        return {"platform": self.platform, "serial": self.serial}
