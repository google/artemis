# Copyright 2025-2026 Minitap, Inc.
# Modifications Copyright 2026 Google LLC
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
#
# Originally from mobile-use (https://github.com/minitap-ai/mobile-use).
# See third_party/mobile_use/METADATA for the upstream source and local modifications.

"""LLM provider models, the base LLM configuration model and its loaders."""

from collections.abc import Callable
import os
from pathlib import Path
from typing import Any, Literal

import google.auth
from google.auth.exceptions import DefaultCredentialsError
from pydantic import BaseModel, ValidationError

from artemis.config.constants import AgentNode, LLMProvider, LLMUtilsNode
from artemis.config.settings import settings
from artemis.utils.cython_compat import CyFunctionDetector
from third_party.mobile_use.utils.file import load_jsonc
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

LLMUtilsNodeWithFallback = LLMUtilsNode
AgentNodeWithFallback = AgentNode


def validate_vertex_ai_credentials() -> None:
    """Validate Google Application Default Credentials for VertexAI provider."""
    try:
        _, project = google.auth.default()
        if not project:
            raise Exception("VertexAI requires a Google Cloud project to be set.")
    except DefaultCredentialsError as e:
        raise Exception(
            f"VertexAI requires valid Google Application Default Credentials (ADC): {e}"
        )


class LLM(BaseModel):
    """Base model representing an LLM model provider and runtime parameters."""

    model_config = {"ignored_types": (CyFunctionDetector,)}
    provider: LLMProvider
    model: str
    temperature: float | None = None
    api_base: str | None = None
    max_tokens: int | None = None
    coordinate_format: (
        Literal["yx_1000", "xy_1000", "yx_px", "xy_px", "yx_norm", "xy_norm"] | None
    ) = None
    thinking_budget: int | None = None
    thinking_level: Literal["minimal", "low", "medium", "high"] | None = None
    reasoning_effort: Literal["none", "low", "medium", "high"] | None = None
    include_thoughts: bool | None = None
    enable_grounding: bool | None = None
    timeout: float | None = None

    def validate_provider(self, name: str) -> None:
        """Ensure the required API key or credentials exist in settings for this provider."""
        if self.provider == "openai":
            if not settings.OPENAI_API_KEY:
                raise Exception(f"{name} requires OPENAI_API_KEY in .env")
        elif self.provider == "google":
            if not settings.GOOGLE_API_KEY:
                raise Exception(f"{name} requires GOOGLE_API_KEY in .env")
        elif self.provider == "vertexai":
            validate_vertex_ai_credentials()
        elif self.provider == "anthropic":
            if not (settings.ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_API_KEY")):
                raise Exception(f"{name} requires ANTHROPIC_API_KEY in .env")
        elif self.provider == "openrouter":
            if not settings.OPEN_ROUTER_API_KEY:
                raise Exception(f"{name} requires OPEN_ROUTER_API_KEY in .env")
        elif self.provider == "xai":
            if not settings.XAI_API_KEY:
                raise Exception(f"{name} requires XAI_API_KEY in .env")

    def __str__(self) -> str:
        return f"{self.provider}/{self.model}"


class LLMWithFallback(LLM):
    """LLM configuration with automatic secondary fallback and timeout specs."""

    fallback: LLM
    fix_model: str | None = None
    timeout: float | None = None

    def __str__(self) -> str:
        return f"{self.provider}/{self.model} (fallback: {self.fallback})"


class LLMConfigUtils(BaseModel):
    """Configuration container for auxiliary utility agents/nodes."""

    model_config = {"ignored_types": (CyFunctionDetector,)}
    outputter: LLMWithFallback
    hopper: LLMWithFallback
    video_analyzer: LLMWithFallback | None = None
    object_detector: LLMWithFallback | None = None


class LLMConfigBase(BaseModel):
    """Base LLM configuration: planner, utility nodes and their accessors."""

    model_config = {"ignored_types": (CyFunctionDetector,)}
    planner: LLMWithFallback
    utils: LLMConfigUtils

    def __str__(self) -> str:
        return f"""
📃 Planner: {self.planner}
🧩 Utils:
    🔽 Hopper: {self.utils.hopper}
    📝 Outputter: {self.utils.outputter}
    🎬 Video Analyzer: {self.utils.video_analyzer or "Not configured"}
    👁️ Object Detector: {self.utils.object_detector or "Not configured"}
"""

    def validate_providers(self) -> None:
        """Validate credentials of the planner and the utility nodes."""
        self.planner.validate_provider("Planner")
        self.utils.outputter.validate_provider("Outputter")
        self.utils.hopper.validate_provider("Hopper")
        if self.utils.video_analyzer:
            self.utils.video_analyzer.validate_provider("VideoAnalyzer")
        if self.utils.object_detector:
            self.utils.object_detector.validate_provider("ObjectDetector")

    def get_utils(self, item: LLMUtilsNode) -> LLMWithFallback:
        """Retrieve model configuration for a specific utility node."""
        value = getattr(self.utils, item)
        if value is None:
            raise ValueError(
                f"Utils '{item}' is not configured. Please add it to your LLM "
                "config or enable it via AgentConfigBuilder."
            )
        return value


def initialize_llm_config[C: LLMConfigBase](parse: Callable[[], C]) -> C:
    """Parse the LLM config with ``parse`` and validate provider credentials."""
    llm_config = parse()
    llm_config.validate_providers()
    logger.success("LLM config initialized")
    return llm_config


def load_llm_config_override[C: LLMConfigBase](
    path: Path,
    get_default: Callable[[], C],
    merge: Callable[[C, dict[str, Any]], C],
) -> C:
    """Merge the JSON/JSONC overrides at ``path`` onto the default config.

    Falls back to the default config when the file is missing or the merged
    config is invalid.
    """
    default_config = get_default()

    override_config_dict: dict[str, Any] = {}
    if path.exists():
        logger.info(f"Loading custom LLM config from {path.resolve()}...")
        with open(path, encoding="utf-8") as f:
            override_config_dict = load_jsonc(f)
    else:
        logger.warning(f"Custom LLM config not found at {path} - using default config")

    try:
        return merge(default_config, override_config_dict)
    except ValidationError as e:
        logger.error(f"Invalid LLM config: {e}")
        logger.info("Falling back to default config")
        return default_config
