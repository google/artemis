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

import time
from typing import Any

try:
    from admin_console.database.connection import db_session
except ImportError:
    from apps.admin_console.database.connection import db_session


class SessionRepository:
    """Repository handling all database queries and updates for Sessions."""

    def __init__(self, db_path=None):
        self.db_path = db_path

    def get_all_sessions(self) -> list[dict[str, Any]]:
        with db_session(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM sessions ORDER BY start_time DESC")
            return [dict(r) for r in cursor.fetchall()]

    def get_session_by_id(self, session_id: str) -> dict[str, Any] | None:
        with db_session(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM sessions WHERE session_id = ?", (str(session_id),))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_video_recordings_map(self) -> dict[str, str]:
        with db_session(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='video_recordings'"
            )
            if cursor.fetchone() is None:
                return {}
            cursor.execute(
                "SELECT session_id, local_video_path FROM video_recordings WHERE session_id IS NOT NULL AND local_video_path IS NOT NULL ORDER BY start_time ASC"
            )
            return {str(r[0]): str(r[1]) for r in cursor.fetchall() if r[0] and r[1]}

    def get_llm_traces_for_profile(self, session_id: str, limit: int = 3) -> list[str]:
        with db_session(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT payload FROM traces WHERE session_id = ? AND type = 'llm_call' LIMIT ?",
                (session_id, limit),
            )
            rows = cursor.fetchall()
            return [r[0] for r in rows if r and r[0]]

    def get_agent_trace_names(self, session_id: str) -> list[str]:
        try:
            with db_session(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT DISTINCT name FROM traces WHERE session_id = ? AND type IN ('agent', 'llm_call')",
                    (session_id,),
                )
                rows = cursor.fetchall()
                return [r[0] for r in rows if r and r[0]]
        except Exception:
            return []

    def get_session_by_id(self, session_id: str) -> dict[str, Any] | None:
        try:
            with db_session(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM sessions WHERE session_id = ?",
                    (str(session_id),),
                )
                row = cursor.fetchone()
                return dict(row) if row else None
        except Exception:
            return None

    def harvest_orphaned_sessions(self, orphaned_ids: list[str]) -> int:
        if not orphaned_ids:
            return 0
        with db_session(self.db_path) as conn:
            cursor = conn.cursor()
            now = time.time()
            for s_id in orphaned_ids:
                cursor.execute(
                    "UPDATE sessions SET status = ?, end_time = ? "
                    "WHERE session_id = ? AND status = ?",
                    ("failed", now, s_id, "running"),
                )
            conn.commit()
            return len(orphaned_ids)

    def cleanup_orphans_on_startup(self) -> int:
        try:
            with db_session(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE sessions SET status = ?, end_time = ? WHERE status = ?",
                    ("failed", time.time(), "running"),
                )
                count = cursor.rowcount
                conn.commit()
                return count
        except Exception:
            return 0

    def get_latest_session(self) -> dict[str, Any] | None:
        try:
            with db_session(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT session_id, status FROM sessions ORDER BY start_time DESC LIMIT 1"
                )
                row = cursor.fetchone()
                return dict(row) if row else None
        except Exception:
            return None

    def get_running_session_id(self) -> str | None:
        try:
            with db_session(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT session_id FROM sessions WHERE status = 'running' "
                    "ORDER BY start_time DESC LIMIT 1"
                )
                row = cursor.fetchone()
                return row["session_id"] if row else None
        except Exception:
            return None

    def get_session_status(self, session_id: str) -> str | None:
        try:
            with db_session(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT status FROM sessions WHERE session_id = ?",
                    (str(session_id),),
                )
                row = cursor.fetchone()
                return row["status"] if row else None
        except Exception:
            return None

    def update_session_status(
        self, session_id: str, status: str, end_time: float | None = None
    ) -> bool:
        try:
            with db_session(self.db_path) as conn:
                cursor = conn.cursor()
                t = end_time or time.time()
                cursor.execute(
                    "UPDATE sessions SET status = ?, end_time = ? WHERE session_id = ?",
                    (status, t, str(session_id)),
                )
                conn.commit()
                return cursor.rowcount > 0
        except Exception:
            return False

    def mark_all_running_cancelled(self) -> int:
        try:
            with db_session(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE sessions SET status = ?, end_time = ? WHERE status = ?",
                    ("cancelled", time.time(), "running"),
                )
                count = cursor.rowcount
                conn.commit()
                return count
        except Exception:
            return 0

    def get_background_tasks(self, session_id: str) -> list[dict[str, Any]]:
        try:
            with db_session(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT * FROM background_tasks WHERE session_id = ? ORDER BY start_time ASC",
                    (session_id,),
                )
                return [dict(r) for r in cursor.fetchall()]
        except Exception:
            return []


session_repo = SessionRepository()
