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
from fastapi import HTTPException

try:
    from admin_console.core.config import DB_PATH
except ImportError:
    from apps.admin_console.core.config import DB_PATH


def get_db(db_path: Path | None = None) -> sqlite3.Connection:
    """Returns a SQLite connection with timeout and Row row_factory."""
    path = db_path or DB_PATH
    if not path.exists():
        raise HTTPException(status_code=500, detail=f"Database not found at {path}")
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def db_session(db_path: Path | None = None):
    """Context manager for SQLite connections ensuring clean closure."""
    conn = get_db(db_path)
    try:
        yield conn
    finally:
        conn.close()
