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

from contextlib import contextmanager
from pathlib import Path
import sqlite3

from artemis.config import DB_PATH


_initialized_dbs = set()


def get_db(db_path: Path | None = None) -> sqlite3.Connection:
    """Returns a SQLite connection with timeout and Row row_factory."""
    path = db_path or DB_PATH
    path_key = str(path)
    if path_key not in _initialized_dbs:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            from artemis.data_engine.storage import StorageManager

            StorageManager(db_path=path, base_trace_dir=path.parent)
            _initialized_dbs.add(path_key)
        except Exception:
            pass
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
    except Exception:
        pass
    return conn


@contextmanager
def db_session(db_path: Path | None = None):
    """Context manager for SQLite connections ensuring clean closure."""
    conn = get_db(db_path)
    try:
        yield conn
    finally:
        conn.close()
