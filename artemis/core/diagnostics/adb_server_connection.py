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

"""Process-wide ADB server endpoint discovery and activation."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Callable

from dotenv import set_key

from artemis.config import settings
from artemis.config.paths import ROOT_DIR, get_app_dir
from artemis.runtime.adb_endpoint import (
    AdbEndpoint,
    AdbSession,
    InvalidAdbEndpoint,
    current_adb_endpoint,
)
from artemis.toolchain import toolchain
from artemis.utils.logger import get_logger

logger = get_logger(__name__)

AdbServerEndpoint = AdbEndpoint
InvalidAdbServerEndpoint = InvalidAdbEndpoint


class AdbServerConnectionManager:
    """Validate, probe, activate, and persist the selected ADB server endpoint."""

    def __init__(
        self,
        *,
        adb_resolver: Callable[[], str | None] | None = None,
        env_files: list[Path] | None = None,
    ) -> None:
        self._adb_resolver = adb_resolver or (lambda: toolchain.resolve("adb"))
        self._env_files = env_files
        self._activation_lock = asyncio.Lock()

    @staticmethod
    def validate_endpoint(host: str, port: int) -> AdbServerEndpoint:
        return AdbEndpoint.create(host, port)

    def current_endpoint(self) -> AdbServerEndpoint:
        return current_adb_endpoint()

    def status(self) -> dict[str, Any]:
        return {"endpoint": self.current_endpoint().to_dict()}

    def synchronize_environment(self) -> AdbServerEndpoint:
        """Align subprocess and Python ADB clients with the configured endpoint."""
        socket_endpoint = self._endpoint_from_socket(os.environ.get("ADB_SERVER_SOCKET"))
        has_explicit_host = bool(os.environ.get("ADB_HOST") or os.environ.get("ADB_PORT"))
        endpoint = (
            socket_endpoint
            if socket_endpoint is not None and not has_explicit_host
            else self.current_endpoint()
        )
        self._activate(endpoint)
        return endpoint

    async def connect(
        self,
        host: str,
        port: int,
        *,
        persist: bool = True,
    ) -> dict[str, Any]:
        """Probe an endpoint, then make it the process-wide ADB server on success."""
        endpoint = self.validate_endpoint(host, port)
        probe_result = await self.probe(host, port)
        if not probe_result["success"]:
            return probe_result

        persisted = False
        persistence_error: str | None = None
        async with self._activation_lock:
            self._activate(endpoint)
            if persist:
                try:
                    await asyncio.to_thread(self._persist, endpoint)
                    persisted = True
                except Exception as exc:
                    persistence_error = str(exc)
                    logger.warning(f"Unable to persist ADB server endpoint: {exc}")

        message = probe_result["message"]
        if persist and not persisted:
            message = (
                f"{message} The connection is active for this session, but Artemis could not "
                "save it for the next launch."
            )

        return {
            **probe_result,
            "endpoint": endpoint.to_dict(),
            "message": message,
            "persisted": persisted,
            "persistence_error": persistence_error,
        }

    async def probe(self, host: str, port: int) -> dict[str, Any]:
        """Test an endpoint without changing the process preference."""
        endpoint = self.validate_endpoint(host, port)
        return await asyncio.to_thread(self._probe_sync, endpoint)

    async def use_local_server(self, *, persist: bool = True) -> dict[str, Any]:
        """Restore the standard local ADB server without killing any remote daemon."""
        endpoint = AdbEndpoint.local()
        persisted = False
        persistence_error: str | None = None
        async with self._activation_lock:
            self._activate(endpoint)
            if persist:
                try:
                    await asyncio.to_thread(self._persist, endpoint)
                    persisted = True
                except Exception as exc:
                    persistence_error = str(exc)
                    logger.warning(f"Unable to persist local ADB server endpoint: {exc}")

        message = "Artemis is now using the local ADB server."
        if persist and not persisted:
            message += " The setting could not be saved for the next launch."
        return {
            "success": True,
            "endpoint": endpoint.to_dict(),
            "persisted": persisted,
            "persistence_error": persistence_error,
            "message": message,
        }

    def _probe_sync(self, endpoint: AdbServerEndpoint) -> dict[str, Any]:
        adb_path = self._adb_resolver()
        if not adb_path:
            return {
                "success": False,
                "error_code": "adb_not_found",
                "message": "Android Platform Tools (adb) is not installed or not on PATH.",
                "endpoint": endpoint.to_dict(),
                "devices": [],
            }

        session = AdbSession(endpoint, adb_path=adb_path)
        command = session.command(["devices", "-l"])
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
                env=self._probe_environment(),
            )
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error_code": "timeout",
                "message": (
                    "The ADB server did not respond. Check that the host and port are correct "
                    "and accessible from this computer."
                ),
                "endpoint": endpoint.to_dict(),
                "devices": [],
            }
        except OSError as exc:
            logger.warning(f"Failed to execute ADB endpoint probe: {exc}")
            return {
                "success": False,
                "error_code": "adb_execution_failed",
                "message": f"Unable to run adb: {exc}",
                "endpoint": endpoint.to_dict(),
                "devices": [],
            }

        stdout = result.stdout or ""
        stderr = result.stderr or ""
        output = "\n".join(part.strip() for part in (stdout, stderr) if part.strip())
        devices = self._parse_devices(stdout)
        if result.returncode != 0:
            return {
                "success": False,
                "error_code": "server_unreachable",
                "message": self._connection_error_message(output),
                "output": output,
                "endpoint": endpoint.to_dict(),
                "devices": [],
            }

        ready_devices = [device for device in devices if device["state"] == "device"]
        if ready_devices:
            message = (
                f"Connected to {endpoint.host}:{endpoint.port} and found "
                f"{len(ready_devices)} ready device{'s' if len(ready_devices) != 1 else ''}."
            )
        elif devices:
            message = (
                "The ADB server is reachable, but its devices require attention. "
                "Unlock the phone and approve USB debugging if prompted."
            )
        else:
            message = (
                "The ADB server is reachable, but no devices are attached to the remote computer."
            )

        return {
            "success": True,
            "message": message,
            "output": output,
            "endpoint": endpoint.to_dict(),
            "devices": devices,
        }

    @staticmethod
    def _parse_devices(output: str) -> list[dict[str, str | None]]:
        devices: list[dict[str, str | None]] = []
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if not line or line.lower().startswith("list of devices") or line.startswith("*"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            metadata = {
                key: value.replace("_", " ")
                for token in parts[2:]
                if ":" in token
                for key, value in [token.split(":", 1)]
            }
            devices.append(
                {
                    "serial": parts[0],
                    "state": parts[1],
                    "model": metadata.get("model"),
                    "product": metadata.get("product"),
                }
            )
        return devices

    @staticmethod
    def _connection_error_message(output: str) -> str:
        lowered = output.lower()
        if "cannot connect" in lowered or "failed to connect" in lowered:
            return (
                "No ADB server is listening at this address. Verify that the host and port are "
                "correct and accessible from this computer."
            )
        if "version" in lowered and "mismatch" in lowered:
            return (
                "The local and remote ADB versions are incompatible. Use matching Android "
                "Platform Tools versions on both computers."
            )
        return output or "Unable to reach the ADB server endpoint."

    @staticmethod
    def _probe_environment() -> dict[str, str]:
        environment = os.environ.copy()
        environment.pop("ADB_SERVER_SOCKET", None)
        return environment

    @classmethod
    def _endpoint_from_socket(cls, value: str | None) -> AdbServerEndpoint | None:
        if not value or not value.startswith("tcp:"):
            return None
        address = value.removeprefix("tcp:")
        if address.startswith("["):
            match = re.fullmatch(r"\[([^]]+)]:(\d+)", address)
        else:
            match = re.fullmatch(r"(.+):(\d+)", address)
        if match is None:
            return None
        try:
            return cls.validate_endpoint(match.group(1), int(match.group(2)))
        except (InvalidAdbServerEndpoint, ValueError):
            return None

    @staticmethod
    def _activate(endpoint: AdbServerEndpoint) -> None:
        settings.ADB_HOST = endpoint.host
        settings.ADB_PORT = endpoint.port
        endpoint.apply_to_environment()
        logger.info(
            f"Activated {endpoint.mode} ADB server endpoint {endpoint.host}:{endpoint.port}"
        )

    def _persist(self, endpoint: AdbServerEndpoint) -> None:
        for env_file in self._environment_files():
            env_file.parent.mkdir(parents=True, exist_ok=True)
            if not env_file.exists():
                env_file.touch()
            set_key(str(env_file), "ADB_HOST", endpoint.host, quote_mode="never")
            set_key(str(env_file), "ADB_PORT", str(endpoint.port), quote_mode="never")

    def _environment_files(self) -> list[Path]:
        if self._env_files is not None:
            candidates = self._env_files
        else:
            candidates = [ROOT_DIR / ".env", get_app_dir() / ".env"]

        unique_files: list[Path] = []
        seen: set[Path] = set()
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved not in seen:
                seen.add(resolved)
                unique_files.append(candidate)
        return unique_files


adb_server_connection = AdbServerConnectionManager()
adb_server_connection.synchronize_environment()
