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

"""``/api/sessions/{id}/usage``: token totals, live context and run tuning."""

import json

from apps.admin_console.database.connection import db_session
from apps.admin_console.database.repositories.session_repository import SessionRepository


def _insert_session(db_path, session_id, device_info):
    with db_session(db_path) as conn:
        conn.execute(
            "INSERT INTO sessions (session_id, initial_goal, start_time, status, device_info)"
            " VALUES (?, ?, ?, ?, ?)",
            (session_id, "goal", 1.0, "completed", json.dumps(device_info)),
        )
        conn.commit()


def _insert_usage(db_path, session_id, trace_id, ts, payload, *, name="llm_usage"):
    with db_session(db_path) as conn:
        conn.execute(
            "INSERT INTO traces (trace_id, session_id, type, name, timestamp, status, payload)"
            " VALUES (?, ?, 'llm_call', ?, ?, 'success', ?)",
            (trace_id, session_id, name, ts, json.dumps(payload)),
        )
        conn.commit()


def _usage(prompt, completion, node=None, cached=0):
    payload = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "cached_tokens": cached,
        "context_base_tokens": prompt,
    }
    if node:
        payload["node"] = node
    return payload


def test_usage_sums_all_agents_and_tracks_latest_operator_context(tmp_path):
    db_path = tmp_path / "sessions.db"
    repo = SessionRepository(db_path)
    _insert_session(
        db_path,
        "s1",
        {
            "profile": "pro",
            "run_tuning": {"verification_level": "checkpoints", "explorer_mode": "pro"},
        },
    )
    _insert_usage(db_path, "s1", "t1", 10.0, _usage(30_000, 500, node="planner"))
    _insert_usage(db_path, "s1", "t2", 11.0, _usage(120_000, 800, node="operator", cached=90_000))
    _insert_usage(db_path, "s1", "t3", 12.0, _usage(5_000, 100, node="checker"))
    _insert_usage(db_path, "s1", "t4", 13.0, _usage(150_000, 700, node="operator"))
    # A retry trace shares the type but is not a usage record.
    _insert_usage(db_path, "s1", "t5", 14.0, {"error": "503"}, name="llm_retry")

    usage = repo.get_session_usage("s1")

    assert usage["llm_calls"] == 4
    assert usage["prompt_tokens"] == 305_000
    assert usage["completion_tokens"] == 2_100
    assert usage["total_tokens"] == 307_100
    assert usage["cached_tokens"] == 90_000
    assert usage["operator_context_tokens"] == 150_000
    assert usage["operator_context_updated_at"] == 13.0
    assert usage["operator_context_window_tokens"] == 1_000_000
    assert usage["profile"] == "pro"
    assert usage["run_tuning"] == {"verification_level": "checkpoints", "explorer_mode": "pro"}


def test_usage_flash_runner_counts_as_operator_and_has_no_tuning(tmp_path):
    db_path = tmp_path / "sessions.db"
    repo = SessionRepository(db_path)
    _insert_session(db_path, "s2", {"profile": "flash"})
    _insert_usage(db_path, "s2", "t1", 1.0, _usage(20_000, 200, node="FlashRunner"))

    usage = repo.get_session_usage("s2")

    assert usage["operator_context_tokens"] == 20_000
    assert usage["total_tokens"] == 20_200
    assert usage["profile"] == "flash"
    assert usage["run_tuning"] is None


def test_usage_for_unknown_or_empty_session_is_zeroed(tmp_path):
    db_path = tmp_path / "sessions.db"
    repo = SessionRepository(db_path)

    usage = repo.get_session_usage("missing")

    assert usage["llm_calls"] == 0
    assert usage["total_tokens"] == 0
    assert usage["operator_context_tokens"] is None
    assert usage["profile"] is None
    assert usage["run_tuning"] is None


def test_usage_route_is_registered():
    from apps.admin_console.routers import sessions

    paths = {getattr(r, "path", None) for r in sessions.router.routes}
    assert "/api/sessions/{session_id}/usage" in paths
