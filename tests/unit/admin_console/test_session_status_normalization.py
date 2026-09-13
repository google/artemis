"""Legacy "success" session rows must surface as the canonical "completed".

The sessions table historically received "success" from the DataEngine while
the queue reconcile wrote "completed", so API consumers saw a mix of both
terminal strings. Reads through the repository now normalize the legacy alias;
get_session_status stays raw so the queue reconcile can persist the rewrite.
"""

from apps.admin_console.database.connection import db_session
from apps.admin_console.database.repositories.session_repository import SessionRepository


def _insert_session(db_path, session_id, status):
    with db_session(db_path) as conn:
        conn.execute(
            "INSERT INTO sessions "
            "(session_id, initial_goal, start_time, end_time, status, device_info, pid) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, session_id, 1.0, 2.0, status, "{}", None),
        )
        conn.commit()


def test_get_all_sessions_normalizes_legacy_success(tmp_path):
    db_path = tmp_path / "sessions.db"
    repository = SessionRepository(db_path)
    _insert_session(db_path, "legacy", "success")
    _insert_session(db_path, "modern", "completed")
    _insert_session(db_path, "broken", "failed")

    statuses = {r["session_id"]: r["status"] for r in repository.get_all_sessions()}
    assert statuses == {"legacy": "completed", "modern": "completed", "broken": "failed"}


def test_get_session_by_id_normalizes_legacy_success(tmp_path):
    db_path = tmp_path / "sessions.db"
    repository = SessionRepository(db_path)
    _insert_session(db_path, "legacy", "success")

    row = repository.get_session_by_id("legacy")
    assert row is not None
    assert row["status"] == "completed"


def test_get_latest_session_normalizes_legacy_success(tmp_path):
    db_path = tmp_path / "sessions.db"
    repository = SessionRepository(db_path)
    _insert_session(db_path, "legacy", "success")

    row = repository.get_latest_session()
    assert row is not None
    assert row["status"] == "completed"


def test_get_session_status_stays_raw_for_queue_reconcile(tmp_path):
    db_path = tmp_path / "sessions.db"
    repository = SessionRepository(db_path)
    _insert_session(db_path, "legacy", "success")

    assert repository.get_session_status("legacy") == "success"
