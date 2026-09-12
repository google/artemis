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
import os
from app.config import settings

logger = logging.getLogger("artemis.docker")

try:
    import docker
    from docker.errors import DockerException, NotFound

    DOCKER_AVAILABLE = True
except ImportError:
    DOCKER_AVAILABLE = False


class DockerManagerService:
    """Manages creation, monitoring, and deletion of ephemeral Artemis session containers via Docker Socket."""

    def __init__(self):
        self._client: docker.DockerClient | None = None
        if DOCKER_AVAILABLE:
            try:
                self._client = docker.DockerClient(base_url=settings.DOCKER_SOCKET_PATH)
                logger.info(
                    f"[Docker Service] Connected to Docker socket at {settings.DOCKER_SOCKET_PATH}"
                )
                self._ensure_network()
            except Exception as e:
                logger.warning(
                    f"[Docker Service] Could not connect to Docker socket: {e}. Running in simulation mode."
                )
                self._client = None
        else:
            logger.warning("[Docker Service] Docker SDK not available; running in simulation mode.")

    def _ensure_network(self) -> None:
        """Ensure the shared user-defined bridge network exists."""
        if not self._client:
            return
        try:
            self._client.networks.get(settings.DOCKER_NETWORK)
        except NotFound:
            logger.info(f"[Docker Service] Creating network {settings.DOCKER_NETWORK}")
            self._client.networks.create(settings.DOCKER_NETWORK, driver="bridge")
        except Exception as e:
            logger.warning(f"[Docker Service] Network check warning: {e}")

    async def start_artemis_container(
        self,
        session_id: str,
        cuttlefish_host: str,
        cuttlefish_adb_port: int,
    ) -> dict:
        """Spawn a dedicated Artemis session container connected to the target Cuttlefish emulator."""
        container_name = f"{settings.ARTEMIS_CONTAINER_PREFIX}-{session_id}"
        adb_target = f"{cuttlefish_host}:{cuttlefish_adb_port}"

        logger.info(
            f"[Docker Service] Launching container '{container_name}' targeting ADB device '{adb_target}'..."
        )

        if not self._client:
            # Simulated mode for environments without raw Docker socket access
            await asyncio.sleep(0.5)
            return {
                "container_id": f"mock-container-{session_id[:8]}",
                "container_name": container_name,
                "internal_ip": "172.20.0.5",
            }

        loop = asyncio.get_event_loop()

        def _run_container():
            # Check if an existing container with same name exists and remove it
            try:
                old = self._client.containers.get(container_name)
                old.remove(force=True)
            except NotFound:
                pass

            # Assemble container environment
            env_vars = {
                "SESSION_ID": session_id,
                "ADB_DEVICE_SERIAL": adb_target,
                "PORT": "8080",
                "PYTHONUNBUFFERED": "1",
            }
            # Automatically forward LLM credentials configured on backend
            for key in (
                "GEMINI_API_KEY",
                "GOOGLE_API_KEY",
                "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY",
                "OPEN_ROUTER_API_KEY",
                "XAI_API_KEY",
                "VISION_API_KEY",
                "OPENAI_BASE_URL",
            ):
                val = os.environ.get(key)
                if val:
                    env_vars[key] = val

            # Launch Artemis container
            container = self._client.containers.run(
                image=settings.ARTEMIS_IMAGE,
                name=container_name,
                detach=True,
                network=settings.DOCKER_NETWORK,
                extra_hosts={"host.docker.internal": "host-gateway"},
                environment=env_vars,
                labels={
                    "artemis.managed": "true",
                    "artemis.session_id": session_id,
                },
                restart_policy={"Name": "on-failure", "MaximumRetryCount": 3},
            )

            # Reload to get network IP
            container.reload()
            networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
            internal_ip = networks.get(settings.DOCKER_NETWORK, {}).get("IPAddress", "")

            # Execute adb connect inside the container to establish connection with Cuttlefish
            try:
                connect_cmd = f"adb connect {adb_target}"
                logger.info(f"[Docker Service] Executing '{connect_cmd}' in {container_name}...")
                container.exec_run(connect_cmd)
            except Exception as e:
                logger.warning(f"[Docker Service] adb connect command notice: {e}")

            return {
                "container_id": container.id,
                "container_name": container_name,
                "internal_ip": internal_ip,
            }

        result = await loop.run_in_executor(None, _run_container)
        logger.info(f"[Docker Service] Container {container_name} successfully booted.")
        return result

    async def stop_artemis_container(self, session_id: str) -> None:
        """Stop and remove an Artemis session container."""
        container_name = f"{settings.ARTEMIS_CONTAINER_PREFIX}-{session_id}"
        logger.info(f"[Docker Service] Stopping and removing container '{container_name}'...")

        if not self._client:
            return

        loop = asyncio.get_event_loop()

        def _remove_container():
            try:
                container = self._client.containers.get(container_name)
                container.stop(timeout=5)
                container.remove(force=True)
                logger.info(f"[Docker Service] Container {container_name} removed.")
            except NotFound:
                logger.info(f"[Docker Service] Container {container_name} was already removed.")
            except Exception as e:
                logger.error(f"[Docker Service] Error removing container {container_name}: {e}")

        await loop.run_in_executor(None, _remove_container)


docker_manager = DockerManagerService()
