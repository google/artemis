from unittest.mock import MagicMock

import pytest

from apps.admin_console.routers import media as media_router


@pytest.mark.asyncio
async def test_session_video_does_not_expose_recording_in_progress(monkeypatch):
    repo = MagicMock()
    repo.get_video_recordings_map.return_value = {}
    repo.get_session_by_id.return_value = {"session_id": "session-1", "status": "completed"}
    repo.get_video_recording_for_session.return_value = {
        "session_id": "session-1",
        "status": "recording",
        "local_video_path": "/tmp/recording.mkv",
    }
    monkeypatch.setattr(media_router, "session_repo", repo, raising=False)
    monkeypatch.setattr(media_router.media_service, "build_video_index", MagicMock(return_value={}))
    resolve = MagicMock(return_value="/videos/recording.mkv")
    monkeypatch.setattr(media_router.media_service, "resolve_video_url", resolve)

    response = await media_router.get_session_video("session-1")

    assert response["status"] == "processing"
    assert response["has_video"] is False
    assert response["video_url"] is None
    resolve.assert_not_called()


@pytest.mark.asyncio
async def test_session_video_publishes_only_finalized_versioned_media(monkeypatch):
    repo = MagicMock()
    repo.get_video_recordings_map.return_value = {"session-1": "/tmp/recording.mp4"}
    repo.get_session_by_id.return_value = {"session_id": "session-1", "status": "completed"}
    repo.get_video_recording_for_session.return_value = {
        "session_id": "session-1",
        "status": "ready",
        "end_time": 123.456,
        "local_video_path": "/tmp/recording.mp4",
    }
    monkeypatch.setattr(media_router, "session_repo", repo, raising=False)
    monkeypatch.setattr(media_router.media_service, "build_video_index", MagicMock(return_value={}))
    monkeypatch.setattr(
        media_router.media_service,
        "resolve_video_url",
        MagicMock(return_value="/videos/recording.mp4"),
    )
    monkeypatch.setattr(
        media_router.media_service,
        "resolve_video_segments",
        MagicMock(
            return_value=[
                {"url": "/videos/recording.mp4", "start": 0, "duration": 4, "width": 1080, "height": 1920}
            ]
        ),
    )

    response = await media_router.get_session_video("session-1")

    assert response["status"] == "ready"
    assert response["has_video"] is True
    assert response["video_url"] == "/videos/recording.mp4?v=123456"
    assert response["video_segments"][0]["url"] == "/videos/recording.mp4?v=123456"


@pytest.mark.asyncio
async def test_session_video_surfaces_terminal_recording_failure(monkeypatch):
    repo = MagicMock()
    repo.get_video_recordings_map.return_value = {}
    repo.get_session_by_id.return_value = {"session_id": "session-1", "status": "failed"}
    repo.get_video_recording_for_session.return_value = {
        "session_id": "session-1",
        "status": "failed",
        "error": "ffmpeg failed",
    }
    monkeypatch.setattr(media_router, "session_repo", repo, raising=False)
    monkeypatch.setattr(media_router.media_service, "build_video_index", MagicMock(return_value={}))

    response = await media_router.get_session_video("session-1")

    assert response["status"] == "failed"
    assert response["message"] == "ffmpeg failed"
    assert response["video_url"] is None
