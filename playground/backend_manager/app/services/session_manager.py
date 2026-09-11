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
from datetime import datetime, timedelta, timezone
import logging
import uuid
from app.config import settings
from app.schemas.session_schema import (
    CreateSessionRequest,
    HeartbeatResponse,
    SessionListItem,
    SessionRecord,
    SessionResponse,
    SessionStatus,
)
from app.services.bigquery_service import bigquery_service
from app.services.cloud_orchestrator_service import cloud_orchestrator_service
from app.services.docker_service import docker_manager

logger = logging.getLogger("artemis.session_manager")


class SessionManagerService:
    """Master orchestrator for multi-tenant Artemis session containers and Cuttlefish AVDs."""

    def __init__(self):
        self._reaper_task: asyncio.Task | None = None

    def start_background_reaper(self):
        """Start the background watchdog task to reap expired sessions."""
        if not self._reaper_task or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reaper_loop())
            logger.info("[Session Manager] Background session reaper loop started.")

    async def _reaper_loop(self):
        """Periodically scans for expired sessions and reclaims compute resources."""
        while True:
            try:
                await asyncio.sleep(settings.REAPER_CHECK_INTERVAL_SECONDS)
                now = datetime.now(timezone.utc)
                active_sessions = await bigquery_service.list_all_active_sessions()

                for s in active_sessions:
                    # Check if session has exceeded expiration or missed heartbeats
                    if s.expires_at < now:
                        logger.info(f"[Session Reaper] Session {s.session_id} has expired (TTL elapsed). Tearing down...")
                        await self.terminate_session(s.session_id, user_id=s.user_id, reason="TTL expired")
                    elif (now - s.last_heartbeat_at).total_seconds() > settings.SESSION_HEARTBEAT_TIMEOUT_SECONDS:
                        logger.info(f"[Session Reaper] Session {s.session_id} heartbeat timed out. Tearing down...")
                        await self.terminate_session(s.session_id, user_id=s.user_id, reason="Heartbeat timeout")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Session Reaper] Error in reaper loop: {e}")

    async def create_session(self, user_id: str, request: CreateSessionRequest) -> SessionResponse:
        """Provision a new Cuttlefish emulator + Artemis container and link them via ADB."""
        session_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(minutes=request.ttl_minutes)

        logger.info(f"[Session Manager] Starting session creation for user={user_id}, session_id={session_id}...")

        # Initialize record with PROVISIONING status
        record = SessionRecord(
            user_id=user_id,
            session_id=session_id,
            status=SessionStatus.PROVISIONING,
            created_at=now,
            last_heartbeat_at=now,
            expires_at=expires_at,
        )
        await bigquery_service.save_session(record)

        try:
            # 1. Start Cuttlefish AVD via Cloud Orchestrator
            cf_info = await cloud_orchestrator_service.create_cuttlefish_instance(
                session_id=session_id,
                preset=request.device_preset,
            )
            record.cuttlefish_instance_id = cf_info.instance_id
            record.cuttlefish_adb_port = cf_info.adb_port
            record.cuttlefish_webrtc_port = cf_info.webrtc_port

            # 2. Start Artemis Container via Docker Socket
            docker_info = await docker_manager.start_artemis_container(
                session_id=session_id,
                cuttlefish_host=cf_info.host,
                cuttlefish_adb_port=cf_info.adb_port,
            )
            record.artemis_container_id = docker_info["container_id"]
            record.artemis_container_name = docker_info["container_name"]
            record.artemis_internal_ip = docker_info["internal_ip"]
            record.status = SessionStatus.ACTIVE

            # 3. Save finalized state to BigQuery
            await bigquery_service.save_session(record)
            logger.info(f"[Session Manager] Session {session_id} is ACTIVE and ready.")

            return self._build_session_response(record)

        except Exception as e:
            logger.error(f"[Session Manager] Failed to create session {session_id}: {e}")
            record.status = SessionStatus.ERROR
            record.error_message = str(e)
            await bigquery_service.save_session(record)
            # Clean up partial resources
            await self._cleanup_resources(session_id)
            raise e

    async def get_session(self, session_id: str, user_id: str) -> SessionResponse | None:
        """Fetch session information, ensuring tenant ownership."""
        record = await bigquery_service.get_session(session_id)
        if not record:
            return None
        if record.user_id != user_id:
            logger.warning(f"[Session Manager] Tenant mismatch: user {user_id} attempted access to session {session_id}")
            return None
        return self._build_session_response(record)

    async def list_user_sessions(self, user_id: str) -> list[SessionListItem]:
        """List all active sessions for the current user."""
        records = await bigquery_service.list_user_sessions(user_id)
        return [
            SessionListItem(
                session_id=r.session_id,
                status=r.status,
                created_at=r.created_at,
                expires_at=r.expires_at,
                artemis_container_id=r.artemis_container_id,
            )
            for r in records
        ]

    async def heartbeat(self, session_id: str, user_id: str) -> HeartbeatResponse | None:
        """Extend session TTL and record client liveness."""
        record = await bigquery_service.get_session(session_id)
        if not record or record.user_id != user_id or record.status == SessionStatus.TERMINATED:
            return None

        now = datetime.now(timezone.utc)
        record.last_heartbeat_at = now
        record.expires_at = now + timedelta(minutes=settings.SESSION_TTL_MINUTES)
        await bigquery_service.save_session(record)

        return HeartbeatResponse(
            session_id=session_id,
            status=record.status,
            last_heartbeat_at=record.last_heartbeat_at,
            expires_at=record.expires_at,
        )

    async def terminate_session(self, session_id: str, user_id: str | None = None, reason: str = "User requested") -> bool:
        """Gracefully terminate container, emulator, and update records."""
        record = await bigquery_service.get_session(session_id)
        if not record:
            return False
        if user_id and record.user_id != user_id:
            return False

        logger.info(f"[Session Manager] Terminating session {session_id} (Reason: {reason})...")
        record.status = SessionStatus.TERMINATING
        await bigquery_service.save_session(record)

        await self._cleanup_resources(session_id)

        record.status = SessionStatus.TERMINATED
        await bigquery_service.save_session(record)
        logger.info(f"[Session Manager] Session {session_id} successfully terminated.")
        return True

    async def _cleanup_resources(self, session_id: str):
        """Clean up Docker container and Cloud Orchestrator instance."""
        await asyncio.gather(
            docker_manager.stop_artemis_container(session_id),
            cloud_orchestrator_service.destroy_cuttlefish_instance(session_id),
            return_exceptions=True,
        )

    def _build_session_response(self, record: SessionRecord) -> SessionResponse:
        """Construct the client response containing the Nginx-compatible proxy URLs."""
        return SessionResponse(
            session_id=record.session_id,
            status=record.status,
            # Routes through Nginx reverse proxy
            artemis_api_url=f"/session/{record.session_id}/artemis/",
            artemis_stream_url=f"/session/{record.session_id}/artemis/api/stream/active",
            cuttlefish_webrtc_url=f"/session/{record.session_id}/cuttlefish/",
            cuttlefish_adb_endpoint=f"{settings.CUTTLEFISH_HOST_GATEWAY}:{record.cuttlefish_adb_port}",
            created_at=record.created_at,
            expires_at=record.expires_at,
        )


session_manager = SessionManagerService()
