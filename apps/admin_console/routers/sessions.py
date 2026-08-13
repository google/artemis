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

import time
from uuid import UUID
from fastapi import APIRouter, HTTPException

try:
    from admin_console.core.config import DB_PATH, TRACES_PATH
    from admin_console.core.state import state
    from admin_console.database.repositories.session_repository import session_repo
    from admin_console.database.repositories.trace_repository import trace_repo
    from admin_console.services.media_service import media_service
    from admin_console.services.model_service import model_service
except ImportError:
    from apps.admin_console.core.config import DB_PATH, TRACES_PATH
    from apps.admin_console.core.state import state
    from apps.admin_console.database.repositories.session_repository import session_repo
    from apps.admin_console.database.repositories.trace_repository import trace_repo
    from apps.admin_console.services.media_service import media_service
    from apps.admin_console.services.model_service import model_service


router = APIRouter(tags=["sessions"])


@router.get("/api/sessions")
async def list_sessions():
    try:
        rows = session_repo.get_all_sessions()
        video_rec_map = session_repo.get_video_recordings_map()
        video_idx = media_service.build_video_index()

        orphaned_ids = []
        result = []

        for row_dict in rows:
            s_id = str(row_dict.get("session_id"))
            if row_dict.get("status") == "running":
                is_active = s_id in state.active_connections or (
                    state.is_running and s_id == str(state.active_session_id)
                )
                if not is_active:
                    row_dict["status"] = "failed"
                    row_dict["end_time"] = time.time()
                    orphaned_ids.append(s_id)

            row_dict["video_url"] = media_service.resolve_video_url(
                row_dict, video_rec_map, video_idx
            )

            llm_traces = session_repo.get_llm_traces_for_profile(s_id)
            agent_names = session_repo.get_agent_trace_names(s_id)
            sess_profile = model_service.resolve_session_profile(
                row_dict, llm_traces, state.current_profile, agent_names=agent_names
            )

            row_dict["model_info"] = model_service.get_active_model_info(sess_profile)
            result.append(row_dict)

        if orphaned_ids:
            try:
                harvested = session_repo.harvest_orphaned_sessions(orphaned_ids)
                print(f"[list_sessions] Auto-harvested {harvested} orphaned running session(s).")
            except Exception as e:
                print(f"[list_sessions] Failed to update orphaned sessions: {e}")

        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/sessions/{session_id}/tree")
async def get_tree(session_id: str):
    try:
        return trace_repo.get_trace_tree(session_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/sessions/{session_id}/background_tasks")
async def get_session_background_tasks(session_id: str):
    try:
        return session_repo.get_background_tasks(session_id)
    except Exception:
        return []


@router.post("/api/cleanup")
async def cleanup_history_endpoint():
    try:
        from artemis.data_engine.storage import StorageManager

        storage = StorageManager(DB_PATH, TRACES_PATH)
        storage.clear_all_data()
        return {
            "status": "success",
            "message": "History cleaned up successfully",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/sessions/{session_id}/delete")
async def delete_session_endpoint(session_id: str):
    try:
        from artemis.data_engine.storage import StorageManager

        storage = StorageManager(DB_PATH, TRACES_PATH)
        storage.delete_session(UUID(session_id))
        return {
            "status": "success",
            "message": f"Session {session_id} deleted successfully",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
