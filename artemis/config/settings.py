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
    ENV_ANTHROPIC_API_KEY,
    ENV_DATA_ENGINE_DB_PATH,
    ENV_GCP_API_KEY,
    ENV_GEMINI_API_KEY,
    ENV_GOOGLE_API_KEY,
    ENV_OCR_API_KEY,
    ENV_OPEN_ROUTER_API_KEY,
    ENV_OPENAI_API_KEY,
    ENV_VISION_API_KEY,
    ENV_XAI_API_KEY,
    ExplorerVersion,
)
from artemis.config.paths import (
    GLOBAL_APP_DIR,
    ROOT_DIR,
    get_app_dir,
    get_data_engine_db_path,
    get_default_traces_path,
    get_temp_dir,
)
from artemis.utils.logger import get_logger

# Load environment configuration from workspace root dir and app dir if present
load_dotenv(dotenv_path=ROOT_DIR / ".env", verbose=True)
_global_env = GLOBAL_APP_DIR / ".env"
if _global_env.exists() and _global_env.resolve() != (ROOT_DIR / ".env").resolve():
    load_dotenv(dotenv_path=_global_env, verbose=True)

logger = get_logger(__name__)


def is_placeholder_key(val: str | SecretStr | None) -> bool:
    """Check if an API credential is empty or an unconfigured placeholder value."""
    if val is None:
        return True
    raw_str = val.get_secret_value() if isinstance(val, SecretStr) else str(val)
    raw_str = raw_str.strip().lower()
    if not raw_str:
        return True
    if raw_str in (
        "api_key",
        "your_api_key",
        "your_api_key_here",
        "your_gemini_api_key_here",
        "your_google_cloud_vision_api_key_here",
        "your_openai_api_key_here",
        "your_anthropic_api_key_here",
        "your_openrouter_api_key_here",
        "your_xai_api_key_here",
        "none",
        "empty",
        "null",
        "undefined",
    ):
        return True
    if (
        (raw_str.startswith("your_") and raw_str.endswith("_here"))
        or (raw_str.startswith("<") and raw_str.endswith(">"))
        or (raw_str.startswith("[") and raw_str.endswith("]"))
    ):
        return True
    return False


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
        """Normalize Google / Gemini / GCP and OCR API keys and filter placeholders."""
        # Sanitize any placeholder values loaded from environment or .env
        for attr in (
            "OPENAI_API_KEY",
            "GOOGLE_API_KEY",
            "GEMINI_API_KEY",
            "GCP_API_KEY",
            "ANTHROPIC_API_KEY",
            "XAI_API_KEY",
            "OPEN_ROUTER_API_KEY",
            "OCR_API_KEY",
            "VISION_API_KEY",
            "API_KEY",
        ):
            val = getattr(self, attr, None)
            if val and is_placeholder_key(val):
                setattr(self, attr, None)

        if not self.GOOGLE_API_KEY:
            if self.GEMINI_API_KEY:
                self.GOOGLE_API_KEY = self.GEMINI_API_KEY
            elif self.GCP_API_KEY:
                self.GOOGLE_API_KEY = self.GCP_API_KEY

        # Fallback for OCR & Vision API keys
        if not self.OCR_API_KEY and self.VISION_API_KEY:
            self.OCR_API_KEY = self.VISION_API_KEY
        return self

    def get_api_key(self, provider: str) -> SecretStr | None:
        """Get API key for a specified provider.

        Args:
            provider: Provider name (e.g., 'google', 'gemini', 'openai', 'ocr', 'vision').

        Returns:
            SecretStr containing the API key or None if not configured.
        """
        provider_lower = provider.lower()
        key: SecretStr | None = None
        if provider_lower in ("google", "gemini", "vertexai"):
            key = self.GOOGLE_API_KEY or self.GEMINI_API_KEY or self.GCP_API_KEY
        elif provider_lower in ("ocr", "vision", "google_vision"):
            key = self.OCR_API_KEY or self.VISION_API_KEY
        elif provider_lower == "openai":
            key = self.OPENAI_API_KEY
        elif provider_lower in ("anthropic", "claude"):
            key = self.ANTHROPIC_API_KEY
        elif provider_lower == "openrouter":
            key = self.OPEN_ROUTER_API_KEY
        elif provider_lower in ("xai", "grok"):
            key = self.XAI_API_KEY

        if key and not is_placeholder_key(key):
            return key
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
            env_key_name = ENV_GEMINI_API_KEY
            os.environ[ENV_GOOGLE_API_KEY] = key
            os.environ[ENV_GEMINI_API_KEY] = key
            os.environ[ENV_GCP_API_KEY] = key
        elif provider_lower == "openai":
            self.OPENAI_API_KEY = secret
            env_key_name = ENV_OPENAI_API_KEY
            os.environ[ENV_OPENAI_API_KEY] = key
        elif provider_lower in ("anthropic", "claude"):
            self.ANTHROPIC_API_KEY = secret
            env_key_name = ENV_ANTHROPIC_API_KEY
            os.environ[ENV_ANTHROPIC_API_KEY] = key
        elif provider_lower == "openrouter":
            self.OPEN_ROUTER_API_KEY = secret
            env_key_name = ENV_OPEN_ROUTER_API_KEY
            os.environ[ENV_OPEN_ROUTER_API_KEY] = key
        elif provider_lower == "xai":
            self.XAI_API_KEY = secret
            env_key_name = ENV_XAI_API_KEY
            os.environ[ENV_XAI_API_KEY] = key
        elif provider_lower in ("ocr", "vision", "google_vision"):
            self.OCR_API_KEY = secret
            self.VISION_API_KEY = secret
            env_key_name = ENV_OCR_API_KEY
            os.environ[ENV_OCR_API_KEY] = key
            os.environ[ENV_VISION_API_KEY] = key

        if persist_to_env and env_key_name:
            target_env_files = [
                ROOT_DIR / ".env",
                get_app_dir() / ".env",
            ]
            seen_paths = set()
            for env_file in target_env_files:
                try:
                    resolved = env_file.resolve()
                    if resolved in seen_paths:
                        continue
                    seen_paths.add(resolved)

                    lines = []
                    if env_file.exists():
                        lines = env_file.read_text(encoding="utf-8").splitlines()

                    # Determine keys to update
                    keys_to_update = [env_key_name]
                    if provider_lower in ("google", "gemini", "vertexai"):
                        keys_to_update = [ENV_GEMINI_API_KEY, ENV_GOOGLE_API_KEY, ENV_GCP_API_KEY]
                    elif provider_lower in ("ocr", "vision", "google_vision"):
                        keys_to_update = [ENV_OCR_API_KEY, ENV_VISION_API_KEY]

                    new_lines = []
                    updated_set = set()
                    for line in lines:
                        replaced = False
                        for k in keys_to_update:
                            if (
                                line.startswith(f"{k}=")
                                or line.startswith(f"#{k}=")
                                or line.startswith(f"# {k}=")
                            ):
                                new_lines.append(f"{k}={key}")
                                updated_set.add(k)
                                replaced = True
                                break
                        if not replaced:
                            new_lines.append(line)

                    # Ensure the primary key is present if not replaced
                    primary_key = keys_to_update[0]
                    if primary_key not in updated_set:
                        new_lines.append(f"{primary_key}={key}")

                    env_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
                except Exception as e:
                    logger.warning(f"Could not persist {env_key_name} to {env_file}: {e}")


# Singleton instance
settings = Settings()

# Synchronize DATA_ENGINE_DB_PATH in environment for external sub-processes/tools
if settings.DATA_ENGINE_DB_PATH:
    os.environ[ENV_DATA_ENGINE_DB_PATH] = str(settings.DATA_ENGINE_DB_PATH)
