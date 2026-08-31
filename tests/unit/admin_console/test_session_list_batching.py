from unittest.mock import MagicMock

import pytest

from apps.admin_console.routers import sessions as sessions_router


@pytest.mark.asyncio
async def test_list_sessions_uses_batched_history_metadata(monkeypatch):
    repo = MagicMock()
    repo.get_all_sessions.return_value = [
        {
            "session_id": "session-1",
            "status": "completed",
            "start_time": 1.0,
            "device_info": '{"profile": "flash"}',
        },
        {
            "session_id": "session-2",
            "status": "completed",
            "start_time": 2.0,
            "device_info": '{"profile": "pro"}',
        },
        {
            "session_id": "session-3",
            "status": "completed",
            "start_time": 3.0,
            "device_info": None,
        },
    ]
    repo.get_video_recordings_map.return_value = {}
    repo.get_latest_video_recordings_map.return_value = {
        "session-1": {"status": "ready"},
        "session-2": {"status": "unavailable"},
    }
    repo.get_llm_traces_for_profiles_map.return_value = {
        "session-3": ['{"model": "gemini-3.7-pro"}']
    }
    repo.get_agent_trace_names_map.return_value = {}

    monkeypatch.setattr(sessions_router, "session_repo", repo, raising=False)
    monkeypatch.setattr(
        sessions_router.media_service, "build_video_index", MagicMock(return_value={})
    )
    monkeypatch.setattr(
        sessions_router.media_service, "resolve_video_url", MagicMock(return_value=None)
    )

    result = await sessions_router.list_sessions()

    assert len(result) == 3
    repo.get_latest_video_recordings_map.assert_called_once_with()
    repo.get_llm_traces_for_profiles_map.assert_called_once_with(["session-3"])
    repo.get_agent_trace_names_map.assert_called_once_with()
    repo.get_video_recording_for_session.assert_not_called()
    repo.get_llm_traces_for_profile.assert_not_called()
    repo.get_agent_trace_names.assert_not_called()


@pytest.mark.asyncio
async def test_list_sessions_extracts_device_serial(monkeypatch):
    repo = MagicMock()
    repo.get_all_sessions.return_value = [
        {
            "session_id": "session-dev-1",
            "status": "completed",
            "start_time": 1.0,
            "device_info": '{"device_id": "63191FDKX00062", "profile": "flash"}',
        },
        {
            "session_id": "session-dev-2",
            "status": "completed",
            "start_time": 2.0,
            "device_info": None,
        },
    ]
    repo.get_video_recordings_map.return_value = {}
    repo.get_latest_video_recordings_map.return_value = {
        "session-dev-2": {"status": "ready", "device_id": "emulator-5554"},
    }
    repo.get_llm_traces_for_profiles_map.return_value = {}
    repo.get_agent_trace_names_map.return_value = {}

    monkeypatch.setattr(sessions_router, "session_repo", repo, raising=False)
    monkeypatch.setattr(
        sessions_router.media_service, "build_video_index", MagicMock(return_value={})
    )
    monkeypatch.setattr(
        sessions_router.media_service, "resolve_video_url", MagicMock(return_value=None)
    )

    result = await sessions_router.list_sessions()

    assert len(result) == 2
    assert result[0]["device_serial"] == "63191FDKX00062"
    assert result[0]["device_id"] == "63191FDKX00062"
    assert result[1]["device_serial"] == "emulator-5554"
    assert result[1]["device_id"] == "emulator-5554"
