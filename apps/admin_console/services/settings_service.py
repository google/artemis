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

"""Small persisted key/value settings store for the Artemis console.

Values are JSON-encoded in the ``app_settings`` table so a setting survives
daemon restarts and is readable from any process that shares the Artemis
database (notably the task worker subprocess, which consumes `screen_off`).
"""

from __future__ import annotations

import json
import time
from typing import Any

try:
    from admin_console.database.connection import db_session
except ImportError:  # pragma: no cover - import path differs when run as a package
    from apps.admin_console.database.connection import db_session


class SettingsService:
    """Reads and writes console settings, falling back to built-in defaults."""

    # `screen_off` powers the physical display off while a task runs (scrcpy
    # `--turn-screen-off`); the device stays awake and input/screencap keep working.
    # Off by default so opting in never changes the behaviour of an existing setup.
    DEFAULTS: dict[str, Any] = {
        "screen_off": False,
    }

    def get_all(self) -> dict[str, Any]:
        """Return defaults overlaid with every persisted override."""
        values = dict(self.DEFAULTS)
        with db_session() as conn:
            for row in conn.execute("SELECT key, value FROM app_settings").fetchall():
                key = row["key"]
                if key not in self.DEFAULTS:
                    # Unknown keys are still surfaced so an older/newer frontend
                    # can round-trip settings it knows about.
                    pass
                try:
                    values[key] = json.loads(row["value"]) if row["value"] is not None else None
                except (ValueError, TypeError):
                    values[key] = row["value"]
        return values

    def get(self, key: str, default: Any = None) -> Any:
        """Return one setting value, or ``default`` when it was never set."""
        fallback = self.DEFAULTS.get(key, default)
        with db_session() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return fallback
        try:
            return json.loads(row["value"]) if row["value"] is not None else None
        except (ValueError, TypeError):
            return row["value"]

    def update(self, values: dict[str, Any]) -> dict[str, Any]:
        """Persist the provided keys and return the merged settings snapshot."""
        if not isinstance(values, dict):
            raise ValueError("Settings payload must be a JSON object.")
        now = time.time()
        with db_session() as conn:
            for key, value in values.items():
                if not isinstance(key, str) or not key.strip():
                    continue
                conn.execute(
                    """
                    INSERT INTO app_settings (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                                   updated_at = excluded.updated_at
                    """,
                    (key, json.dumps(value), now),
                )
            conn.commit()
        return self.get_all()


settings_service = SettingsService()
