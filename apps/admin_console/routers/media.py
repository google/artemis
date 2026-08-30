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

import asyncio
from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from artemis.config import IMAGES_DIR, TRACES_PATH, WORKSPACE_ROOT

try:
    from admin_console.database.repositories.session_repository import session_repo
    from admin_console.services.media_service import media_service
except ImportError:
    from apps.admin_console.database.repositories.session_repository import session_repo
    from apps.admin_console.services.media_service import media_service

router = APIRouter(tags=["media"])

_VIDEO_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
}


def _allowed_media_roots() -> list[Path]:
    """Directories the media endpoints are permitted to serve from."""
    roots = []
    for root in (WORKSPACE_ROOT, TRACES_PATH):
        try:
            roots.append(Path(root).resolve())
        except OSError:
            continue
    return roots


def _resolve_media_path(raw_path: str, allowed_suffixes: set[str]) -> Path:
    """Resolve a client-supplied path strictly inside the allowed media roots.

    Every candidate is fully resolved (symlinks, `..`, mixed separators) and
    then re-checked against the allowed roots and an extension allowlist, so a
    crafted URL can never address source code, databases, or dotfiles.
    """
    roots = _allowed_media_roots()
    candidate = Path(raw_path)
    attempts = [candidate] if candidate.is_absolute() else [root / candidate for root in roots]
    for attempt in attempts:
        try:
            resolved = attempt.resolve(strict=True)
        except (OSError, ValueError):
            continue
        if not resolved.is_file():
            continue
        if not any(resolved.is_relative_to(root) for root in roots):
            continue
        if resolved.suffix.lower() not in allowed_suffixes:
            raise HTTPException(status_code=403, detail="File type is not served by the media API.")
        return resolved
    raise HTTPException(status_code=404, detail="Media file not found")


@router.get("/admin", response_class=HTMLResponse)
@router.get("/debug", response_class=HTMLResponse)
async def get_admin_index():
    admin_index = Path(__file__).resolve().parent.parent / "index.html"
    if admin_index.exists():
        return admin_index.read_text(encoding="utf-8")

    admin_index_alt = WORKSPACE_ROOT / "apps" / "admin_console" / "index.html"
    if admin_index_alt.exists():
        return admin_index_alt.read_text(encoding="utf-8")

    return "<h1>Admin Console index.html not found</h1>"


@router.get("/images/{image_name}")
@router.get("/api/images/{image_name}")
async def get_image(image_name: str):
    if not image_name.endswith(".jpg"):
        image_name += ".jpg"
    try:
        images_root = IMAGES_DIR.resolve()
        image_path = (images_root / image_name).resolve(strict=True)
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="Image not found")
    if not image_path.is_file() or not image_path.is_relative_to(images_root):
        raise HTTPException(status_code=404, detail="Image not found")

    return FileResponse(image_path, media_type="image/jpeg")


@router.get("/videos/{video_path:path}")
async def get_video(video_path: str):
    path = _resolve_media_path(video_path, set(_VIDEO_MEDIA_TYPES))
    return FileResponse(path, media_type=_VIDEO_MEDIA_TYPES[path.suffix.lower()])


@router.get("/api/sessions/{session_id}/video")
async def get_session_video(session_id: str):
    # Blocking work (sqlite, filesystem scan, possible ffmpeg conversion) runs
    # off the event loop.
    return await asyncio.to_thread(_get_session_video_sync, session_id)


def _get_session_video_sync(session_id: str):
    video_rec_map = session_repo.get_video_recordings_map()
    video_idx = media_service.build_video_index()
    row = session_repo.get_session_by_id(session_id)
    row_dict = dict(row) if row else {"session_id": session_id}
    recording = session_repo.get_video_recording_for_session(session_id)
    recording_status = str((recording or {}).get("status") or "")

    if recording_status in ("recording", "finalizing"):
        return {
            "session_id": session_id,
            "status": "processing",
            "has_video": False,
            "video_url": None,
            "video_segments": [],
            "retry_after_ms": 750,
        }

    if recording_status == "failed":
        v_url = media_service.resolve_video_url(row_dict, video_rec_map, video_idx)
        if not v_url:
            return {
                "session_id": session_id,
                "status": "failed",
                "has_video": False,
                "video_url": None,
                "video_segments": [],
                "message": recording.get("error") or "Recording finalization failed",
            }
        # If a video was recovered/found, update DB to ready and proceed to serve it
        session_repo.mark_recording_ready(session_id, v_url)

    v_url = media_service.resolve_video_url(row_dict, video_rec_map, video_idx)
    video_segments = media_service.resolve_video_segments(v_url)
    if v_url:
        version = int(float((recording or {}).get("end_time") or row_dict.get("end_time") or 0) * 1000)
        separator = "&" if "?" in v_url else "?"
        versioned_url = f"{v_url}{separator}v={version}" if version else v_url
        for segment in video_segments:
            segment_separator = "&" if "?" in segment["url"] else "?"
            segment["url"] = (
                f"{segment['url']}{segment_separator}v={version}" if version else segment["url"]
            )
        return {
            "session_id": session_id,
            "status": "ready",
            "has_video": True,
            "video_url": versioned_url,
            "video_segments": video_segments,
        }

    return {
        "session_id": session_id,
        "status": "unavailable",
        "has_video": False,
        "video_url": None,
        "video_segments": [],
    }


@router.get("/local_file")
async def get_local_file(path: str):
    p, media_type = media_service.get_safe_local_file(path)
    return FileResponse(p, media_type=media_type)


@router.get("/api/sessions/{session_id}/plan")
async def get_task_plan(session_id: str):
    return {"plan": media_service.get_task_plan_content(session_id)}


@router.get("/api/sessions/{session_id}/notes")
async def get_all_notes(session_id: str):
    return {"notes": media_service.get_session_notes_content(session_id)}
