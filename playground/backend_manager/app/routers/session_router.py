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

from fastapi import APIRouter, Depends, HTTPException, status
from app.auth.jwt_handler import get_current_user
from app.schemas.session_schema import (
    CreateSessionRequest,
    HeartbeatResponse,
    SessionListItem,
    SessionResponse,
)
from app.services.session_manager import session_manager

router = APIRouter(prefix="/api/v1/sessions", tags=["Session & Container Management"])


@router.post("/create", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
async def create_new_session(
    payload: CreateSessionRequest,
    user_id: str = Depends(get_current_user),
):
    """Create a new session, provision Cuttlefish AVD and Artemis container, and establish ADB link."""
    try:
        session = await session_manager.create_session(user_id=user_id, request=payload)
        return session
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create session: {str(e)}",
        )


@router.get("", response_model=list[SessionListItem])
async def list_user_sessions(user_id: str = Depends(get_current_user)):
    """List all active sessions belonging to the authenticated user."""
    return await session_manager.list_user_sessions(user_id)


@router.get("/{session_id}", response_model=SessionResponse)
async def get_session_details(
    session_id: str,
    user_id: str = Depends(get_current_user),
):
    """Retrieve details and URLs for an existing session."""
    session = await session_manager.get_session(session_id, user_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found or unauthorized",
        )
    return session


@router.post("/{session_id}/heartbeat", response_model=HeartbeatResponse)
async def session_heartbeat(
    session_id: str,
    user_id: str = Depends(get_current_user),
):
    """Extend session expiration and report client liveness."""
    res = await session_manager.heartbeat(session_id, user_id)
    if not res:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Active session {session_id} not found",
        )
    return res


@router.delete("/{session_id}", status_code=status.HTTP_200_OK)
async def terminate_session(
    session_id: str,
    user_id: str = Depends(get_current_user),
):
    """Terminate the session, destroy Artemis container and Cuttlefish AVD."""
    success = await session_manager.terminate_session(session_id, user_id=user_id, reason="User terminated")
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found or unauthorized",
        )
    return {"message": f"Session {session_id} successfully terminated"}
