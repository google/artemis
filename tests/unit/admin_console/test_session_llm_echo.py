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

"""Server-side producer/consumer contract for the per-task LLM override echo.

The SDK writes ``llm_model`` / ``llm_provider`` into the session's schemaless
``device_info``; the console reads them back and reports the model the run
actually used. These tests drive the real route handler against a real database
row, so a producer/consumer mismatch fails here rather than in the UI.
"""

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from apps.admin_console.database.connection import db_session
from apps.admin_console.database.repositories.session_repository import SessionRepository
from apps.admin_console.routers import sessions as sessions_router
from apps.admin_console.services.model_service import ModelService

# The configured (global) LLM, patched so the tests never read artemis.jsonc.
_CONFIGURED = ("google", "gemini-3.8-flash")


def _insert_session(db_path, session_id, device_info):
    with db_session(db_path) as conn:
        conn.execute(
            "INSERT INTO sessions (session_id, initial_goal, start_time, status, device_info)"
            " VALUES (?, ?, ?, ?, ?)",
            (session_id, "goal", 1.0, "completed", json.dumps(device_info)),
        )
        conn.commit()


def _real_repo_row(tmp_path, monkeypatch, session_id, device_info):
    """Back the router's repository with a real DB row for one session."""
    db_path = tmp_path / "sessions.db"
    repo = SessionRepository(db_path)
    _insert_session(db_path, session_id, device_info)
    monkeypatch.setattr(sessions_router.session_repo, "get_session_by_id", repo.get_session_by_id)
    return repo


@patch.object(ModelService, "_get_llm_provider_and_model", return_value=_CONFIGURED)
def test_get_session_details_echoes_the_recorded_override(_configured, tmp_path, monkeypatch):
    """GET /api/sessions/{id} reports the model this run was pinned to."""
    _real_repo_row(
        tmp_path,
        monkeypatch,
        "s1",
        {
            "profile": "pro",
            "llm_model": "gpt-5.1",
            "llm_provider": "openai",
            "run_tuning": {"verification_level": "final"},
        },
    )

    payload = asyncio.run(sessions_router.get_session_details("s1"))

    assert payload["llm_model"] == "gpt-5.1"
    assert payload["llm_provider"] == "openai"
    # Both halves sit at the top level of the payload: that is the contract
    # artemis-client's TaskResult.from_payload reads, so the echo resolves for a
    # remote caller and not only against a mocked transport.
    assert isinstance(payload["llm_model"], str)
    assert isinstance(payload["llm_provider"], str)
    # Parity with the list endpoint: model_info reflects the same override.
    assert payload["model_info"]["id"] == "gpt-5.1"
    assert payload["model_info"]["provider"] == "openai"
    assert payload["model_info"]["name"] == "Pro"
    # The stored row is untouched.
    assert json.loads(payload["device_info"])["llm_model"] == "gpt-5.1"


@patch.object(ModelService, "_get_llm_provider_and_model", return_value=_CONFIGURED)
def test_get_session_details_reports_null_for_pre_override_rows(_configured, tmp_path, monkeypatch):
    """A row written before the override existed reports null, not a guess."""
    _real_repo_row(tmp_path, monkeypatch, "s2", {"profile": "pro"})

    payload = asyncio.run(sessions_router.get_session_details("s2"))

    assert payload["llm_model"] is None
    assert payload["llm_provider"] is None
    assert payload["model_info"]["id"] == "gemini-3.8-flash"
    assert payload["model_info"]["provider"] == "google"


@pytest.mark.asyncio
@patch.object(ModelService, "_get_llm_provider_and_model", return_value=_CONFIGURED)
async def test_list_sessions_model_info_prefers_the_recorded_override(_configured, monkeypatch):
    """The list endpoint applies the same override to every row."""
    repo = MagicMock()
    repo.get_all_sessions.return_value = [
        {
            "session_id": "session-override",
            "status": "completed",
            "start_time": 1.0,
            "device_info": json.dumps(
                {"profile": "pro", "llm_model": "gpt-5.1", "llm_provider": "openai"}
            ),
        },
        {
            "session_id": "session-legacy",
            "status": "completed",
            "start_time": 2.0,
            "device_info": json.dumps({"profile": "pro"}),
        },
    ]
    repo.get_video_recordings_map.return_value = {}
    repo.get_latest_video_recordings_map.return_value = {}
    repo.get_agent_trace_names_map.return_value = {}
    repo.get_llm_traces_for_profiles_map.return_value = {}

    monkeypatch.setattr(sessions_router, "session_repo", repo, raising=False)
    monkeypatch.setattr(
        sessions_router.media_service, "build_video_index", MagicMock(return_value={})
    )
    monkeypatch.setattr(
        sessions_router.media_service, "resolve_video_url", MagicMock(return_value=None)
    )

    rows = {row["session_id"]: row for row in await sessions_router.list_sessions()}

    assert rows["session-override"]["model_info"]["id"] == "gpt-5.1"
    assert rows["session-override"]["model_info"]["provider"] == "openai"
    assert rows["session-legacy"]["model_info"]["id"] == "gemini-3.8-flash"
    assert rows["session-legacy"]["model_info"]["provider"] == "google"


@pytest.mark.asyncio
@patch.object(ModelService, "_get_llm_provider_and_model", return_value=_CONFIGURED)
async def test_list_sessions_reports_the_override_for_a_profile_less_row(_configured, monkeypatch):
    """No resolvable profile must not hide a stored override.

    Regression: the row used to fall back to the global ``default_model_info``
    whenever the profile stayed unresolved, dropping the pinned model.
    """
    repo = MagicMock()
    repo.get_all_sessions.return_value = [
        {
            "session_id": "session-profileless-override",
            "status": "completed",
            "start_time": 1.0,
            "device_info": json.dumps({"llm_model": "gpt-5.1", "llm_provider": "openai"}),
        },
        {
            "session_id": "session-profileless-legacy",
            "status": "completed",
            "start_time": 2.0,
            "device_info": None,
        },
    ]
    repo.get_video_recordings_map.return_value = {}
    repo.get_latest_video_recordings_map.return_value = {}
    repo.get_agent_trace_names_map.return_value = {}
    repo.get_llm_traces_for_profiles_map.return_value = {}

    monkeypatch.setattr(sessions_router, "session_repo", repo, raising=False)
    monkeypatch.setattr(
        sessions_router.media_service, "build_video_index", MagicMock(return_value={})
    )
    monkeypatch.setattr(
        sessions_router.media_service, "resolve_video_url", MagicMock(return_value=None)
    )

    rows = {row["session_id"]: row for row in await sessions_router.list_sessions()}

    override = rows["session-profileless-override"]["model_info"]
    assert (override["id"], override["provider"]) == ("gpt-5.1", "openai")
    # The architecture stays Flash until the trace-name pass settles the profile.
    assert override["name"] == "Flash"
    # No keys at all: the untouched global default, built once per request.
    legacy = rows["session-profileless-legacy"]["model_info"]
    assert (legacy["id"], legacy["provider"]) == ("gemini-3.8-flash", "google")
    assert legacy == sessions_router.model_service.get_active_model_info()
