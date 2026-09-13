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

from datetime import datetime, timezone
import logging
from typing import Optional
from app.config import settings
from app.schemas.session_schema import SessionRecord, SessionStatus

logger = logging.getLogger("artemis.bigquery")

try:
    from google.cloud import bigquery
    from google.cloud.exceptions import NotFound

    BQ_AVAILABLE = True
except ImportError:
    BQ_AVAILABLE = False


class BigQueryMappingService:
    """Manages session metadata and user-to-container mapping records in BigQuery."""

    def __init__(self):
        self._local_cache: dict[str, SessionRecord] = {}
        self._client: Optional["bigquery.Client"] = None
        self._table_ref: str = (
            f"{settings.GCP_PROJECT_ID}.{settings.BQ_DATASET}.{settings.BQ_TABLE}"
        )

        if BQ_AVAILABLE and not settings.USE_LOCAL_FALLBACK_DB:
            try:
                self._client = bigquery.Client(project=settings.GCP_PROJECT_ID)
                self._ensure_dataset_and_table()
                logger.info(f"[BigQuery] Connected to BigQuery table: {self._table_ref}")
            except Exception as e:
                logger.warning(
                    f"[BigQuery] Failed to initialize BigQuery client: {e}. Falling back to in-memory store."
                )
                self._client = None
        else:
            logger.info("[BigQuery] Using in-memory fallback database for local/test execution.")

    def _ensure_dataset_and_table(self):
        """Ensure the destination dataset and partitioned table exist in BigQuery."""
        if not self._client:
            return
        try:
            dataset_ref = bigquery.DatasetReference(settings.GCP_PROJECT_ID, settings.BQ_DATASET)
            self._client.get_dataset(dataset_ref)
        except NotFound:
            dataset = bigquery.Dataset(dataset_ref)
            dataset.location = "US"
            self._client.create_dataset(dataset, timeout=30)
            logger.info(f"[BigQuery] Created dataset {settings.BQ_DATASET}")

        schema = [
            bigquery.SchemaField("user_id", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("session_id", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("artemis_container_id", "STRING", mode="NULLABLE"),
            bigquery.SchemaField("artemis_container_name", "STRING", mode="NULLABLE"),
            bigquery.SchemaField("artemis_internal_ip", "STRING", mode="NULLABLE"),
            bigquery.SchemaField("cuttlefish_instance_id", "STRING", mode="NULLABLE"),
            bigquery.SchemaField("cuttlefish_adb_port", "INTEGER", mode="NULLABLE"),
            bigquery.SchemaField("cuttlefish_webrtc_port", "INTEGER", mode="NULLABLE"),
            bigquery.SchemaField("status", "STRING", mode="REQUIRED"),
            bigquery.SchemaField("created_at", "TIMESTAMP", mode="REQUIRED"),
            bigquery.SchemaField("last_heartbeat_at", "TIMESTAMP", mode="REQUIRED"),
            bigquery.SchemaField("expires_at", "TIMESTAMP", mode="REQUIRED"),
            bigquery.SchemaField("error_message", "STRING", mode="NULLABLE"),
        ]

        table = bigquery.Table(self._table_ref, schema=schema)
        table.time_partitioning = bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY,
            field="created_at",
        )
        try:
            self._client.get_table(self._table_ref)
        except NotFound:
            self._client.create_table(table)
            logger.info(f"[BigQuery] Created partitioned table {self._table_ref}")

    async def save_session(self, record: SessionRecord) -> None:
        """Upsert a session record in the cache and BigQuery."""
        self._local_cache[record.session_id] = record

        if self._client:
            try:
                row = {
                    "user_id": record.user_id,
                    "session_id": record.session_id,
                    "artemis_container_id": record.artemis_container_id,
                    "artemis_container_name": record.artemis_container_name,
                    "artemis_internal_ip": record.artemis_internal_ip,
                    "cuttlefish_instance_id": record.cuttlefish_instance_id,
                    "cuttlefish_adb_port": record.cuttlefish_adb_port,
                    "cuttlefish_webrtc_port": record.cuttlefish_webrtc_port,
                    "status": record.status.value,
                    "created_at": record.created_at.isoformat(),
                    "last_heartbeat_at": record.last_heartbeat_at.isoformat(),
                    "expires_at": record.expires_at.isoformat(),
                    "error_message": record.error_message,
                }
                errors = self._client.insert_rows_json(self._table_ref, [row])
                if errors:
                    logger.error(
                        f"[BigQuery] Insert errors for session {record.session_id}: {errors}"
                    )
            except Exception as e:
                logger.error(f"[BigQuery] Streaming insert failed: {e}")

    async def get_session(self, session_id: str) -> SessionRecord | None:
        """Retrieve a session record by ID."""
        return self._local_cache.get(session_id)

    async def list_user_sessions(self, user_id: str) -> list[SessionRecord]:
        """List active/valid sessions belonging to a specific user."""
        return [
            rec
            for rec in self._local_cache.values()
            if rec.user_id == user_id and rec.status != SessionStatus.TERMINATED
        ]

    async def list_all_active_sessions(self) -> list[SessionRecord]:
        """List all non-terminated sessions for maintenance/reaping."""
        return [
            rec
            for rec in self._local_cache.values()
            if rec.status in (SessionStatus.PROVISIONING, SessionStatus.ACTIVE, SessionStatus.IDLE)
        ]

    async def update_status(
        self, session_id: str, status: SessionStatus, error_message: str | None = None
    ) -> None:
        """Update the status of an existing session record."""
        record = self._local_cache.get(session_id)
        if record:
            record.status = status
            record.error_message = error_message
            await self.save_session(record)


bigquery_service = BigQueryMappingService()
