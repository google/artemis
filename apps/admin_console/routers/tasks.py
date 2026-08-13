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
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

try:
    from admin_console.core.state import state
    from admin_console.database.repositories.session_repository import session_repo
    from admin_console.schemas.task_schema import RunRequest
    from admin_console.services.ipc_service import ipc_service
    from admin_console.services.model_service import model_service
    from admin_console.services.task_queue_service import task_queue_service
except ImportError:
    from apps.admin_console.core.state import state
    from apps.admin_console.database.repositories.session_repository import session_repo
    from apps.admin_console.schemas.task_schema import RunRequest
    from apps.admin_console.services.ipc_service import ipc_service
    from apps.admin_console.services.model_service import model_service
    from apps.admin_console.services.task_queue_service import task_queue_service


router = APIRouter(tags=["tasks"])


@router.post("/api/run")
async def run_task(request: RunRequest):
    incoming_goals = []
    if request.goals:
        incoming_goals = request.goals
    elif request.goal:
        incoming_goals = [request.goal]

    if not incoming_goals:
        raise HTTPException(
            status_code=400,
            detail="Either 'goal' or 'goals' list must be provided.",
        )

    return await task_queue_service.enqueue_tasks(
        incoming_goals, profile=request.profile or "flash"
    )


@router.post("/api/stop")
async def stop_task(all: bool = False):
    stopped = task_queue_service.stop_tasks(clear_all=all)
    if stopped:
        return {"status": "stopped"}
    return {"status": "no_running_task"}


@router.post("/api/resume")
async def resume_task():
    resumed = task_queue_service.resume_task()
    if resumed:
        return {"status": "resumed"}
    return {"status": "not_paused"}


@router.get("/api/status")
async def get_status():
    latest_session = session_repo.get_latest_session()
    latest_session_id = latest_session.get("session_id") if latest_session else None
    bg_tasks = session_repo.get_background_tasks(latest_session_id) if latest_session_id else []

    is_running = state.is_running
    running_sid = state.active_session_id or (
        session_repo.get_running_session_id() if is_running else None
    )
    active_profile = state.current_profile
    if not active_profile and (running_sid or latest_session_id):
        check_sid = running_sid or latest_session_id
        sess_row = session_repo.get_session_by_id(check_sid)
        if sess_row:
            llm_traces = session_repo.get_llm_traces_for_profile(check_sid)
            agent_names = session_repo.get_agent_trace_names(check_sid)
            active_profile = model_service.resolve_session_profile(
                sess_row, llm_traces, agent_names=agent_names
            )

    model_info = model_service.get_active_model_info(active_profile)
    queue_data = state.queue_tasks

    if state.current_process:
        if state.current_process.returncode is None:
            is_paused = state.is_paused
            return {
                "status": "paused" if is_paused else "running",
                "goal": state.current_goal,
                "pid": state.current_process.pid,
                "session_id": running_sid,
                "background_tasks": bg_tasks,
                "queue": queue_data,
                "model_info": model_info,
            }
        else:
            completed_status = {
                "status": "completed",
                "goal": state.current_goal,
                "returncode": state.current_process.returncode,
                "session_id": latest_session_id,
                "background_tasks": bg_tasks,
                "queue": queue_data,
                "model_info": model_info,
            }
            state.current_process = None
            return completed_status

    if latest_session_id and str(latest_session_id) in state.active_connections:
        conn_info = state.active_connections[str(latest_session_id)]
        is_paused = state.is_paused
        conn_profile = conn_info.get("profile") or active_profile
        conn_model_info = model_service.get_active_model_info(conn_profile)
        return {
            "status": "paused" if is_paused else "running",
            "goal": conn_info.get("goal"),
            "pid": conn_info.get("pid"),
            "session_id": latest_session_id,
            "background_tasks": bg_tasks,
            "queue": queue_data,
            "model_info": conn_model_info,
        }

    return {
        "status": "idle",
        "session_id": latest_session_id,
        "background_tasks": bg_tasks,
        "queue": queue_data,
        "model_info": model_info,
    }


@router.get("/api/stream/{session_id}")
async def stream_events(session_id: str, client: str | None = None):
    async def event_generator():
        queue = asyncio.Queue()

        def callback(event_type, data):
            try:
                # Filter events by session_id when subscribed to a specific session
                if session_id and session_id not in ("all", "active"):
                    evt_session_id = None
                    if isinstance(data, dict):
                        evt_session_id = data.get("session_id")

                    if evt_session_id and str(evt_session_id) != str(session_id):
                        return

                    if (
                        not evt_session_id
                        and state.active_session_id
                        and str(state.active_session_id) != str(session_id)
                    ):
                        return

                sanitized_data = ipc_service.sanitize_event_data(event_type, data)
                loop = asyncio.get_running_loop()
                loop.call_soon_threadsafe(queue.put_nowait, (event_type, sanitized_data))
            except RuntimeError:
                pass

        state.add_subscriber(callback)
        yield f'event: info\ndata: {{"message": "Subscribed to session {session_id}"}}\n\n'

        try:
            while True:
                try:
                    event_type, data = await asyncio.wait_for(queue.get(), timeout=5.0)
                    yield f"event: {event_type}\ndata: {json.dumps(data, default=str)}\n\n"
                except TimeoutError:
                    yield "event: keep-alive\ndata: {}\n\n"
        except asyncio.CancelledError:
            state.remove_subscriber(callback)

    return StreamingResponse(event_generator(), media_type="text/event-stream")
