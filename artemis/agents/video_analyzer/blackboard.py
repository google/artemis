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

"""Persistent, video-scoped memory for video analysis agents."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import threading
import time
from typing import Any, Iterator, Literal
from uuid import uuid4

from artemis.context import ArtemisContext
from artemis.utils.logger import get_logger
from artemis.utils.video import get_active_session

logger = get_logger(__name__)


ClaimState = Literal["claimed", "cached", "in_progress"]


@dataclass(frozen=True)
class SegmentClaim:
    """Result of atomically attempting to acquire one analysis segment."""

    state: ClaimState
    lease_owner: str | None = None
    summary: str | None = None
    analysis: str | None = None


def _normalise_query(query: str | None) -> str:
    return " ".join(str(query or "").strip().lower().split())


def _query_key(query: str | None) -> str:
    return hashlib.sha256(_normalise_query(query).encode("utf-8")).hexdigest()


def _to_ms(value: float | int) -> int:
    return int(round(float(value) * 1000.0))


def _to_seconds(value: int) -> float:
    return round(value / 1000.0, 3)


class VideoBlackboard:
    """SQLite-backed blackboard with a context-local in-memory fallback."""

    def __init__(
        self,
        board_key: str,
        *,
        db_path: Path | None = None,
        evidence_dir: Path | None = None,
        lease_seconds: float = 300.0,
    ) -> None:
        self.board_key = board_key
        self.db_path = Path(db_path) if db_path is not None else None
        self.evidence_dir = Path(evidence_dir) if evidence_dir is not None else None
        self.lease_seconds = max(0.01, float(lease_seconds))
        self._lock = threading.RLock()
        self._memory_observations: dict[str, dict[str, Any]] = {}
        self._memory_segments: dict[tuple[str, str, int, int], dict[str, Any]] = {}

        if self.db_path is not None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_db()
        if self.evidence_dir is not None:
            self.evidence_dir.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        if self.db_path is None:
            raise RuntimeError("SQLite connection requested for in-memory blackboard")
        with self._lock:
            conn = sqlite3.connect(self.db_path, timeout=30.0)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
            finally:
                conn.close()

    def _init_db(self) -> None:
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS video_analysis_segments (
                    board_key TEXT NOT NULL,
                    query_key TEXT NOT NULL,
                    query_text TEXT NOT NULL,
                    modality TEXT NOT NULL,
                    start_ms INTEGER NOT NULL,
                    end_ms INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    lease_owner TEXT,
                    lease_expires_at REAL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    summary TEXT,
                    analysis TEXT,
                    error TEXT,
                    error_category TEXT,
                    model_name TEXT,
                    source_generation INTEGER,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (board_key, query_key, modality, start_ms, end_ms)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS video_analysis_observations (
                    observation_id TEXT PRIMARY KEY,
                    board_key TEXT NOT NULL,
                    start_ms INTEGER NOT NULL,
                    end_ms INTEGER,
                    query_text TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    confidence REAL,
                    modality TEXT NOT NULL,
                    screenshot_path TEXT,
                    extra_json TEXT,
                    created_at REAL NOT NULL
                )
                """
            )
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(video_analysis_segments)").fetchall()
            }
            for name, sql_type in (
                ("error_category", "TEXT"),
                ("model_name", "TEXT"),
                ("source_generation", "INTEGER"),
            ):
                if name not in columns:
                    try:
                        conn.execute(
                            f"ALTER TABLE video_analysis_segments ADD COLUMN {name} {sql_type}"
                        )
                    except sqlite3.OperationalError as error:
                        if "duplicate column" not in str(error).lower():
                            raise
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_video_analysis_segments_lookup
                ON video_analysis_segments (board_key, query_key, modality, status, start_ms, end_ms)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_video_analysis_observations_board
                ON video_analysis_observations (board_key, start_ms, end_ms)
                """
            )
            conn.commit()

    def _persist_evidence(self, entry: dict[str, Any], observation_id: str) -> str | None:
        candidate = entry.get("screenshot") or entry.get("screenshot_path")
        if not candidate:
            return None
        source = Path(str(candidate))
        if not source.exists() or not source.is_file() or self.evidence_dir is None:
            return str(source) if source.exists() else None
        try:
            if source.stat().st_size <= 0:
                return None
        except OSError:
            return None

        suffix = source.suffix.lower() or ".jpg"
        destination = self.evidence_dir / f"{observation_id}{suffix}"
        if source.resolve() != destination.resolve() and not destination.exists():
            try:
                shutil.copy2(source, destination)
            except OSError as exc:
                logger.warning(f"Failed to persist video evidence {source}: {exc}")
                return str(source)
        return str(destination)

    def add_observation(self, entry: dict[str, Any], *, modality: str = "video") -> dict[str, Any]:
        """Idempotently persist one evidence-backed observation."""

        start = float(entry.get("start", 0.0))
        raw_end = entry.get("end")
        end = None if raw_end in (None, "unknown") else float(raw_end)
        query_text = str(entry.get("target", ""))
        summary = str(entry.get("summary", "")).strip()
        identity = json.dumps(
            {
                "board_key": self.board_key,
                "start_ms": _to_ms(start),
                "end_ms": _to_ms(end) if end is not None else None,
                "query": _normalise_query(query_text),
                "summary": summary,
                "modality": modality,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        observation_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        screenshot_path = self._persist_evidence(entry, observation_id)

        stored = dict(entry)
        if screenshot_path:
            stored["screenshot"] = screenshot_path
        stored["modality"] = modality
        stored["observation_id"] = observation_id

        excluded = {
            "start",
            "end",
            "target",
            "summary",
            "confidence_score",
            "screenshot",
            "screenshot_path",
            "modality",
            "observation_id",
        }
        extra = {key: value for key, value in stored.items() if key not in excluded}
        now = time.time()

        if self.db_path is None:
            with self._lock:
                self._memory_observations.setdefault(observation_id, stored)
            return dict(self._memory_observations[observation_id])

        with self._connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO video_analysis_observations (
                    observation_id, board_key, start_ms, end_ms, query_text,
                    summary, confidence, modality, screenshot_path, extra_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation_id,
                    self.board_key,
                    _to_ms(start),
                    _to_ms(end) if end is not None else None,
                    query_text,
                    summary,
                    float(entry.get("confidence_score", 0.0)),
                    modality,
                    screenshot_path,
                    json.dumps(extra, default=str),
                    now,
                ),
            )
            conn.commit()
        return stored

    def list_observations(self) -> list[dict[str, Any]]:
        """Return all observations for this recording in timeline order."""

        if self.db_path is None:
            with self._lock:
                entries = [dict(value) for value in self._memory_observations.values()]
            return sorted(entries, key=lambda value: float(value.get("start", 0.0)))

        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM video_analysis_observations
                WHERE board_key = ?
                ORDER BY start_ms, created_at
                """,
                (self.board_key,),
            ).fetchall()

        entries: list[dict[str, Any]] = []
        for row in rows:
            entry: dict[str, Any] = {
                "start": _to_seconds(row["start_ms"]),
                "end": _to_seconds(row["end_ms"]) if row["end_ms"] is not None else "unknown",
                "target": row["query_text"],
                "summary": row["summary"],
                "confidence_score": row["confidence"],
                "modality": row["modality"],
                "observation_id": row["observation_id"],
            }
            if row["screenshot_path"]:
                entry["screenshot"] = row["screenshot_path"]
            if row["extra_json"]:
                try:
                    entry.update(json.loads(row["extra_json"]))
                except (TypeError, json.JSONDecodeError):
                    pass
            entries.append(entry)
        return entries

    def list_ledger_entries(self) -> list[dict[str, Any]]:
        """Return observations plus successful query-specific segment summaries."""

        entries = self.list_observations()
        if self.db_path is None:
            with self._lock:
                for (query_key, modality, start_ms, end_ms), value in self._memory_segments.items():
                    if value.get("status") != "succeeded":
                        continue
                    entries.append(
                        {
                            "start": _to_seconds(start_ms),
                            "end": _to_seconds(end_ms),
                            "target": value.get("query_text", ""),
                            "summary": value.get("summary") or "No summary provided.",
                            "confidence_score": 0.5,
                            "modality": modality,
                            "kind": "segment_result",
                            "query_key": query_key,
                        }
                    )
        else:
            with self._connection() as conn:
                rows = conn.execute(
                    """
                    SELECT query_key, query_text, modality, start_ms, end_ms, summary
                    FROM video_analysis_segments
                    WHERE board_key = ? AND status = 'succeeded'
                    ORDER BY start_ms, updated_at
                    """,
                    (self.board_key,),
                ).fetchall()
            entries.extend(
                {
                    "start": _to_seconds(row["start_ms"]),
                    "end": _to_seconds(row["end_ms"]),
                    "target": row["query_text"],
                    "summary": row["summary"] or "No summary provided.",
                    "confidence_score": 0.5,
                    "modality": row["modality"],
                    "kind": "segment_result",
                    "query_key": row["query_key"],
                }
                for row in rows
            )
        return sorted(entries, key=lambda value: float(value.get("start", 0.0)))

    def claim_segment(
        self,
        start: float,
        end: float,
        query: str,
        *,
        modality: str = "video",
        model_name: str | None = None,
        source_generation: int | None = None,
    ) -> SegmentClaim:
        """Atomically acquire work, or return a cached/in-progress result."""

        start_ms = _to_ms(start)
        end_ms = _to_ms(end)
        if end_ms <= start_ms:
            raise ValueError("Video analysis segment end must be greater than start")
        q_key = _query_key(query)
        key = (q_key, modality, start_ms, end_ms)
        now = time.time()
        owner = str(uuid4())
        expires = now + self.lease_seconds

        if self.db_path is None:
            with self._lock:
                existing = self._memory_segments.get(key)
                if existing and existing["status"] == "succeeded":
                    return SegmentClaim(
                        "cached", summary=existing.get("summary"), analysis=existing.get("analysis")
                    )
                if (
                    existing
                    and existing["status"] == "running"
                    and float(existing.get("lease_expires_at") or 0.0) > now
                ):
                    return SegmentClaim("in_progress")
                attempts = int(existing.get("attempt_count", 0)) + 1 if existing else 1
                self._memory_segments[key] = {
                    "query_text": query,
                    "status": "running",
                    "lease_owner": owner,
                    "lease_expires_at": expires,
                    "attempt_count": attempts,
                    "model_name": model_name,
                    "source_generation": source_generation,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "updated_at": now,
                }
            return SegmentClaim("claimed", lease_owner=owner)

        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM video_analysis_segments
                WHERE board_key = ? AND query_key = ? AND modality = ?
                  AND start_ms = ? AND end_ms = ?
                """,
                (self.board_key, q_key, modality, start_ms, end_ms),
            ).fetchone()
            if row is not None and row["status"] == "succeeded":
                conn.commit()
                return SegmentClaim("cached", summary=row["summary"], analysis=row["analysis"])
            if (
                row is not None
                and row["status"] == "running"
                and float(row["lease_expires_at"] or 0.0) > now
            ):
                conn.commit()
                return SegmentClaim("in_progress")

            if row is None:
                conn.execute(
                    """
                    INSERT INTO video_analysis_segments (
                        board_key, query_key, query_text, modality, start_ms, end_ms,
                        status, lease_owner, lease_expires_at, attempt_count,
                        model_name, source_generation, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, 1, ?, ?, ?, ?)
                    """,
                    (
                        self.board_key,
                        q_key,
                        query,
                        modality,
                        start_ms,
                        end_ms,
                        owner,
                        expires,
                        model_name,
                        source_generation,
                        now,
                        now,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE video_analysis_segments
                    SET status = 'running', lease_owner = ?, lease_expires_at = ?,
                        attempt_count = attempt_count + 1, error = NULL,
                        error_category = NULL, model_name = ?, source_generation = ?,
                        updated_at = ?
                    WHERE board_key = ? AND query_key = ? AND modality = ?
                      AND start_ms = ? AND end_ms = ?
                    """,
                    (
                        owner,
                        expires,
                        model_name,
                        source_generation,
                        now,
                        self.board_key,
                        q_key,
                        modality,
                        start_ms,
                        end_ms,
                    ),
                )
            conn.commit()
        return SegmentClaim("claimed", lease_owner=owner)

    def complete_segment(
        self,
        start: float,
        end: float,
        query: str,
        lease_owner: str,
        summary: str,
        analysis: str | None = None,
        *,
        modality: str = "video",
    ) -> None:
        """Commit a complete result while preserving lease ownership."""

        key = (_query_key(query), modality, _to_ms(start), _to_ms(end))
        now = time.time()
        if self.db_path is None:
            with self._lock:
                row = self._memory_segments.get(key)
                if row is None or row.get("lease_owner") != lease_owner:
                    return
                row.update(
                    {
                        "status": "succeeded",
                        "summary": summary,
                        "analysis": analysis,
                        "lease_expires_at": None,
                        "updated_at": now,
                    }
                )
            return

        with self._connection() as conn:
            conn.execute(
                """
                UPDATE video_analysis_segments
                SET status = 'succeeded', summary = ?, analysis = ?, error = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE board_key = ? AND query_key = ? AND modality = ?
                  AND start_ms = ? AND end_ms = ? AND lease_owner = ?
                """,
                (
                    summary,
                    analysis,
                    now,
                    self.board_key,
                    key[0],
                    modality,
                    key[2],
                    key[3],
                    lease_owner,
                ),
            )
            conn.commit()

    def fail_segment(
        self,
        start: float,
        end: float,
        query: str,
        lease_owner: str,
        error: str,
        *,
        modality: str = "video",
        retryable: bool = True,
        error_category: str | None = None,
    ) -> None:
        """Persist a failed attempt without deleting prior observations."""

        key = (_query_key(query), modality, _to_ms(start), _to_ms(end))
        status = "retryable_failed" if retryable else "permanent_failed"
        now = time.time()
        if self.db_path is None:
            with self._lock:
                row = self._memory_segments.get(key)
                if row is None or row.get("lease_owner") != lease_owner:
                    return
                row.update(
                    {
                        "status": status,
                        "error": str(error),
                        "error_category": error_category,
                        "lease_expires_at": None,
                        "updated_at": now,
                    }
                )
            return

        with self._connection() as conn:
            conn.execute(
                """
                UPDATE video_analysis_segments
                SET status = ?, error = ?, error_category = ?,
                    lease_expires_at = NULL, updated_at = ?
                WHERE board_key = ? AND query_key = ? AND modality = ?
                  AND start_ms = ? AND end_ms = ? AND lease_owner = ?
                """,
                (
                    status,
                    str(error),
                    error_category,
                    now,
                    self.board_key,
                    key[0],
                    modality,
                    key[2],
                    key[3],
                    lease_owner,
                ),
            )
            conn.commit()

    def metrics(self) -> dict[str, Any]:
        """Return a compact operational snapshot for tracing and diagnostics."""

        if self.db_path is None:
            with self._lock:
                rows = [dict(value) for value in self._memory_segments.values()]
                observation_count = len(self._memory_observations)
        else:
            with self._connection() as conn:
                rows = [
                    dict(row)
                    for row in conn.execute(
                        """
                        SELECT status, attempt_count, start_ms, end_ms, error_category
                        FROM video_analysis_segments WHERE board_key = ?
                        """,
                        (self.board_key,),
                    ).fetchall()
                ]
                observation_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM video_analysis_observations WHERE board_key = ?",
                        (self.board_key,),
                    ).fetchone()[0]
                )

        statuses: dict[str, int] = {}
        failure_categories: dict[str, int] = {}
        successful_seconds = 0.0
        attempts = 0
        for row in rows:
            status = str(row.get("status", "unknown"))
            statuses[status] = statuses.get(status, 0) + 1
            attempts += int(row.get("attempt_count", 0) or 0)
            category = row.get("error_category")
            if category:
                failure_categories[str(category)] = failure_categories.get(str(category), 0) + 1
            if status == "succeeded" and row.get("start_ms") is not None:
                successful_seconds += max(
                    0.0,
                    (float(row.get("end_ms", 0)) - float(row.get("start_ms", 0))) / 1000.0,
                )
        return {
            "board_key": self.board_key,
            "segments": statuses,
            "attempts": attempts,
            "failure_categories": failure_categories,
            "successful_seconds": round(successful_seconds, 3),
            "observations": observation_count,
        }

    def successful_segments(
        self, start: float, end: float, query: str, *, modality: str = "video"
    ) -> list[dict[str, Any]]:
        """Return successful segments overlapping a requested interval."""

        start_ms, end_ms = _to_ms(start), _to_ms(end)
        q_key = _query_key(query)
        if self.db_path is None:
            with self._lock:
                rows = []
                for (
                    row_query,
                    row_modality,
                    row_start,
                    row_end,
                ), value in self._memory_segments.items():
                    if (
                        row_query == q_key
                        and row_modality == modality
                        and value.get("status") == "succeeded"
                        and row_end > start_ms
                        and row_start < end_ms
                    ):
                        rows.append(
                            {
                                **value,
                                "start": _to_seconds(row_start),
                                "end": _to_seconds(row_end),
                            }
                        )
            return sorted(rows, key=lambda value: value["start"])

        with self._connection() as conn:
            db_rows = conn.execute(
                """
                SELECT start_ms, end_ms, summary, analysis, status
                FROM video_analysis_segments
                WHERE board_key = ? AND query_key = ? AND modality = ?
                  AND status = 'succeeded' AND end_ms > ? AND start_ms < ?
                ORDER BY start_ms
                """,
                (self.board_key, q_key, modality, start_ms, end_ms),
            ).fetchall()
        return [
            {
                "start": _to_seconds(row["start_ms"]),
                "end": _to_seconds(row["end_ms"]),
                "summary": row["summary"],
                "analysis": row["analysis"],
                "status": row["status"],
            }
            for row in db_rows
        ]

    def missing_intervals(
        self, start: float, end: float, query: str, *, modality: str = "video"
    ) -> list[tuple[float, float]]:
        """Subtract successful exact-query coverage from a requested interval."""

        start_ms, end_ms = _to_ms(start), _to_ms(end)
        if end_ms <= start_ms:
            return []
        covered = []
        for row in self.successful_segments(start, end, query, modality=modality):
            covered.append((max(start_ms, _to_ms(row["start"])), min(end_ms, _to_ms(row["end"]))))
        covered = sorted((left, right) for left, right in covered if right > left)

        merged: list[tuple[int, int]] = []
        for left, right in covered:
            if not merged or left > merged[-1][1]:
                merged.append((left, right))
            else:
                merged[-1] = (merged[-1][0], max(merged[-1][1], right))

        missing: list[tuple[float, float]] = []
        cursor = start_ms
        for left, right in merged:
            if left > cursor:
                missing.append((_to_seconds(cursor), _to_seconds(left)))
            cursor = max(cursor, right)
        if cursor < end_ms:
            missing.append((_to_seconds(cursor), _to_seconds(end_ms)))
        return missing

    def format_cached_segments(
        self, start: float, end: float, query: str, *, modality: str = "video"
    ) -> list[str]:
        """Format cached segment results in the child-agent response shape."""

        results = []
        for row in self.successful_segments(start, end, query, modality=modality):
            text = (
                f"[from {row['start']:.1f}s to {row['end']:.1f}s] "
                f"Summary: {row.get('summary') or 'No summary provided.'}"
            )
            if row.get("analysis"):
                text += f" Analysis: {row['analysis']}"
            results.append(text)
        return results


def _safe_path(value: Any) -> Path | None:
    if isinstance(value, Path):
        return value
    if isinstance(value, str) and value:
        return Path(value)
    return None


def _latest_recorded_video_key(db_path: Path | None, session_id: Any) -> str | None:
    """Resolve a stopped recording so a restarted analyzer keeps its memory key."""
    if db_path is None or not session_id or not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        try:
            row = conn.execute(
                """
                SELECT video_id FROM video_recordings
                WHERE session_id = ?
                ORDER BY start_time DESC LIMIT 1
                """,
                (str(session_id),),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return f"video:{row[0]}" if row and row[0] else None


def get_video_blackboard(ctx: ArtemisContext) -> VideoBlackboard:
    """Return the blackboard shared by every analyzer in an Artemis context."""

    existing = getattr(ctx, "_video_blackboard", None)
    if isinstance(existing, VideoBlackboard):
        return existing

    device_id = str(getattr(getattr(ctx, "device", None), "device_id", "default"))
    data_engine = getattr(ctx, "data_engine", None)
    db_path = None
    base_dir = None
    storage = getattr(data_engine, "storage", None)
    if storage is not None:
        db_path = _safe_path(getattr(storage, "db_path", None))
    if data_engine is not None:
        base_dir = _safe_path(getattr(data_engine, "base_dir", None))

    if base_dir is None:
        execution_setup = getattr(ctx, "execution_setup", None)
        traces_path = _safe_path(getattr(execution_setup, "traces_path", None))
        trace_name = getattr(execution_setup, "trace_name", None)
        if traces_path is not None:
            base_dir = traces_path / str(trace_name) if trace_name else traces_path

    if db_path is None and base_dir is not None:
        db_path = base_dir / "video_blackboard.db"
    evidence_dir = base_dir / "video_blackboard" / "evidence" if base_dir else None

    session = get_active_session(device_id)
    current_session_id = getattr(data_engine, "current_session_id", None)
    if session is not None:
        board_key = f"video:{session.video_id}"
    else:
        board_key = _latest_recorded_video_key(db_path, current_session_id) or (
            f"session:{current_session_id}" if current_session_id else f"device:{device_id}:runtime"
        )

    board = VideoBlackboard(board_key, db_path=db_path, evidence_dir=evidence_dir)
    try:
        setattr(ctx, "_video_blackboard", board)
    except (AttributeError, ValueError):
        logger.debug(
            "Context does not allow caching video_blackboard; SQLite sharing remains active"
        )
    return board
