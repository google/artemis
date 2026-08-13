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

"""Application settings and environment variable configuration management."""

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings

from artemis.config.constants import (
    DEFAULT_ADB_HOST,
    DEFAULT_ADB_PORT,
    DEFAULT_EXPLORER_VERSION,
    DEFAULT_MODEL,
    DEFAULT_PROFILE,
    ENV_DATA_ENGINE_DB_PATH,
    ENV_GCP_API_KEY,
    ENV_GEMINI_API_KEY,
    ENV_GOOGLE_API_KEY,
    ENV_OPEN_ROUTER_API_KEY,
    ENV_OPENAI_API_KEY,
    ENV_XAI_API_KEY,
    ExplorerVersion,
)
from artemis.config.paths import (
    GLOBAL_APP_DIR,
    get_app_dir,
    get_data_engine_db_path,
    get_default_traces_path,
    get_temp_dir,
)
from artemis.utils.logger import get_logger

# Load environment configuration from app dir if present, else current working dir
_global_env = GLOBAL_APP_DIR / ".env"
if _global_env.exists():
    load_dotenv(dotenv_path=_global_env, verbose=True)
else:
    load_dotenv(verbose=True)

logger = get_logger(__name__)


class Settings(BaseSettings):
    """Centralized ARTEMIS runtime settings loaded from environment and .env files."""

    # LLM Provider Authentication
    OPENAI_API_KEY: SecretStr | None = None
    GOOGLE_API_KEY: SecretStr | None = None
    GEMINI_API_KEY: SecretStr | None = None
    GCP_API_KEY: SecretStr | None = None
    ANTHROPIC_API_KEY: SecretStr | None = None
    XAI_API_KEY: SecretStr | None = None
    OPEN_ROUTER_API_KEY: SecretStr | None = None

    # Google Cloud Vision OCR Authentication
    OCR_API_KEY: SecretStr | None = None
    VISION_API_KEY: SecretStr | None = None
    API_KEY: SecretStr | None = None

    # Custom Provider Endpoints
    OPENAI_BASE_URL: str | None = None

    # Android ADB Connectivity
    ADB_HOST: str | None = Field(default=DEFAULT_ADB_HOST)
    ADB_PORT: int | None = Field(default=DEFAULT_ADB_PORT)
    ADB_DEVICE_SERIAL: str | None = None

    # Execution Defaults
    PROJECT_NAME: str | None = None
    ARTEMIS_DEFAULT_PROFILE: str = Field(default=DEFAULT_PROFILE)
    ARTEMIS_DEFAULT_MODEL: str = Field(default=DEFAULT_MODEL)

    # Explorer Tool Settings
    EXPLORER_VERSION: ExplorerVersion = Field(
        default=DEFAULT_EXPLORER_VERSION,
        description="Active version mode for the UI Explorer tool ('flash', 'pro', or 'ultra')",
    )
    EXPLORER_CACHING: bool = Field(
        default=True,
        description="Enable context caching when running multi-turn pro/ultra Explorer",
    )

    # Paths & Storage
    TRACES_PATH: Path = Field(default_factory=get_default_traces_path)
    DATA_ENGINE_DB_PATH: Path = Field(default_factory=get_data_engine_db_path)
    TEMP_PATH: Path = Field(default_factory=get_temp_dir)

    model_config = {"env_file": ".env", "extra": "ignore"}

    @model_validator(mode="after")
    def fallback_api_keys(self) -> "Settings":
        """Normalize Google / Gemini / GCP and OCR API keys for seamless interoperability."""
        if not self.GOOGLE_API_KEY:
            if self.GEMINI_API_KEY:
                self.GOOGLE_API_KEY = self.GEMINI_API_KEY
            elif self.GCP_API_KEY:
                self.GOOGLE_API_KEY = self.GCP_API_KEY

        # Fallback for OCR & Vision API keys
        if not self.OCR_API_KEY:
            if self.VISION_API_KEY:
                self.OCR_API_KEY = self.VISION_API_KEY
            elif self.API_KEY:
                self.OCR_API_KEY = self.API_KEY
        if not self.API_KEY and self.OCR_API_KEY:
            self.API_KEY = self.OCR_API_KEY
        return self

    def get_api_key(self, provider: str) -> SecretStr | None:
        """Get API key for a specified provider.

        Args:
            provider: Provider name (e.g., 'google', 'gemini', 'openai', 'ocr', 'vision').

        Returns:
            SecretStr containing the API key or None if not configured.
        """
        provider_lower = provider.lower()
        if provider_lower in ("google", "gemini", "vertexai"):
            return self.GOOGLE_API_KEY or self.GEMINI_API_KEY or self.GCP_API_KEY
        elif provider_lower in ("ocr", "vision", "google_vision"):
            return self.OCR_API_KEY or self.VISION_API_KEY or self.API_KEY or self.GOOGLE_API_KEY
        elif provider_lower == "openai":
            return self.OPENAI_API_KEY
        elif provider_lower == "anthropic":
            return self.ANTHROPIC_API_KEY
        elif provider_lower == "openrouter":
            return self.OPEN_ROUTER_API_KEY
        elif provider_lower == "xai":
            return self.XAI_API_KEY
        return None

    def set_api_key(self, provider: str, key: str, persist_to_env: bool = False) -> None:
        """Dynamically set an API key at runtime, optionally persisting to .env in the app dir.

        Args:
            provider: Target provider name.
            key: Secret API key string.
            persist_to_env: Whether to save the key to the app directory's .env file.
        """
        secret = SecretStr(key)
        provider_lower = provider.lower()
        env_key_name = None

        if provider_lower in ("google", "gemini", "vertexai"):
            self.GOOGLE_API_KEY = secret
            self.GEMINI_API_KEY = secret
            self.GCP_API_KEY = secret
            env_key_name = ENV_GOOGLE_API_KEY
            os.environ[ENV_GOOGLE_API_KEY] = key
            os.environ[ENV_GEMINI_API_KEY] = key
            os.environ[ENV_GCP_API_KEY] = key
        elif provider_lower == "openai":
            self.OPENAI_API_KEY = secret
            env_key_name = ENV_OPENAI_API_KEY
            os.environ[ENV_OPENAI_API_KEY] = key
        elif provider_lower == "openrouter":
            self.OPEN_ROUTER_API_KEY = secret
            env_key_name = ENV_OPEN_ROUTER_API_KEY
            os.environ[ENV_OPEN_ROUTER_API_KEY] = key
        elif provider_lower == "xai":
            self.XAI_API_KEY = secret
            env_key_name = ENV_XAI_API_KEY
            os.environ[ENV_XAI_API_KEY] = key

        if persist_to_env and env_key_name:
            try:
                env_file = get_app_dir() / ".env"
                lines = []
                if env_file.exists():
                    lines = env_file.read_text(encoding="utf-8").splitlines()
                lines = [line for line in lines if not line.startswith(f"{env_key_name}=")]
                lines.append(f"{env_key_name}={key}")
                env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
            except Exception as e:
                logger.warning(f"Could not persist {env_key_name} to .env: {e}")


# Singleton instance
settings = Settings()

# Synchronize DATA_ENGINE_DB_PATH in environment for external sub-processes/tools
if settings.DATA_ENGINE_DB_PATH:
    os.environ[ENV_DATA_ENGINE_DB_PATH] = str(settings.DATA_ENGINE_DB_PATH)
