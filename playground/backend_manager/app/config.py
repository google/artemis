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

import os
from pathlib import Path
from typing import Self
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PRODUCTION_ENV = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="allow")

    # Application & Server Settings
    APP_NAME: str = "Artemis Backend Manager"
    APP_ENV: str = "development"
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    DEBUG: bool = True

    # Security & JWT Token Config
    # Required: the empty default is validated and rejected, so every deployment must set
    # its own secret. (A default keeps `Settings()` well-typed for static type checkers.)
    JWT_SECRET_KEY: str = Field(default="", min_length=32, validate_default=True)
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24  # 24 hours

    # OTP Authentication Config
    OTP_EXPIRE_SECONDS: int = 300  # 5 minutes
    OTP_MAX_ATTEMPTS: int = 5  # Wrong guesses allowed before the issued code is locked
    # Minimum wait before a new code replaces the current one. Without it, requesting a
    # fresh code after every lockout would reset OTP_MAX_ATTEMPTS indefinitely.
    OTP_RESEND_COOLDOWN_SECONDS: int = 60
    # Fixed code issued instead of a random one, for local development only.
    # Leave empty in any shared deployment; it is rejected when APP_ENV=production.
    DEV_MOCK_OTP: str = ""

    # Docker Socket & Ephemeral Container Settings
    DOCKER_SOCKET_PATH: str = "unix://var/run/docker.sock"
    ARTEMIS_IMAGE: str = "artemis:latest"
    DOCKER_NETWORK: str = "artemis-net"
    ARTEMIS_CONTAINER_PREFIX: str = "artemis-session"
    ARTEMIS_CPU_LIMIT: float = 2.0
    ARTEMIS_MEMORY_LIMIT: str = "2g"

    # Cloud Orchestrator & Cuttlefish Host Settings
    CLOUD_ORCHESTRATOR_URL: str = os.getenv(
        "CLOUD_ORCHESTRATOR_URL", "http://cloud-orchestrator:2081"
    )
    CUTTLEFISH_HOST_GATEWAY: str = os.getenv("CUTTLEFISH_HOST_GATEWAY", "cloud-orchestrator")
    CUTTLEFISH_START_ADB_PORT: int = 6520
    CUTTLEFISH_START_WEBRTC_PORT: int = 8443

    # Google Cloud BigQuery Settings
    GCP_PROJECT_ID: str = os.getenv("GOOGLE_CLOUD_PROJECT", "artemis-cloud-project")
    BQ_DATASET: str = "artemis_orchestration"
    BQ_TABLE: str = "sessions_registry"
    USE_LOCAL_FALLBACK_DB: bool = True  # In-memory fallback if BigQuery credentials are unset

    # Session Lifecycle & Auto-Reaper
    SESSION_TTL_MINUTES: int = 60
    SESSION_HEARTBEAT_TIMEOUT_SECONDS: int = 120
    REAPER_CHECK_INTERVAL_SECONDS: int = 30

    # Paths
    STATIC_DIR: Path = Path(__file__).resolve().parent / "static"

    @model_validator(mode="after")
    def _reject_mock_otp_in_production(self) -> Self:
        if self.APP_ENV.strip().lower() == PRODUCTION_ENV and self.DEV_MOCK_OTP:
            raise ValueError(
                "DEV_MOCK_OTP must be empty when APP_ENV=production: "
                "a fixed code would let anyone sign in as any user."
            )
        return self


settings = Settings()
