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

"""System Readiness & Diagnostic Orchestration Engine."""

import asyncio
import shutil
import subprocess
import time
from typing import Any

from artemis.core.diagnostics.probes.adb_probe import AdbDeviceProbe
from artemis.core.diagnostics.probes.base import BaseProbe
from artemis.core.diagnostics.probes.credentials_probe import (
    LLMCredentialsProbe,
    VisionOCRProbe,
)
from artemis.core.diagnostics.probes.runtime_probe import (
    PythonRuntimeProbe,
    SystemConfigProbe,
)
from artemis.core.diagnostics.probes.toolchain_probe import ToolchainProbe
from artemis.core.diagnostics.schema import (
    DeviceInfo,
    ProbeCategory,
    ProbeResult,
    ProbeStatus,
    SystemReadinessReport,
)
from artemis.platform import platform
from artemis.toolchain import toolchain
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


class ReadinessEngine:
    """Central orchestration engine executing modular readiness probes."""

    _REPORT_CACHE_TTL_SECONDS = 2.0

    def __init__(self):
        self._probes: dict[str, BaseProbe] = {}
        self._active_device_serial: str | None = None
        self._python_probe = PythonRuntimeProbe()
        self._config_probe = SystemConfigProbe()
        self._toolchain_probe = ToolchainProbe()
        self._credentials_probe = LLMCredentialsProbe()
        self._ocr_probe = VisionOCRProbe()
        self._adb_probe = AdbDeviceProbe()
        self._report_cache: SystemReadinessReport | None = None
        self._report_cache_time = 0.0
        self._report_cache_generation = -1
        self._cache_generation = 0
        self._report_lock = asyncio.Lock()

        # Register default core probes in logical lifecycle order
        self.register_probe(self._python_probe)
        self.register_probe(self._config_probe)
        self.register_probe(self._toolchain_probe)
        self.register_probe(self._credentials_probe)
        self.register_probe(self._ocr_probe)
        self.register_probe(self._adb_probe)

    def register_probe(self, probe: BaseProbe) -> None:
        """Register a new diagnostic probe."""
        self._probes[probe.probe_id] = probe

    def unregister_probe(self, probe_id: str) -> None:
        """Remove a diagnostic probe."""
        self._probes.pop(probe_id, None)

    def set_active_device_serial(self, serial: str | None) -> None:
        """Set user-selected active target device serial."""
        self._active_device_serial = serial
        self.invalidate_cache()
        if hasattr(self._adb_probe, "set_target_serial"):
            self._adb_probe.set_target_serial(serial)

    def get_active_device_serial(self) -> str | None:
        """Get currently selected active device serial."""
        return self._active_device_serial

    async def run_probe(self, probe_id: str) -> ProbeResult | None:
        """Execute a single specific probe by ID."""
        probe = self._probes.get(probe_id)
        if not probe:
            return None
        return await probe.probe()

    async def run_device_submission_probe(self) -> ProbeResult:
        """Run the bounded device gate used by task submission."""
        return await self._adb_probe.probe_submission_readiness()

    def invalidate_cache(self) -> None:
        """Invalidate the UI readiness snapshot after an explicit configuration change."""
        self._cache_generation += 1
        self._report_cache = None
        self._report_cache_time = 0.0
        self._report_cache_generation = -1

    def _cached_report(self, max_age_seconds: float) -> SystemReadinessReport | None:
        if self._report_cache is None:
            return None
        if self._report_cache_generation != self._cache_generation:
            return None
        if time.monotonic() - self._report_cache_time > max_age_seconds:
            return None
        return self._report_cache.model_copy(deep=True)

    async def run_all(
        self,
        categories: list[ProbeCategory] | None = None,
        *,
        force_refresh: bool = False,
    ) -> SystemReadinessReport:
        """Return a coalesced readiness snapshot.

        The dashboard polls frequently, so allowing every HTTP request to launch a
        complete toolchain and ADB scan creates a request storm. Full reports are
        cached briefly and all concurrent refreshes share one execution. The task
        submission gate remains independent and always performs its own bounded
        device check.
        """
        cacheable = categories is None
        request_started = time.monotonic()
        if cacheable and not force_refresh:
            cached = self._cached_report(self._REPORT_CACHE_TTL_SECONDS)
            if cached is not None:
                return cached

        async with self._report_lock:
            # A refresh that completed while this caller waited satisfies even a
            # forced request that began before it, coalescing concurrent clicks.
            if cacheable and self._report_cache_time >= request_started:
                cached = self._cached_report(float("inf"))
                if cached is not None:
                    return cached
            if cacheable and not force_refresh:
                cached = self._cached_report(self._REPORT_CACHE_TTL_SECONDS)
                if cached is not None:
                    return cached

            if force_refresh:
                toolchain.clear_cache()
                self._adb_probe.invalidate_enrichment_cache()

            build_generation = self._cache_generation
            report = await self._build_report(categories)
            # If a device/configuration change happened during the scan, return
            # this result only to its original caller and never publish it as the
            # shared snapshot for later requests.
            if cacheable and build_generation == self._cache_generation:
                self._report_cache = report.model_copy(deep=True)
                self._report_cache_time = time.monotonic()
                self._report_cache_generation = build_generation
            return report

    async def _build_report(
        self, categories: list[ProbeCategory] | None = None
    ) -> SystemReadinessReport:
        """Execute the underlying probes and compile an uncached report."""
        target_probes = [
            probe
            for probe in self._probes.values()
            if categories is None or probe.category in categories
        ]

        # Concurrently execute probes
        results: list[ProbeResult] = await asyncio.gather(
            *[probe.probe() for probe in target_probes],
            return_exceptions=False,
        )

        blockers = [r for r in results if r.is_blocker]
        passed_blockers = [r for r in blockers if r.status == ProbeStatus.PASS]
        overall_ready = len(blockers) > 0 and len(blockers) == len(passed_blockers)

        # Extract active device info from ADB probe metadata if available
        active_device: DeviceInfo | None = None
        adb_result = next((r for r in results if r.id == "android_adb"), None)
        if adb_result and adb_result.metadata.get("active_device"):
            try:
                active_device = DeviceInfo(**adb_result.metadata["active_device"])
            except Exception:
                pass

        return SystemReadinessReport(
            overall_ready=overall_ready,
            blocker_count=len(blockers),
            passed_blocker_count=len(passed_blockers),
            probes=results,
            active_device=active_device,
            os_type=platform.os_type.value,
            timestamp=time.time(),
        )

    async def heal_adb_keys(self, force: bool = False) -> dict[str, Any]:
        """Auto-heal corrupted or 0-byte ADB authentication keys."""
        from artemis.core.diagnostics.adb_keys import heal_adb_keys

        logger.info("[ReadinessEngine] Auto-healing ADB authentication keys...")
        return await asyncio.to_thread(heal_adb_keys, None, force)

    async def restart_adb_server(self) -> dict[str, Any]:
        """Execute adb kill-server && adb start-server to recover connectivity, auto-healing corrupted keys if needed."""
        from artemis.core.diagnostics.adb_keys import heal_adb_keys, inspect_adb_keys

        adb_path = shutil.which("adb") or "adb"

        def _restart_sync():
            # If keys are corrupted, heal them first
            key_status = inspect_adb_keys()
            if key_status.is_corrupted:
                logger.warning(
                    f"[ReadinessEngine] Corrupted ADB keys detected ({key_status.error_reason}). Auto-healing..."
                )
                return heal_adb_keys(adb_path=adb_path)

            try:
                subprocess.run(
                    [adb_path, "kill-server"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
                res = subprocess.run(
                    [adb_path, "start-server"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
                success = res.returncode == 0
                return {
                    "success": success,
                    "message": "ADB server restarted successfully"
                    if success
                    else "Failed to restart ADB",
                    "output": "",
                }
            except Exception as exc:
                return {
                    "success": False,
                    "message": f"Error executing ADB restart: {exc}",
                }

        logger.info("[ReadinessEngine] Restarting ADB server...")
        return await asyncio.to_thread(_restart_sync)

    async def launch_emulator(self, avd_name: str) -> dict[str, Any]:
        """Launch an Android emulator by AVD name in the background and track its lifecycle."""
        from artemis.core.diagnostics.emulator_manager import emulator_manager

        state = await emulator_manager.launch(avd_name)
        return state.model_dump()

    def get_emulator_status(self) -> dict[str, Any]:
        """Query current status of background emulator launch."""
        from artemis.core.diagnostics.emulator_manager import emulator_manager

        return emulator_manager.get_status().model_dump()

    async def stop_emulator(self) -> dict[str, Any]:
        """Stop current running emulator."""
        from artemis.core.diagnostics.emulator_manager import emulator_manager

        return await emulator_manager.stop()

    def dismiss_emulator(self) -> dict[str, Any]:
        """Dismiss current emulator launch state."""
        from artemis.core.diagnostics.emulator_manager import emulator_manager

        return emulator_manager.dismiss()

    async def connect_wireless_adb(self, host: str, port: int = 5555) -> dict[str, Any]:
        """Connect to an Android device over Wi-Fi via adb connect."""
        clean_host = host.strip()
        if not clean_host:
            return {"success": False, "message": "Host IP address cannot be empty"}
        target = f"{clean_host}:{port}"
        adb_path = shutil.which("adb") or "adb"

        def _connect_sync():
            try:
                res = subprocess.run(
                    [adb_path, "connect", target],
                    capture_output=True,
                    text=True,
                    timeout=8,
                )
                output = (res.stdout + "\n" + res.stderr).strip()
                success = "connected to" in output.lower() and "failed" not in output.lower()
                return {
                    "success": success,
                    "target": target,
                    "output": output,
                    "message": f"Connected to {target}" if success else output,
                }
            except Exception as exc:
                return {
                    "success": False,
                    "target": target,
                    "message": f"Connection error: {exc}",
                }

        logger.info(f"[ReadinessEngine] Connecting to wireless ADB target '{target}'...")
        return await asyncio.to_thread(_connect_sync)


# Global singleton instance
readiness_engine = ReadinessEngine()
