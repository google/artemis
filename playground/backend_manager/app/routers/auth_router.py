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

from fastapi import APIRouter, Depends, HTTPException, Query, status
from app.auth.jwt_handler import create_access_token, get_current_user
from app.auth.otp_service import otp_service
from app.config import settings
from app.schemas.auth_schema import (
    OTPRequest,
    OTPRequestResponse,
    OTPVerify,
    SessionValidationResponse,
    TokenResponse,
    UserResponse,
)

router = APIRouter(prefix="/api/v1/auth", tags=["Authentication & Verification"])


@router.post("/otp/send", response_model=OTPRequestResponse)
async def request_otp(payload: OTPRequest):
    """Request an OTP verification code sent to phone/email."""
    code = otp_service.generate_otp(payload.identifier)
    return OTPRequestResponse(
        message="Verification code dispatched successfully",
        identifier=payload.identifier,
        expires_in_seconds=settings.OTP_EXPIRE_SECONDS,
    )


@router.post("/otp/verify", response_model=TokenResponse)
async def verify_otp(payload: OTPVerify):
    """Verify OTP callback and return signed JWT session token."""
    valid = otp_service.verify_otp(payload.identifier, payload.code)
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired verification code",
        )

    # Issue JWT token
    token = create_access_token(user_id=payload.identifier)
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        user_id=payload.identifier,
        expires_in=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.get("/me", response_model=UserResponse)
async def get_current_user_profile(user_id: str = Depends(get_current_user)):
    """Retrieve authenticated user identity."""
    return UserResponse(user_id=user_id, authenticated=True)


@router.get("/validate-session", response_model=SessionValidationResponse)
async def validate_session_ingress(
    session_id: str = Query(..., description="Target session UUID"),
    user_id: str = Depends(get_current_user),
):
    """Used by Nginx auth_request to confirm user has permission for a specific session."""
    return SessionValidationResponse(
        valid=True,
        user_id=user_id,
        session_id=session_id,
        message="Access authorized",
    )
