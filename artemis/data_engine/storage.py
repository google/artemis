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

from collections.abc import Iterator
from contextlib import contextmanager
import difflib
import json
from pathlib import Path
import shutil
import sqlite3
import threading
from typing import Any
import uuid
from uuid import UUID

from artemis.data_engine.models import (
    BackgroundTaskRecord,
    FailedOutputRecord,
    HistoryChunkRecord,
    ImageRecord,
    SessionMetadata,
    StepRecord,
    TraceRecord,
    VideoRecordingRecord,
)
from artemis.utils.logger import get_logger


def _safe_uuid(val: Any) -> UUID | str:
    if val is None:
        return ""
    if isinstance(val, UUID):
        return val
    try:
        return UUID(str(val))
    except (ValueError, TypeError):
        return str(val)


class SafeJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, bytes):
            return f"<binary data: {len(obj)} bytes>"
        try:
            return super().default(obj)
        except TypeError:
            return str(obj)


logger = get_logger(__name__)


class StorageManager:
    """Manages persistence for Data Engine using SQLite and File System."""

    def __init__(self, db_path: str | Path, base_trace_dir: str | Path):
        self.db_path = Path(db_path)
        self.base_trace_dir = Path(base_trace_dir)
        self._lock = threading.RLock()

        # Ensure directories exist
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.base_trace_dir.mkdir(parents=True, exist_ok=True)

        self._init_db()

    @contextmanager
    def _get_connection(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self.db_path, timeout=30.0)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
            finally:
                conn.close()

    def _init_db(self):
        """Initialize database tables."""
        with self._get_connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    initial_goal TEXT,
                    start_time REAL,
                    end_time REAL,
                    status TEXT,
                    device_info TEXT,
                    video_filepath TEXT
                )
            """)

            try:
                conn.execute("ALTER TABLE sessions ADD COLUMN video_filepath TEXT")
            except sqlite3.OperationalError:
                pass

            try:
                conn.execute("ALTER TABLE sessions ADD COLUMN pid INTEGER")
            except sqlite3.OperationalError:
                pass

            conn.execute("""
                CREATE TABLE IF NOT EXISTS images (
                    image_name TEXT PRIMARY KEY,
                    timestamp REAL,
                    ocr_result TEXT,
                    ui_tree TEXT,
                    extra_metadata TEXT
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS steps (
                    step_id TEXT PRIMARY KEY,
                    session_id TEXT,
                    step_number INTEGER,
                    timestamp REAL,
                    pre_image_name TEXT,
                    post_image_name TEXT,
                    summary TEXT,
                    action_taken TEXT,
                    operator_raw_thinking TEXT,
                    operator_native_thinking TEXT,
                    last_execution_result TEXT,
                    extra_metadata TEXT,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY(pre_image_name) REFERENCES images(image_name),
                    FOREIGN KEY(post_image_name) REFERENCES images(image_name)
                )
            """)
            try:
                conn.execute("ALTER TABLE steps ADD COLUMN action_taken TEXT")
            except sqlite3.OperationalError:
                pass

            try:
                conn.execute("ALTER TABLE steps ADD COLUMN operator_raw_thinking TEXT")
            except sqlite3.OperationalError:
                pass

            try:
                conn.execute("ALTER TABLE steps ADD COLUMN operator_native_thinking TEXT")
            except sqlite3.OperationalError:
                pass

            try:
                conn.execute("ALTER TABLE steps ADD COLUMN last_execution_result TEXT")
            except sqlite3.OperationalError:
                pass

            conn.execute("""
                CREATE TABLE IF NOT EXISTS traces (
                    trace_id TEXT PRIMARY KEY,
                    session_id TEXT,
                    step_id TEXT,
                    parent_trace_id TEXT,
                    type TEXT,
                    name TEXT,
                    timestamp REAL,
                    duration REAL,
                    status TEXT,
                    payload TEXT,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY(step_id) REFERENCES steps(step_id),
                    FOREIGN KEY(parent_trace_id) REFERENCES traces(trace_id)
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS failed_outputs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    trace_id TEXT,
                    model_name TEXT,
                    prompt TEXT,
                    raw_output TEXT,
                    error_message TEXT,
                    timestamp REAL,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY(trace_id) REFERENCES traces(trace_id)
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS background_tasks (
                    task_id TEXT PRIMARY KEY,
                    session_id TEXT,
                    summary TEXT,
                    status TEXT,
                    start_time REAL,
                    end_time REAL,
                    trace_id TEXT,
                    logs TEXT,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS history_chunks (
                    chunk_id TEXT PRIMARY KEY,
                    session_id TEXT,
                    start_step_id TEXT,
                    end_step_id TEXT,
                    start_step_number INTEGER,
                    end_step_number INTEGER,
                    source_step_ids TEXT,
                    subgoal_hash TEXT,
                    version INTEGER,
                    status TEXT,
                    band1 TEXT,
                    band2 TEXT,
                    band3 TEXT,
                    rendered_text TEXT,
                    created_at REAL,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS video_recordings (
                    video_id TEXT PRIMARY KEY,
                    session_id TEXT,
                    device_id TEXT,
                    start_time REAL,
                    end_time REAL,
                    local_video_path TEXT,
                    status TEXT NOT NULL DEFAULT 'recording',
                    error TEXT,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                )
            """)
            video_recording_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(video_recordings)").fetchall()
            }
            if "status" not in video_recording_columns:
                conn.execute(
                    "ALTER TABLE video_recordings ADD COLUMN status TEXT "
                    "NOT NULL DEFAULT 'recording'"
                )
                conn.execute(
                    "UPDATE video_recordings SET status = 'ready' WHERE end_time IS NOT NULL"
                )
            if "error" not in video_recording_columns:
                conn.execute("ALTER TABLE video_recordings ADD COLUMN error TEXT")
            try:
                conn.execute("ALTER TABLE background_tasks ADD COLUMN logs TEXT")
            except sqlite3.OperationalError:
                pass
            conn.commit()
        logger.info(f"Database initialized at {self.db_path}")

    def create_session(self, session: SessionMetadata):
        """Insert a new session record."""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO sessions (session_id, initial_goal, start_time, end_time, status, device_info, pid, video_filepath)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(session.session_id),
                    session.initial_goal,
                    session.start_time,
                    session.end_time,
                    session.status,
                    json.dumps(session.device_info),
                    session.pid,
                    session.video_filepath,
                ),
            )
            conn.commit()

        # Create session directory
        session_dir = self.base_trace_dir / str(session.session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Session directory created at {session_dir}")

    def update_session(self, session: SessionMetadata):
        """Update an existing session record."""
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE sessions 
                SET end_time = ?, status = ?, device_info = ?, video_filepath = ?
                WHERE session_id = ?
                """,
                (
                    session.end_time,
                    session.status,
                    json.dumps(session.device_info),
                    session.video_filepath,
                    str(session.session_id),
                ),
            )
            conn.commit()

    def create_video_recording(self, record: VideoRecordingRecord):
        """Insert a new video recording record."""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO video_recordings (
                    video_id, session_id, device_id, start_time, end_time,
                    local_video_path, status, error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(record.video_id),
                    str(record.session_id) if record.session_id else None,
                    record.device_id,
                    record.start_time,
                    record.end_time,
                    record.local_video_path,
                    record.status,
                    record.error,
                ),
            )
            conn.commit()

    def update_video_recording(self, record: VideoRecordingRecord):
        """Update an existing video recording record and session video path."""
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE video_recordings
                SET end_time = ?, local_video_path = ?, status = ?, error = ?
                WHERE video_id = ?
                """,
                (
                    record.end_time,
                    record.local_video_path,
                    record.status,
                    record.error,
                    str(record.video_id),
                ),
            )
            if record.session_id and record.local_video_path:
                conn.execute(
                    """
                    UPDATE sessions
                    SET video_filepath = ?
                    WHERE session_id = ?
                    """,
                    (
                        record.local_video_path,
                        str(record.session_id),
                    ),
                )
            conn.commit()

    def update_session_video_path(self, session_id: UUID, video_path: str):
        """Update video filepath across session and video_recordings tables."""
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE sessions SET video_filepath = ? WHERE session_id = ?",
                (str(video_path), str(session_id)),
            )
            conn.execute(
                "UPDATE video_recordings SET local_video_path = ? WHERE session_id = ?",
                (str(video_path), str(session_id)),
            )
            conn.commit()

    def get_video_recording(self, video_id: UUID) -> VideoRecordingRecord | None:
        """Retrieve a video recording record by ID."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM video_recordings WHERE video_id = ?",
                (str(video_id),),
            )
            row = cursor.fetchone()
            if row:
                return VideoRecordingRecord(
                    video_id=_safe_uuid(row["video_id"]),
                    session_id=_safe_uuid(row["session_id"]) if row["session_id"] else None,
                    device_id=row["device_id"],
                    start_time=row["start_time"],
                    end_time=row["end_time"],
                    local_video_path=row["local_video_path"],
                    status=row["status"] if "status" in row.keys() else (
                        "ready" if row["end_time"] is not None else "recording"
                    ),
                    error=row["error"] if "error" in row.keys() else None,
                )
        return None

    def create_image(self, image: ImageRecord):
        """Insert a new image record."""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO images (image_name, timestamp, ocr_result, ui_tree, extra_metadata)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    image.image_name,
                    image.timestamp,
                    json.dumps(image.ocr_result) if image.ocr_result else None,
                    json.dumps(image.ui_tree) if image.ui_tree else None,
                    json.dumps(image.extra_metadata),
                ),
            )
            conn.commit()

    def get_image(self, image_name: str) -> ImageRecord | None:
        """Retrieve an image by its name."""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM images WHERE image_name = ?", (image_name,))
            row = cursor.fetchone()
            if row:
                return ImageRecord(
                    image_name=row["image_name"],
                    timestamp=row["timestamp"],
                    ocr_result=json.loads(row["ocr_result"]) if row["ocr_result"] else None,
                    ui_tree=json.loads(row["ui_tree"]) if row["ui_tree"] else None,
                    extra_metadata=json.loads(row["extra_metadata"]),
                )
        return None

    def update_image_data(self, image_name: str, ocr_result: Any | None, ui_tree: Any | None):
        """Update image record with OCR and UI tree if they are not already set."""
        with self._get_connection() as conn:
            ocr_json = json.dumps(ocr_result) if ocr_result is not None else None
            ui_tree_json = json.dumps(ui_tree) if ui_tree is not None else None

            conn.execute(
                """
                UPDATE images
                SET ocr_result = COALESCE(ocr_result, ?),
                    ui_tree = COALESCE(ui_tree, ?)
                WHERE image_name = ?
                """,
                (ocr_json, ui_tree_json, image_name),
            )
            conn.commit()

    def search_ui_by_hash(self, image_name: str, query: str, threshold: float = 0.6) -> list[dict]:
        """Search for text in UI data (XML and OCR) retrieved by image hash."""
        record = self.get_image(image_name)
        if not record:
            return []

        matches = []

        def check_match(text, target_query):
            if not text:
                return 0.0
            return difflib.SequenceMatcher(None, target_query.lower(), text.lower()).ratio()

        # Search in ui_tree (raw XML elements)
        if record.ui_tree:
            for node in record.ui_tree:
                best_ratio = 0.0
                best_target = ""

                # Check text
                text = node.get("text")
                if text:
                    ratio = check_match(text, query)
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_target = text

                # Check content-desc
                desc = node.get("content-desc")
                if desc:
                    ratio = check_match(desc, query)
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_target = desc

                if best_ratio > threshold:
                    matches.append(
                        {
                            "score": best_ratio,
                            "type": "xml",
                            "node": node,
                            "matched_text": best_target,
                        }
                    )

        # Search in ocr_result (raw OCR results)
        if record.ocr_result:
            for ocr in record.ocr_result:
                text = ocr.get("text")
                if text:
                    ratio = check_match(text, query)
                    if ratio > threshold:
                        matches.append(
                            {
                                "score": ratio,
                                "type": "ocr",
                                "node": ocr,
                                "matched_text": text,
                            }
                        )

        # Sort by score descending
        matches.sort(key=lambda x: x["score"], reverse=True)
        return matches

    def create_step(self, step: StepRecord):
        """Insert a new step record."""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO steps (
                    step_id, session_id, step_number, timestamp,
                    pre_image_name, post_image_name,
                    summary, action_taken, operator_raw_thinking, operator_native_thinking, last_execution_result, extra_metadata
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(step.step_id),
                    str(step.session_id),
                    step.step_number,
                    step.timestamp,
                    step.pre_image_name,
                    step.post_image_name,
                    step.summary,
                    json.dumps(step.action_taken) if step.action_taken else None,
                    step.operator_raw_thinking,
                    step.operator_native_thinking,
                    json.dumps(step.last_execution_result) if step.last_execution_result else None,
                    json.dumps(step.extra_metadata),
                ),
            )
            conn.commit()

    def update_step_action(self, step_id: UUID, action_taken: dict):
        """Update the action taken for a step."""
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE steps 
                SET action_taken = ?
                WHERE step_id = ?
                """,
                (
                    json.dumps(action_taken),
                    str(step_id),
                ),
            )
            conn.commit()

    def update_step_summary(
        self,
        step_id: UUID,
        summary: str | None,
        *,
        source: str | None = None,
        version: int | None = None,
        model: str | None = None,
        status: str = "ready",
    ) -> bool:
        """Versioned, status-carrying summary write (replaces the blind overwrite).

        Writes ``summary_status`` / ``summary_source`` / ``summary_version`` /
        ``summary_model`` into the step's ``extra_metadata``. Concurrent writes
        to the same step are ordered by version: an explicit ``version`` lower
        than the stored one is dropped; ``version=None`` auto-increments the
        stored version. Status downgrades are refused without a newer explicit
        version: ``pending`` never overwrites ``ready``/``failed``, and
        ``failed`` never overwrites ``ready``. The ``summary`` column itself is
        only touched by a ``ready`` write with a non-None summary.

        Returns True when the write was applied, False when it was dropped as
        stale (or the step does not exist).
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT extra_metadata FROM steps WHERE step_id = ?",
                (str(step_id),),
            )
            row = cursor.fetchone()
            if row is None:
                return False

            try:
                meta = json.loads(row["extra_metadata"]) if row["extra_metadata"] else {}
                if not isinstance(meta, dict):
                    meta = {}
            except Exception:
                meta = {}

            current_version = meta.get("summary_version")
            current_version = current_version if isinstance(current_version, int) else 0
            current_status = meta.get("summary_status")

            if version is not None:
                if version < current_version:
                    return False
                new_version = version
                is_newer = version > current_version
            else:
                new_version = current_version + 1
                is_newer = False

            # Status downgrades require an explicitly newer version.
            if not is_newer:
                if status == "pending" and current_status in ("ready", "failed"):
                    return False
                if status == "failed" and current_status == "ready":
                    return False

            meta["summary_status"] = status
            meta["summary_version"] = new_version
            if source is not None:
                meta["summary_source"] = source
            if model is not None:
                meta["summary_model"] = model

            if status == "ready" and summary is not None:
                conn.execute(
                    "UPDATE steps SET summary = ?, extra_metadata = ? WHERE step_id = ?",
                    (summary, json.dumps(meta), str(step_id)),
                )
            else:
                conn.execute(
                    "UPDATE steps SET extra_metadata = ? WHERE step_id = ?",
                    (json.dumps(meta), str(step_id)),
                )
            conn.commit()
            return True

    def create_history_chunk(self, chunk: HistoryChunkRecord):
        """Insert one history-chunk version row (append-only; never updates)."""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO history_chunks (
                    chunk_id, session_id, start_step_id, end_step_id,
                    start_step_number, end_step_number, source_step_ids,
                    subgoal_hash, version, status, band1, band2, band3,
                    rendered_text, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(chunk.chunk_id),
                    str(chunk.session_id),
                    chunk.start_step_id,
                    chunk.end_step_id,
                    chunk.start_step_number,
                    chunk.end_step_number,
                    json.dumps(chunk.source_step_ids),
                    chunk.subgoal_hash,
                    chunk.version,
                    chunk.status,
                    json.dumps(chunk.band1, ensure_ascii=False, cls=SafeJSONEncoder),
                    chunk.band2,
                    chunk.band3,
                    chunk.rendered_text,
                    chunk.created_at,
                ),
            )
            conn.commit()

    def get_history_chunks(
        self, session_id: UUID | str, *, all_versions: bool = False
    ) -> list[HistoryChunkRecord]:
        """History chunks of a session ordered by step range.

        By default only the newest version per step range is returned; pass
        ``all_versions=True`` for the full append-only trail (audit/tests).
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM history_chunks WHERE session_id = ?
                ORDER BY start_step_number ASC, version ASC, created_at ASC
                """,
                (str(session_id),),
            )
            rows = cursor.fetchall()

        records: list[HistoryChunkRecord] = []
        for row in rows:
            try:
                source_ids = json.loads(row["source_step_ids"]) if row["source_step_ids"] else []
            except Exception:
                source_ids = []
            try:
                band1 = json.loads(row["band1"]) if row["band1"] else {}
                if not isinstance(band1, dict):
                    band1 = {}
            except Exception:
                band1 = {}
            records.append(
                HistoryChunkRecord(
                    chunk_id=row["chunk_id"],
                    session_id=row["session_id"],
                    start_step_id=row["start_step_id"],
                    end_step_id=row["end_step_id"],
                    start_step_number=row["start_step_number"],
                    end_step_number=row["end_step_number"],
                    source_step_ids=source_ids,
                    subgoal_hash=row["subgoal_hash"],
                    version=row["version"],
                    status=row["status"],
                    band1=band1,
                    band2=row["band2"],
                    band3=row["band3"],
                    rendered_text=row["rendered_text"],
                    created_at=row["created_at"],
                )
            )
        if all_versions:
            return records

        latest: dict[tuple[int, int], HistoryChunkRecord] = {}
        for rec in records:
            key = (rec.start_step_number, rec.end_step_number)
            current = latest.get(key)
            if current is None or rec.version >= current.version:
                latest[key] = rec
        return sorted(latest.values(), key=lambda r: r.start_step_number)

    def update_step_thinking(self, step_id: UUID, operator_raw_thinking: str):
        """Update the raw thinking process for a step."""
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE steps 
                SET operator_raw_thinking = ?
                WHERE step_id = ?
                """,
                (
                    operator_raw_thinking,
                    str(step_id),
                ),
            )
            conn.commit()

    def update_step_native_thinking(self, step_id: UUID, operator_native_thinking: str):
        """Update the native thinking process for a step."""
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE steps 
                SET operator_native_thinking = ?
                WHERE step_id = ?
                """,
                (
                    operator_native_thinking,
                    str(step_id),
                ),
            )
            conn.commit()

    def update_step_execution_result(
        self,
        step_id: UUID,
        last_execution_result: dict,
        post_image_name: str | None = None,
    ):
        """Update the validator execution result and post_image_name for a step."""
        with self._get_connection() as conn:
            if post_image_name:
                cursor = conn.execute(
                    "SELECT pre_image_name FROM steps WHERE step_id = ?",
                    (str(step_id),),
                )
                row = cursor.fetchone()
                if row and row["pre_image_name"] == post_image_name:
                    post_image_name = None

            if post_image_name:
                conn.execute(
                    """
                    UPDATE steps 
                    SET last_execution_result = ?, post_image_name = ?
                    WHERE step_id = ?
                    """,
                    (
                        json.dumps(last_execution_result),
                        post_image_name,
                        str(step_id),
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE steps 
                    SET last_execution_result = ?
                    WHERE step_id = ?
                    """,
                    (
                        json.dumps(last_execution_result),
                        str(step_id),
                    ),
                )
            conn.commit()

    def get_session(self, session_id: UUID | str) -> SessionMetadata | None:
        """Retrieve a session by ID."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (str(session_id),),
            )
            row = cursor.fetchone()
            if row:
                row_dict = dict(row)
                return SessionMetadata(
                    session_id=_safe_uuid(row_dict["session_id"]),
                    initial_goal=row_dict["initial_goal"],
                    start_time=row_dict["start_time"],
                    end_time=row_dict["end_time"],
                    status=row_dict["status"],
                    device_info=json.loads(row_dict["device_info"]),
                    pid=row_dict.get("pid"),
                    video_filepath=row_dict.get("video_filepath"),
                )
        return None

    def get_steps(self, session_id: UUID | str) -> list[StepRecord]:
        """Retrieve all steps for a session."""
        steps = []
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM steps WHERE session_id = ? ORDER BY step_number ASC",
                (str(session_id),),
            )
            for row in cursor.fetchall():
                action_taken = None
                if "action_taken" in row.keys() and row["action_taken"]:
                    try:
                        action_taken = json.loads(row["action_taken"])
                    except Exception:
                        pass

                operator_raw_thinking = None
                if "operator_raw_thinking" in row.keys() and row["operator_raw_thinking"]:
                    operator_raw_thinking = row["operator_raw_thinking"]

                operator_native_thinking = None
                if "operator_native_thinking" in row.keys() and row["operator_native_thinking"]:
                    operator_native_thinking = row["operator_native_thinking"]

                last_execution_result = None
                if "last_execution_result" in row.keys() and row["last_execution_result"]:
                    try:
                        last_execution_result = json.loads(row["last_execution_result"])
                    except Exception:
                        pass

                steps.append(
                    StepRecord(
                        step_id=_safe_uuid(row["step_id"]),
                        session_id=_safe_uuid(row["session_id"]),
                        step_number=row["step_number"],
                        timestamp=row["timestamp"],
                        pre_image_name=row["pre_image_name"],
                        post_image_name=row["post_image_name"],
                        summary=row["summary"],
                        action_taken=action_taken,
                        operator_raw_thinking=operator_raw_thinking,
                        operator_native_thinking=operator_native_thinking,
                        last_execution_result=last_execution_result,
                        extra_metadata=json.loads(row["extra_metadata"]),
                    )
                )
        return steps

    get_session_steps = get_steps

    def get_traces_for_step(self, step_id: UUID | str) -> list[TraceRecord]:
        """Retrieve all traces for a step."""
        traces = []
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM traces WHERE step_id = ? ORDER BY timestamp ASC",
                (str(step_id),),
            )
            for row in cursor.fetchall():
                traces.append(
                    TraceRecord(
                        trace_id=_safe_uuid(row["trace_id"]),
                        session_id=_safe_uuid(row["session_id"]),
                        step_id=_safe_uuid(row["step_id"]) if row["step_id"] else None,
                        parent_trace_id=_safe_uuid(row["parent_trace_id"])
                        if row["parent_trace_id"]
                        else None,
                        type=row["type"],
                        name=row["name"],
                        timestamp=row["timestamp"],
                        duration=row["duration"],
                        status=row["status"],
                        payload=json.loads(row["payload"]) if row["payload"] else {},
                    )
                )
        return traces

    def get_steps_with_traces(self, session_id: UUID) -> list[tuple[StepRecord, list[TraceRecord]]]:
        """Retrieve all steps and their associated traces for a session."""
        steps = self.get_steps(session_id)
        result = []
        for step in steps:
            traces = self.get_traces_for_step(step.step_id)
            result.append((step, traces))
        return result

    def create_trace(self, trace: TraceRecord):
        """Insert or update a trace record (handles out-of-order writes and preserves start timestamps)."""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO traces (
                    trace_id, session_id, step_id, parent_trace_id, type, name,
                    timestamp, duration, status, payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trace_id) DO UPDATE SET
                    status = CASE 
                        WHEN excluded.status = 'running' AND traces.status IN ('success', 'failed') THEN traces.status
                        ELSE excluded.status
                    END,
                    duration = COALESCE(excluded.duration, traces.duration),
                    timestamp = MIN(traces.timestamp, excluded.timestamp),
                    payload = CASE
                        WHEN excluded.status = 'running' AND traces.status IN ('success', 'failed') THEN traces.payload
                        ELSE excluded.payload
                    END
                """,
                (
                    str(trace.trace_id),
                    str(trace.session_id),
                    str(trace.step_id) if trace.step_id else None,
                    str(trace.parent_trace_id) if trace.parent_trace_id else None,
                    trace.type,
                    trace.name,
                    trace.timestamp,
                    trace.duration,
                    trace.status,
                    json.dumps(trace.payload, cls=SafeJSONEncoder),
                ),
            )
            conn.commit()

    def create_failed_output(self, record: FailedOutputRecord):
        """Insert a new failed output record (crime scene)."""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO failed_outputs (
                    session_id, trace_id, model_name, prompt, raw_output, error_message, timestamp
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(record.session_id),
                    str(record.trace_id),
                    record.model_name,
                    record.prompt,
                    record.raw_output,
                    record.error_message,
                    record.timestamp,
                ),
            )
            conn.commit()

    def create_background_task(self, record: BackgroundTaskRecord):
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO background_tasks (
                    task_id, session_id, summary, status, start_time, end_time, trace_id, logs
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.task_id,
                    str(record.session_id),
                    record.summary,
                    record.status,
                    record.start_time,
                    record.end_time,
                    record.trace_id,
                    record.logs,
                ),
            )
            conn.commit()

    def update_background_task_status(self, task_id: str, status: str, end_time: float):
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE background_tasks
                SET status = ?, end_time = ?
                WHERE task_id = ?
                """,
                (status, end_time, task_id),
            )
            conn.commit()

    def update_background_task_status_and_logs(
        self, task_id: str, status: str, end_time: float, logs: str
    ):
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE background_tasks
                SET status = ?, end_time = ?, logs = ?
                WHERE task_id = ?
                """,
                (status, end_time, logs, task_id),
            )
            conn.commit()

    def get_background_tasks(self, session_id: UUID) -> list[BackgroundTaskRecord]:
        tasks = []
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM background_tasks WHERE session_id = ? ORDER BY start_time ASC",
                (str(session_id),),
            )
            for row in cursor.fetchall():
                logs = row["logs"] if "logs" in row.keys() else None
                tasks.append(
                    BackgroundTaskRecord(
                        task_id=row["task_id"],
                        session_id=_safe_uuid(row["session_id"]),
                        summary=row["summary"],
                        status=row["status"],
                        start_time=row["start_time"],
                        end_time=row["end_time"],
                        trace_id=row["trace_id"],
                        logs=logs,
                    )
                )
        return tasks

    def get_step_traces(self, step_id: UUID | str) -> list[TraceRecord]:
        """Retrieve all traces for a specific step."""
        traces = []
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM traces WHERE step_id = ? ORDER BY timestamp ASC",
                (str(step_id),),
            )
            for row in cursor.fetchall():
                traces.append(
                    TraceRecord(
                        trace_id=_safe_uuid(row["trace_id"]),
                        session_id=_safe_uuid(row["session_id"]),
                        step_id=_safe_uuid(row["step_id"]) if row["step_id"] else None,
                        parent_trace_id=_safe_uuid(row["parent_trace_id"])
                        if row["parent_trace_id"]
                        else None,
                        type=row["type"],
                        name=row["name"],
                        timestamp=row["timestamp"],
                        duration=row["duration"],
                        status=row["status"],
                        payload=json.loads(row["payload"]) if row["payload"] else {},
                    )
                )
        return traces

    def clear_all_data(self):
        """Clear all data from SQLite tables and delete session directories."""
        with self._get_connection() as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            tables = [
                "video_analysis_observations",
                "video_analysis_segments",
                "failed_outputs",
                "traces",
                "background_tasks",
                "steps",
                "images",
                "sessions",
            ]
            for table in tables:
                try:
                    conn.execute(f"DELETE FROM {table}")
                except sqlite3.OperationalError as e:
                    logger.warning(f"Failed to clear table {table}: {e}")
            conn.commit()
            conn.execute("VACUUM")
            conn.commit()
        logger.info("Database tables cleared and vacuumed.")

        # Delete session directories
        if self.base_trace_dir.exists():
            for path in self.base_trace_dir.iterdir():
                if path.is_dir():
                    if path.name == "images":
                        # Clear images
                        for img_path in path.iterdir():
                            if img_path.is_file():
                                try:
                                    img_path.unlink()
                                except Exception as e:
                                    logger.error(f"Failed to delete image {img_path}: {e}")
                    elif path.name.startswith("web_") or self._is_uuid(path.name):
                        try:
                            shutil.rmtree(path)
                        except Exception as e:
                            logger.error(f"Failed to delete directory {path}: {e}")
                elif path.is_file() and path.name == "mcp_server.log":
                    try:
                        path.unlink()
                    except Exception as e:
                        logger.error(f"Failed to delete log file {path}: {e}")

    def _is_uuid(self, val: str) -> bool:
        # Support both pure UUID and UUID with suffix (e.g., UUID_PASS_...)
        parts = val.split("_")
        target = parts[0] if parts else val
        try:
            uuid.UUID(target)
            return True
        except ValueError:
            return False

    def delete_session(self, session_id: UUID):
        """Delete all data associated with a session, including files on disk."""
        session_id_str = str(session_id)

        # 1. Get video paths before deleting from DB
        video_paths = []
        video_ids: list[str] = []
        try:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    "SELECT video_id, local_video_path FROM video_recordings WHERE session_id = ?",
                    (session_id_str,),
                )
                for row in cursor.fetchall():
                    if row["video_id"]:
                        video_ids.append(str(row["video_id"]))
                    if row["local_video_path"]:
                        video_paths.append(Path(row["local_video_path"]))
        except sqlite3.OperationalError as e:
            logger.warning(f"Could not query video_recordings: {e}")

        # 2. Delete from DB tables
        with self._get_connection() as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            board_keys = [f"session:{session_id_str}"] + [
                f"video:{video_id}" for video_id in video_ids
            ]
            for table in (
                "video_analysis_observations",
                "video_analysis_segments",
            ):
                try:
                    conn.executemany(
                        f"DELETE FROM {table} WHERE board_key = ?",
                        [(key,) for key in board_keys],
                    )
                except sqlite3.OperationalError as e:
                    logger.warning(f"Failed to delete video memory from {table}: {e}")
            tables = [
                "failed_outputs",
                "traces",
                "background_tasks",
                "video_recordings",
                "steps",
                "sessions",
            ]
            for table in tables:
                try:
                    conn.execute(
                        f"DELETE FROM {table} WHERE session_id = ?",
                        (session_id_str,),
                    )
                except sqlite3.OperationalError as e:
                    logger.warning(f"Failed to delete from table {table}: {e}")
            conn.commit()
            conn.execute("VACUUM")
            conn.commit()

        logger.info(f"Database records for session {session_id} cleared.")

        # 3. Delete session directory (notes, etc.)
        session_dir = self.base_trace_dir / session_id_str
        if session_dir.exists() and session_dir.is_dir():
            try:
                shutil.rmtree(session_dir)
                logger.info(f"Deleted session directory: {session_dir}")
            except Exception as e:
                logger.error(f"Failed to delete session directory {session_dir}: {e}")

        # 4. Delete video files and their containing folders (which might have been renamed)
        for video_path in video_paths:
            # video_path is likely: /path/to/traces/task_name/recording.mp4
            # If it was renamed, it might be: /path/to/traces/task_name_PASS_timestamp/recording.mp4

            task_name = video_path.parent.name

            if task_name and task_name != "traces" and task_name != "..":
                # Search for directories starting with task_name in base_trace_dir
                try:
                    for path in self.base_trace_dir.iterdir():
                        if path.is_dir() and path.name.startswith(task_name):
                            try:
                                shutil.rmtree(path)
                                logger.info(f"Deleted trace/video directory: {path}")
                            except Exception as e:
                                logger.error(f"Failed to delete directory {path}: {e}")
                        elif path.is_file() and path.name.startswith(task_name):
                            try:
                                path.unlink()
                                logger.info(f"Deleted trace/video file: {path}")
                            except Exception as e:
                                logger.error(f"Failed to delete file {path}: {e}")
                except Exception as e:
                    logger.error(f"Error iterating base_trace_dir for cleanup: {e}")

            if video_path.exists():
                try:
                    video_path.unlink()
                    logger.info(f"Deleted video file directly: {video_path}")
                    if video_path.parent.exists() and not any(video_path.parent.iterdir()):
                        video_path.parent.rmdir()
                except Exception as e:
                    logger.error(f"Failed to delete video file directly {video_path}: {e}")
