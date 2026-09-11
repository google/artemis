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

import asyncio
import logging
from dataclasses import dataclass
import httpx
from app.config import settings

logger = logging.getLogger("artemis.orchestrator")


@dataclass
class CuttlefishInstanceInfo:
    instance_id: str
    adb_port: int
    webrtc_port: int
    host: str


class CloudOrchestratorService:
    """Manages dynamic discovery, pool allocation, on-demand creation, and binding of Cuttlefish instances."""

    def __init__(self):
        self._lock = asyncio.Lock()
        # session_id -> CuttlefishInstanceInfo
        self._allocated_instances: dict[str, CuttlefishInstanceInfo] = {}
        # emulator_id (e.g. 'cvd-1') -> session_id
        self._emulator_bindings: dict[str, str] = {}

    async def list_available_emulators(self) -> list[dict]:
        """Query Cloud Orchestrator for all running CVDs that are not currently bound to an active session."""
        bound_emulators = set(self._emulator_bindings.keys())

        async with httpx.AsyncClient(timeout=5.0) as client:
            try:
                resp = await client.get(f"{settings.CLOUD_ORCHESTRATOR_URL}/cvds")
                if resp.status_code != 200:
                    logger.error(f"[Cloud Orchestrator] Failed to fetch CVD list: HTTP {resp.status_code} ({resp.text})")
                    return []

                all_cvds = resp.json().get("cvds", [])
                available = []
                for cvd in all_cvds:
                    name = cvd.get("name") or cvd.get("webrtc_device_id")
                    status = cvd.get("status", "")
                    # Only pick emulators that are in 'Running' state and not bound to another session
                    if status.lower() == "running" and name not in bound_emulators:
                        available.append(cvd)

                logger.info(
                    f"[Cloud Orchestrator] CVD Status -> Total: {len(all_cvds)}, "
                    f"Available: {[c.get('name') for c in available]}, "
                    f"Bound: {list(self._emulator_bindings.items())}"
                )
                return available
            except Exception as e:
                logger.warning(f"[Cloud Orchestrator] Error listing CVDs from orchestrator: {e}")
                return []

    async def create_new_cuttlefish_emulator(self, instance_name: str, preset: str = "pixel_7_pro") -> dict:
        """Trigger creation of a new CVD via Host Orchestrator and poll until ready."""
        logger.info(f"[Cloud Orchestrator] Provisioning new Cuttlefish emulator '{instance_name}' via Host Orchestrator...")
        async with httpx.AsyncClient(timeout=10.0) as client:
            create_resp = await client.post(
                f"{settings.CLOUD_ORCHESTRATOR_URL}/cvds",
                json={
                    "env_config": {
                        "instances": [
                            {
                                "name": instance_name,
                                "disk": {
                                    "default_build": "@ab/aosp-android-latest-release/aosp_cf_x86_64_only_phone-userdebug"
                                },
                                "vm": {
                                    "cpus": 4,
                                    "memory_mb": 4096,
                                },
                            }
                        ]
                    }
                },
            )

            if create_resp.status_code not in (200, 201):
                raise RuntimeError(f"Host Orchestrator rejected CVD creation (HTTP {create_resp.status_code}): {create_resp.text}")

            op = create_resp.json()
            op_name = op.get("name")
            logger.info(f"[Cloud Orchestrator] Creation operation started: {op_name}. Waiting for device boot...")

            # Poll operation until done
            for attempt in range(60):  # Poll up to 120 seconds
                await asyncio.sleep(2.0)
                try:
                    op_resp = await client.get(f"{settings.CLOUD_ORCHESTRATOR_URL}/operations/{op_name}")
                    if op_resp.status_code == 200 and op_resp.json().get("done", False):
                        logger.info(f"[Cloud Orchestrator] Operation {op_name} successfully finished!")
                        break
                except Exception as e:
                    logger.debug(f"[Cloud Orchestrator] Polling operation {op_name}: {e}")

            # Now fetch the newly booted CVD from the orchestrator list
            list_resp = await client.get(f"{settings.CLOUD_ORCHESTRATOR_URL}/cvds")
            if list_resp.status_code == 200:
                for cvd in list_resp.json().get("cvds", []):
                    if cvd.get("name") == instance_name or instance_name in cvd.get("name", ""):
                        logger.info(f"[Cloud Orchestrator] Newly provisioned CVD found: {cvd}")
                        return cvd

            raise RuntimeError(f"CVD '{instance_name}' creation operation finished, but device was not found in running CVD list")

    async def create_cuttlefish_instance(self, session_id: str, preset: str = "pixel_7_pro") -> CuttlefishInstanceInfo:
        """
        Main entrypoint:
        1. Find an available running emulator not bound to any active Artemis session.
        2. If none available, provision a new emulator via Host Orchestrator.
        3. Dynamically extract the chosen emulator's ADB port (no static/default port assumptions).
        4. Bind the emulator to the Artemis session.
        """
        async with self._lock:
            # Check if this session already has an allocated instance
            if session_id in self._allocated_instances:
                return self._allocated_instances[session_id]

            # 1. Get emulator list that is not used by other artemis containers
            available = await self.list_available_emulators()

            chosen_cvd = None
            if available:
                chosen_cvd = available[0]
                logger.info(f"[Cloud Orchestrator] Reusing existing free emulator '{chosen_cvd.get('name')}' for session {session_id}.")
            else:
                # 2. If no emulator available, create one
                instance_name = f"cvd-{session_id[:8]}"
                chosen_cvd = await self.create_new_cuttlefish_emulator(instance_name, preset=preset)

            # 4. Extract dynamic ADB port from the chosen emulator
            emulator_name = chosen_cvd.get("name") or chosen_cvd.get("webrtc_device_id")
            adb_port = chosen_cvd.get("adb_port")

            # If adb_port key is absent or null, dynamically parse adb_serial (e.g. '127.0.0.1:6520')
            if not adb_port and "adb_serial" in chosen_cvd:
                try:
                    adb_port = int(chosen_cvd["adb_serial"].split(":")[-1])
                except (ValueError, IndexError):
                    pass

            if not adb_port:
                raise RuntimeError(f"Could not dynamically determine ADB port for emulator '{emulator_name}': {chosen_cvd}")

            adb_port = int(adb_port)
            logger.info(f"[Cloud Orchestrator] Dynamic ADB port for '{emulator_name}': {adb_port}")

            # 3. Bind the emulator to the Artemis session instance
            host = settings.CUTTLEFISH_HOST_GATEWAY
            info = CuttlefishInstanceInfo(
                instance_id=emulator_name,
                adb_port=adb_port,
                webrtc_port=settings.CUTTLEFISH_START_WEBRTC_PORT,
                host=host,
            )
            self._allocated_instances[session_id] = info
            self._emulator_bindings[emulator_name] = session_id

            logger.info(f"[Cloud Orchestrator] Successfully bound emulator '{emulator_name}' (port {adb_port}) to session {session_id}.")
            return info

    async def destroy_cuttlefish_instance(self, session_id: str) -> None:
        """Unbind emulator from session so it returns to the free pool for other Artemis containers."""
        async with self._lock:
            info = self._allocated_instances.pop(session_id, None)
            if not info:
                logger.info(f"[Cloud Orchestrator] No active binding found for session {session_id}")
                return

            self._emulator_bindings.pop(info.instance_id, None)
            logger.info(f"[Cloud Orchestrator] Unbound emulator '{info.instance_id}' from session {session_id}. Emulator returned to pool.")


cloud_orchestrator_service = CloudOrchestratorService()
