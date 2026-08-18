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

from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

try:
    from admin_console.core.config import IMAGES_DIR, WORKSPACE_ROOT
    from admin_console.services.media_service import media_service
except ImportError:
    from apps.admin_console.core.config import IMAGES_DIR, WORKSPACE_ROOT
    from apps.admin_console.services.media_service import media_service

router = APIRouter(tags=["media"])


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
    image_path = IMAGES_DIR / image_name
    if not image_path.exists():
        raise HTTPException(status_code=404, detail="Image not found")

    return FileResponse(image_path, media_type="image/jpeg")


@router.get("/videos/{video_path:path}")
async def get_video(video_path: str):
    path = Path(video_path)
    if not path.exists():
        if not path.is_absolute():
            candidate = WORKSPACE_ROOT / path
            if candidate.exists():
                path = candidate
            else:
                candidate_root = Path("/" + video_path.lstrip("/"))
                if candidate_root.exists():
                    path = candidate_root
                else:
                    path = candidate

    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Video not found at {path}")

    suffix = path.suffix.lower()
    media_type = "video/mp4"
    if suffix == ".webm":
        media_type = "video/webm"
    elif suffix == ".mkv":
        media_type = "video/x-matroska"

    return FileResponse(path, media_type=media_type)


@router.get("/api/sessions/{session_id}/video")
async def get_session_video(session_id: str):
    try:
        from admin_console.database.repositories.session_repository import session_repo
    except ImportError:
        from apps.admin_console.database.repositories.session_repository import session_repo

    video_rec_map = session_repo.get_video_recordings_map()
    video_idx = media_service.build_video_index()
    row = session_repo.get_session_by_id(session_id)
    row_dict = dict(row) if row else {"session_id": session_id}
    v_url = media_service.resolve_video_url(row_dict, video_rec_map, video_idx)
    return {
        "session_id": session_id,
        "has_video": bool(v_url),
        "video_url": v_url,
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
