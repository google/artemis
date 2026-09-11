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

from datetime import datetime
from enum import Enum
from pydantic import BaseModel, Field


class SessionStatus(str, Enum):
    PROVISIONING = "PROVISIONING"
    ACTIVE = "ACTIVE"
    IDLE = "IDLE"
    TERMINATING = "TERMINATING"
    TERMINATED = "TERMINATED"
    ERROR = "ERROR"


class CreateSessionRequest(BaseModel):
    device_preset: str = Field(default="pixel_7_pro", description="Target Cuttlefish device configuration")
    initial_task: str | None = Field(default=None, description="Optional initial prompt/task for Artemis")
    ttl_minutes: int = Field(default=60, ge=5, le=480, description="Session time to live in minutes")


class SessionRecord(BaseModel):
    user_id: str
    session_id: str
    artemis_container_id: str | None = None
    artemis_container_name: str | None = None
    artemis_internal_ip: str | None = None
    cuttlefish_instance_id: str | None = None
    cuttlefish_adb_port: int | None = None
    cuttlefish_webrtc_port: int | None = None
    status: SessionStatus = SessionStatus.PROVISIONING
    created_at: datetime
    last_heartbeat_at: datetime
    expires_at: datetime
    error_message: str | None = None


class SessionResponse(BaseModel):
    session_id: str
    status: SessionStatus
    artemis_api_url: str
    artemis_stream_url: str
    cuttlefish_webrtc_url: str
    cuttlefish_adb_endpoint: str
    created_at: datetime
    expires_at: datetime
    message: str = "Session is ready"


class SessionListItem(BaseModel):
    session_id: str
    status: SessionStatus
    created_at: datetime
    expires_at: datetime
    artemis_container_id: str | None


class HeartbeatResponse(BaseModel):
    session_id: str
    status: SessionStatus
    last_heartbeat_at: datetime
    expires_at: datetime
    message: str = "Heartbeat acknowledged"
