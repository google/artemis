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

from pydantic import BaseModel, Field


class OTPRequest(BaseModel):
    identifier: str = Field(
        ..., description="User phone number or email address", example="user@example.com"
    )


class OTPRequestResponse(BaseModel):
    message: str
    identifier: str
    expires_in_seconds: int


class OTPVerify(BaseModel):
    identifier: str = Field(
        ..., description="User phone number or email address", example="user@example.com"
    )
    code: str = Field(..., description="6-digit verification code", example="123456")


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    expires_in: int


class UserResponse(BaseModel):
    user_id: str
    authenticated: bool
    roles: list[str] = ["developer"]


class SessionValidationResponse(BaseModel):
    valid: bool
    user_id: str | None = None
    session_id: str | None = None
    message: str = "Authorized"
