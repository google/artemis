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
import json
import time
from uuid import UUID
from fastapi import APIRouter, HTTPException

from artemis.config import DB_PATH, TRACES_PATH

try:
    from admin_console.core.state import state
    from admin_console.database.repositories.session_repository import session_repo
    from admin_console.database.repositories.trace_repository import trace_repo
    from admin_console.services.media_service import media_service
    from admin_console.services.model_service import model_service
except ImportError:
    from apps.admin_console.core.state import state
    from apps.admin_console.database.repositories.session_repository import session_repo
    from apps.admin_console.database.repositories.trace_repository import trace_repo
    from apps.admin_console.services.media_service import media_service
    from apps.admin_console.services.model_service import model_service


router = APIRouter(tags=["sessions"])


@router.get("/api/sessions")
async def list_sessions():
    # The body does blocking work (sqlite queries, filesystem scans, ffmpeg
    # conversion on first sight of a new recording), so run it off the event
    # loop — the frontend polls this endpoint and it must not stall other
    # requests.
    try:
        return await asyncio.to_thread(_list_sessions_sync)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _list_sessions_sync():
    rows = session_repo.get_all_sessions()
    video_rec_map = session_repo.get_video_recordings_map()
    latest_recordings = session_repo.get_latest_video_recordings_map()
    agent_names_by_session = session_repo.get_agent_trace_names_map()
    video_idx = media_service.build_video_index()
    default_model_info = model_service.get_active_model_info()

    orphaned_ids = []
    unresolved_profiles = []
    result = []

    try:
        from artemis.runtime import DeviceExecutionLock
        active_owners = DeviceExecutionLock.get_active_owners()
        active_owner_sids = {
            str(owner.session_id)
            for owner in active_owners.values()
            if owner.session_id
        }
    except Exception:
        active_owner_sids = set()

    for row_dict in rows:
        s_id = str(row_dict.get("session_id"))
        recording = latest_recordings.get(s_id)
        d_info_raw = row_dict.get("device_info")
        device_id = None
        if d_info_raw:
            try:
                d_info = json.loads(d_info_raw) if isinstance(d_info_raw, str) else d_info_raw
                if isinstance(d_info, dict):
                    device_id = d_info.get("device_id") or d_info.get("device_serial")
            except Exception:
                pass
        if not device_id and recording:
            device_id = recording.get("device_id")
        row_dict["device_id"] = device_id
        row_dict["device_serial"] = device_id

        if row_dict.get("status") == "running":
            is_active = (
                s_id in active_owner_sids
                or s_id in state.active_connections
                or (state.is_running and s_id == str(state.active_session_id))
            )
            worker_is_alive = session_repo.process_is_alive(row_dict.get("pid"))
            if not is_active and not worker_is_alive:
                row_dict["status"] = "failed"
                row_dict["end_time"] = time.time()
                orphaned_ids.append(s_id)

        recording_status = str((recording or {}).get("status") or "unavailable")
        resolved_v_url = (
            media_service.resolve_video_url(row_dict, video_rec_map, video_idx)
            if recording_status != "recording"
            else None
        )
        if resolved_v_url:
            recording_status = "ready"
            row_dict["video_url"] = resolved_v_url
        else:
            row_dict["video_url"] = None
        row_dict["recording_status"] = recording_status

        agent_names = agent_names_by_session.get(s_id, [])
        sess_profile = model_service.resolve_session_profile(
            row_dict, None, state.current_profile, agent_names=agent_names
        )
        row_dict["model_info"] = (
            model_service.get_active_model_info(sess_profile)
            if sess_profile
            else default_model_info
        )
        if not sess_profile:
            unresolved_profiles.append(row_dict)
        result.append(row_dict)

    # Most sessions carry a profile in device_info or an agent trace name.
    # Only inspect the much larger LLM payload for legacy rows that remain
    # ambiguous after those cheap checks.
    if unresolved_profiles:
        unresolved_ids = [str(row.get("session_id")) for row in unresolved_profiles]
        llm_traces_by_session = session_repo.get_llm_traces_for_profiles_map(unresolved_ids)
        for row_dict in unresolved_profiles:
            s_id = str(row_dict.get("session_id"))
            llm_traces = llm_traces_by_session.get(s_id)
            if not llm_traces:
                continue
            sess_profile = model_service.resolve_session_profile(
                row_dict, llm_traces, state.current_profile
            )
            if sess_profile:
                row_dict["model_info"] = model_service.get_active_model_info(sess_profile)

    if orphaned_ids:
        try:
            harvested = session_repo.harvest_orphaned_sessions(orphaned_ids)
            print(f"[list_sessions] Auto-harvested {harvested} orphaned running session(s).")
            try:
                from mcp_server.utils import trace_store

                for o_id in orphaned_ids:
                    if trace_store.read_status(str(o_id)):
                        trace_store.update_trace_status(
                            str(o_id),
                            "failed",
                            error="Process terminated unexpectedly (auto-harvested).",
                        )
            except Exception:
                pass
        except Exception as e:
            print(f"[list_sessions] Failed to update orphaned sessions: {e}")

    return result


@router.get("/api/sessions/{session_id}")
async def get_session_details(session_id: str):
    """Retrieve details for a single automation session."""
    row = session_repo.get_session_by_id(session_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    return dict(row)


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


@router.get("/api/sessions/{session_id}/startup_progress")
async def get_session_startup_progress(session_id: str):
    try:
        return state.get_startup_progress(session_id)
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
