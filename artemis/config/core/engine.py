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

"""Multi-source Cascading Configuration Engine."""

import json
from pathlib import Path
import re
from typing import Any

from dotenv import load_dotenv

from artemis.config.core.interpolator import interpolate_config_value
from artemis.platform import platform
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


def _strip_json_comments(text: str) -> str:
    """Strip // and /* */ comments from JSONC text while preserving string literals."""
    pattern = r'//.*?$|/\*.*?\*/|\'(?:\\.|[^\\\'])*\'|"(?:\\.|[^\\"])*"'

    def replacer(match: re.Match) -> str:
        s = match.group(0)
        if s.startswith("/"):
            return ""
        return s

    return re.sub(pattern, replacer, text, flags=re.DOTALL | re.MULTILINE)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively deep merge two dictionaries."""
    merged = base.copy()
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


class CascadingConfigEngine:
    """Manages 5-tier configuration hierarchy with automatic cascade and interpolation."""

    def __init__(self, workspace_root: Path | None = None):
        self.workspace_root = workspace_root or Path.cwd()
        self._load_environment_files()

    def _load_environment_files(self) -> None:
        """Load .env files from global config dir and workspace root."""
        # 1. Global user .env
        global_env = platform.paths.config_dir / ".env"
        if global_env.exists():
            load_dotenv(dotenv_path=global_env, override=False, encoding="utf-8")

        # 2. Legacy ~/.artemis/.env fallback
        legacy_env = platform.paths.data_dir / ".env"
        if legacy_env.exists() and legacy_env != global_env:
            load_dotenv(dotenv_path=legacy_env, override=False, encoding="utf-8")

        # 3. Workspace .env
        ws_env = self.workspace_root / ".env"
        if ws_env.exists():
            load_dotenv(dotenv_path=ws_env, override=True, encoding="utf-8")

    def load_raw_config(
        self,
        cli_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Load and merge all config layers into a unified dictionary."""
        config: dict[str, Any] = {}

        # Tier 1: Global user configuration ($CONFIG_DIR/artemis.jsonc)
        global_config_path = platform.paths.config_dir / "artemis.jsonc"
        if global_config_path.exists():
            try:
                raw_text = global_config_path.read_text(encoding="utf-8")
                cleaned = _strip_json_comments(raw_text)
                config = _deep_merge(config, json.loads(cleaned))
            except Exception as e:
                logger.warning(f"Failed to load global config {global_config_path}: {e}")

        # Tier 2: Workspace project configuration (config/artemis.jsonc)
        ws_config_path = self.workspace_root / "config" / "artemis.jsonc"
        if not ws_config_path.exists():
            ws_config_path = self.workspace_root / "artemis.jsonc"

        if ws_config_path.exists():
            try:
                raw_text = ws_config_path.read_text(encoding="utf-8")
                cleaned = _strip_json_comments(raw_text)
                config = _deep_merge(config, json.loads(cleaned))
            except Exception as e:
                logger.warning(f"Failed to load workspace config {ws_config_path}: {e}")

        # Tier 3: CLI overrides
        if cli_overrides:
            config = _deep_merge(config, cli_overrides)

        # Apply variable interpolation across merged dictionary
        interpolated = interpolate_config_value(config, workspace_root=self.workspace_root)
        return interpolated


config_engine = CascadingConfigEngine()
