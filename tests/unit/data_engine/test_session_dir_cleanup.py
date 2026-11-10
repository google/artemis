from __future__ import annotations

from pathlib import Path

from artemis.data_engine.storage import StorageManager

SESSION_ID = "0f3f8d4a-4a2f-4f3a-9d3e-0c1b2a3d4e5f"


def _storage(tmp_path: Path) -> StorageManager:
    return StorageManager(tmp_path / "trace.db", tmp_path / "traces")


def test_an_empty_session_directory_is_removed(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    session_dir = storage.base_trace_dir / SESSION_ID
    session_dir.mkdir(parents=True)

    storage.drop_empty_session_dir(SESSION_ID)

    assert not session_dir.exists()


def test_a_session_directory_holding_a_log_is_kept(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    session_dir = storage.base_trace_dir / SESSION_ID
    session_dir.mkdir(parents=True)
    (session_dir / "stdout.log").write_text("started\n", encoding="utf-8")

    storage.drop_empty_session_dir(SESSION_ID)

    assert (session_dir / "stdout.log").exists()


def test_a_session_directory_holding_a_step_directory_is_kept(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    session_dir = storage.base_trace_dir / SESSION_ID
    (session_dir / "steps" / "1").mkdir(parents=True)

    storage.drop_empty_session_dir(SESSION_ID)

    assert (session_dir / "steps" / "1").is_dir()


def test_a_missing_session_directory_is_not_an_error(tmp_path: Path) -> None:
    storage = _storage(tmp_path)

    storage.drop_empty_session_dir(SESSION_ID)

    assert not (storage.base_trace_dir / SESSION_ID).exists()
