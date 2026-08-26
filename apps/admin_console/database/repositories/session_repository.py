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

    @staticmethod
    def process_is_alive(pid: Any) -> bool:
        """Return whether a session's worker PID is still alive."""
        try:
            import psutil

            process = psutil.Process(int(pid))
            return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
        except Exception:
            return False

    def get_all_sessions(self) -> list[dict[str, Any]]:
        with db_session(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM sessions ORDER BY start_time DESC")
            return [dict(r) for r in cursor.fetchall()]

    def get_video_recordings_map(self) -> dict[str, str]:
        with db_session(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='video_recordings'"
            )
            if cursor.fetchone() is None:
                return {}
            columns = {
                str(row[1]) for row in cursor.execute("PRAGMA table_info(video_recordings)")
            }
            ready_clause = "status = 'ready'" if "status" in columns else "end_time IS NOT NULL"
            cursor.execute(
                "SELECT session_id, local_video_path FROM video_recordings "
                "WHERE session_id IS NOT NULL AND local_video_path IS NOT NULL "
                f"AND {ready_clause} ORDER BY start_time ASC"
            )
            return {str(r[0]): str(r[1]) for r in cursor.fetchall() if r[0] and r[1]}

    def get_video_recording_for_session(self, session_id: str) -> dict[str, Any] | None:
        try:
            with db_session(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='video_recordings'"
                )
                if cursor.fetchone() is None:
                    return None
                cursor.execute(
                    "SELECT * FROM video_recordings WHERE session_id = ? "
                    "ORDER BY start_time DESC LIMIT 1",
                    (str(session_id),),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                result = dict(row)
                if "status" not in result:
                    result["status"] = "ready" if result.get("end_time") is not None else "recording"
                result.setdefault("error", None)
                return result
        except Exception:
            return None

    def get_latest_video_recordings_map(self) -> dict[str, dict[str, Any]]:
        """Return the newest recording row for every session in one query.

        The task-list endpoint renders every session at once. Calling
        ``get_video_recording_for_session`` in that loop used to open a new
        SQLite connection for every row, which made a refresh progressively
        slower as history grew.
        """
        try:
            with db_session(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='video_recordings'"
                )
                if cursor.fetchone() is None:
                    return {}

                rows = cursor.execute(
                    "SELECT * FROM video_recordings WHERE session_id IS NOT NULL "
                    "ORDER BY start_time DESC"
                ).fetchall()
                latest: dict[str, dict[str, Any]] = {}
                for row in rows:
                    recording = dict(row)
                    session_id = str(recording.get("session_id") or "")
                    if not session_id or session_id in latest:
                        continue
                    if "status" not in recording:
                        recording["status"] = (
                            "ready" if recording.get("end_time") is not None else "recording"
                        )
                    recording.setdefault("error", None)
                    latest[session_id] = recording
                return latest
        except Exception:
            return {}

    def mark_recording_failed_if_pending(self, session_id: str, error: str) -> bool:
        """Close a recording lifecycle when its worker exits before finalization."""
        try:
            with db_session(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE video_recordings SET status = 'failed', error = ?, "
                    "end_time = COALESCE(end_time, ?) "
                    "WHERE session_id = ? AND status IN ('recording', 'finalizing')",
                    (error, time.time(), str(session_id)),
                )
                conn.commit()
                return cursor.rowcount > 0
        except Exception:
            return False

    def get_llm_traces_for_profile(self, session_id: str, limit: int = 3) -> list[str]:
        with db_session(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT payload FROM traces WHERE session_id = ? AND type = 'llm_call' LIMIT ?",
                (session_id, limit),
            )
            rows = cursor.fetchall()
            return [r[0] for r in rows if r and r[0]]

    def get_llm_traces_for_profiles_map(
        self, session_ids: list[str] | None = None, limit: int = 3
    ) -> dict[str, list[str]]:
        """Return bounded LLM payloads for only the sessions needing fallback."""
        if session_ids is not None and not session_ids:
            return {}

        session_filter = ""
        params: list[Any] = []
        if session_ids is not None:
            placeholders = ",".join("?" for _ in session_ids)
            session_filter = f" AND session_id IN ({placeholders})"
            params.extend(session_ids)
        params.append(limit)

        with db_session(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT session_id, payload FROM ("
                " SELECT session_id, payload, ROW_NUMBER() OVER ("
                "  PARTITION BY session_id ORDER BY timestamp ASC"
                " ) AS row_number FROM traces WHERE type = 'llm_call'"
                f"{session_filter}"
                ") WHERE row_number <= ?",
                params,
            )
            result: dict[str, list[str]] = {}
            for row in cursor.fetchall():
                session_id = str(row[0] or "")
                payload = row[1]
                if session_id and payload:
                    result.setdefault(session_id, []).append(payload)
            return result

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

    def get_agent_trace_names_map(self) -> dict[str, list[str]]:
        """Return distinct agent/LLM trace names grouped by session."""
        with db_session(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT session_id, name FROM traces "
                "WHERE type IN ('agent', 'llm_call') AND session_id IS NOT NULL "
                "GROUP BY session_id, name"
            )
            result: dict[str, list[str]] = {}
            for row in cursor.fetchall():
                session_id = str(row[0] or "")
                name = row[1]
                if session_id and name:
                    result.setdefault(session_id, []).append(name)
            return result

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
            count = 0
            for s_id in orphaned_ids:
                row = cursor.execute(
                    "SELECT pid FROM sessions WHERE session_id = ? AND status = ?",
                    (s_id, "running"),
                ).fetchone()
                if row is None or self.process_is_alive(row["pid"]):
                    continue
                cursor.execute(
                    "UPDATE sessions SET status = ?, end_time = ? "
                    "WHERE session_id = ? AND status = ?",
                    ("failed", now, s_id, "running"),
                )
                count += cursor.rowcount
            conn.commit()
            return count

    def cleanup_orphans_on_startup(self) -> int:
        try:
            with db_session(self.db_path) as conn:
                cursor = conn.cursor()
                rows = cursor.execute(
                    "SELECT session_id, pid FROM sessions WHERE status = ?", ("running",)
                ).fetchall()
                count = 0
                now = time.time()
                for row in rows:
                    if self.process_is_alive(row["pid"]):
                        continue
                    cursor.execute(
                        "UPDATE sessions SET status = ?, end_time = ? "
                        "WHERE session_id = ? AND status = ?",
                        ("failed", now, row["session_id"], "running"),
                    )
                    count += cursor.rowcount
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
