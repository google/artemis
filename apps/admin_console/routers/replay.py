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

import traceback
from fastapi import APIRouter, HTTPException

try:
    from admin_console.core.config import WORKSPACE_ROOT
    from admin_console.schemas.task_schema import ReplayRequest
    from admin_console.replay_manager import ReplayManager
except ImportError:
    from apps.admin_console.core.config import WORKSPACE_ROOT
    from apps.admin_console.schemas.task_schema import ReplayRequest
    from apps.admin_console.replay_manager import ReplayManager

replay_manager = ReplayManager(WORKSPACE_ROOT)


router = APIRouter(tags=["replay"])


@router.get("/api/devices")
async def list_devices():
    """Dynamically queries the ADB server for connected Android devices."""
    return replay_manager.list_devices()


@router.get("/api/replay/tools")
async def get_replay_tools():
    """Returns the list of registered tools available for replay."""
    return replay_manager.get_replay_tools()


@router.get("/api/replay/config")
async def get_replay_config(tool_name: str = "ask_explorer"):
    """Returns the parameter list and types of the selected tool logic."""
    try:
        return replay_manager.get_replay_config(tool_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/sessions/{session_id}/replay_steps")
async def get_replay_steps(session_id: str):
    """Loads and formats metadata for all chunked steps in the session."""
    try:
        return replay_manager.get_replay_steps(session_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/sessions/{session_id}/steps/{step_number}/replay")
async def trigger_step_replay_endpoint(session_id: str, step_number: int, req: ReplayRequest):
    """Triggers sandbox execution of step replay and returns the resulting traces trees."""
    try:
        return await replay_manager.run_step_replay(
            session_id=session_id,
            step_number=step_number,
            device_id=req.device_id,
            user_submits=req.user_submits,
            tool_name=req.tool_name,
            replay_id=req.replay_id,
        )
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/sessions/{session_id}/steps/{step_number}/replay_traces")
async def get_step_replay_traces_endpoint(
    session_id: str, step_number: int, tool_name: str = "ask_explorer"
):
    """Retrieves previously generated step replay traces if they exist."""
    try:
        return replay_manager.get_step_replay_traces(session_id, step_number, tool_name)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
