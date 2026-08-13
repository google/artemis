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

"""LLM provider, model hierarchy, fallback chaining, and configuration loaders."""

import os
from pathlib import Path
from typing import Any, Literal

import google.auth
from google.auth.exceptions import DefaultCredentialsError
from pydantic import BaseModel, ValidationError

from artemis.config.constants import (
    LLM_CONFIG_FILENAME,
    AgentNode,
    LLMProvider,
    LLMUtilsNode,
)
from artemis.config.paths import ROOT_DIR, get_config_path
from artemis.config.settings import settings
from artemis.utils.file import load_jsonc
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


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


class _CyFunctionDetectorMeta(type):
    def __instancecheck__(self, instance):
        name = type(instance).__name__
        return (
            name
            in (
                "cyfunction",
                "cython_function_or_method",
                "builtin_function_or_method",
            )
            or "cyfunction" in name.lower()
        )


class CyFunctionDetector(metaclass=_CyFunctionDetectorMeta):
    """Utility type detector for Cython and C-extension functions in Pydantic."""

    pass


class LLM(BaseModel):
    """Base model representing an LLM model provider and runtime parameters."""

    model_config = {"ignored_types": (CyFunctionDetector,)}
    provider: LLMProvider
    model: str
    temperature: float | None = None
    thinking_budget: int | None = None
    thinking_level: Literal["minimal", "low", "medium", "high"] | None = None
    reasoning_effort: Literal["none", "low", "medium", "high"] | None = None
    include_thoughts: bool | None = None
    enable_grounding: bool | None = None

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


class LLMConfig(BaseModel):
    """Comprehensive LLM configuration mapping every node to primary/fallback models."""

    model_config = {"ignored_types": (CyFunctionDetector,)}
    planner: LLMWithFallback
    utils: LLMConfigUtils
    summarizer: LLMWithFallback
    operator: LLMWithFallback
    operator_summarizer: LLMWithFallback
    log_reader_sub_agent: LLMWithFallback
    log_analyzer: LLMWithFallback
    diagnoser: LLMWithFallback
    validator_failure_analyzer: LLMWithFallback
    checker: LLMWithFallback
    planner_avatar: LLMWithFallback
    history_analyzer_expert: LLMWithFallback
    diagnoser_expert: LLMWithFallback
    explorer: LLMWithFallback
    history_analyzer: LLMWithFallback | None = None
    validator_pixel_safety_net: LLMWithFallback | None = None
    output_analyzer: LLMWithFallback | None = None

    def validate_providers(self) -> None:
        """Validate credentials across all configured agent nodes."""
        self.planner.validate_provider("Planner")
        self.utils.outputter.validate_provider("Outputter")
        self.utils.hopper.validate_provider("Hopper")
        if self.utils.video_analyzer:
            self.utils.video_analyzer.validate_provider("VideoAnalyzer")
        if self.utils.object_detector:
            self.utils.object_detector.validate_provider("ObjectDetector")
        self.summarizer.validate_provider("Summarizer")
        self.operator.validate_provider("Operator")
        self.operator_summarizer.validate_provider("OperatorSummarizer")
        self.log_reader_sub_agent.validate_provider("LogReaderSubAgent")
        self.log_analyzer.validate_provider("LogAnalyzer")
        self.diagnoser.validate_provider("Diagnoser")
        self.validator_failure_analyzer.validate_provider("ValidatorFailureAnalyzer")
        self.checker.validate_provider("Checker")
        self.planner_avatar.validate_provider("PlannerAvatar")
        self.history_analyzer_expert.validate_provider("HistoryAnalyzerExpert")
        self.diagnoser_expert.validate_provider("DiagnoserExpert")
        self.explorer.validate_provider("Explorer")
        if self.history_analyzer:
            self.history_analyzer.validate_provider("HistoryAnalyzer")
        if self.validator_pixel_safety_net:
            self.validator_pixel_safety_net.validate_provider("ValidatorPixelSafetyNet")
        if self.output_analyzer:
            self.output_analyzer.validate_provider("OutputAnalyzer")

    def __str__(self) -> str:
        return f"""
📃 Planner: {self.planner}
🧩 Utils:
    🔽 Hopper: {self.utils.hopper}
    📝 Outputter: {self.utils.outputter}
    🎬 Video Analyzer: {self.utils.video_analyzer or "Not configured"}
    👁️ Object Detector: {self.utils.object_detector or "Not configured"}
"""

    def get_agent(self, item: AgentNode) -> LLMWithFallback:
        """Retrieve model configuration for a specific agent node with sensible defaults."""
        val = getattr(self, item)
        if val is None:
            if item == "history_analyzer":
                return self.operator
            elif item == "validator_pixel_safety_net":
                return LLMWithFallback(
                    provider="google",
                    model="gemini-3.5-flash-lite",
                    temperature=0.0,
                    fallback=LLM(
                        provider="google",
                        model="gemini-3.1-flash-lite",
                        temperature=0.0,
                    ),
                )
            elif item == "output_analyzer":
                return self.log_analyzer
        return val

    def get_utils(self, item: LLMUtilsNode) -> LLMWithFallback:
        """Retrieve model configuration for a specific utility node."""
        value = getattr(self.utils, item)
        if value is None:
            raise ValueError(
                f"Utils '{item}' is not configured. Please add it to your LLM "
                "config or enable it via AgentConfigBuilder."
            )
        return value


def _expand_default_into_nodes(config_dict: dict) -> dict:
    """Expand unified config format with 'default' and 'nodes' into full LLMConfig schema."""
    if "planner" in config_dict and "utils" in config_dict:
        return config_dict

    default_model_cfg = config_dict.get(
        "default",
        {
            "provider": "google",
            "model": "gemini-3.6-flash",
            "fallback": {
                "provider": "google",
                "model": "gemini-3.5-flash",
            },
        },
    )

    nodes_override = config_dict.get("nodes", {})

    all_agent_nodes = [
        "planner",
        "summarizer",
        "operator",
        "operator_summarizer",
        "log_reader_sub_agent",
        "log_analyzer",
        "diagnoser",
        "validator_failure_analyzer",
        "checker",
        "planner_avatar",
        "history_analyzer_expert",
        "diagnoser_expert",
        "explorer",
    ]

    all_utils_nodes = [
        "outputter",
        "hopper",
        "video_analyzer",
        "object_detector",
    ]

    result: dict[str, Any] = {}
    for node in all_agent_nodes:
        node_cfg = dict(default_model_cfg)
        if node in nodes_override:
            for k, v in nodes_override[node].items():
                if isinstance(v, dict) and isinstance(node_cfg.get(k), dict):
                    node_cfg[k] = {**node_cfg[k], **v}
                else:
                    node_cfg[k] = v
        result[node] = node_cfg

    utils_dict: dict[str, Any] = {}
    for util in all_utils_nodes:
        util_cfg = dict(default_model_cfg)
        if util in nodes_override:
            for k, v in nodes_override[util].items():
                if isinstance(v, dict) and isinstance(util_cfg.get(k), dict):
                    util_cfg[k] = {**util_cfg[k], **v}
                else:
                    util_cfg[k] = v
        utils_dict[util] = util_cfg
    result["utils"] = utils_dict

    return result


def parse_llm_config() -> LLMConfig:
    """Parse and instantiate LLMConfig from artemis.jsonc or llm-config.json."""
    config_path = None
    for candidate in ("artemis.jsonc", "artemis.json", LLM_CONFIG_FILENAME):
        try:
            config_path = get_config_path(candidate)
            break
        except FileNotFoundError:
            continue

    if not config_path:
        config_path = get_config_path(LLM_CONFIG_FILENAME, ROOT_DIR / LLM_CONFIG_FILENAME)

    try:
        with open(config_path, encoding="utf-8") as f:
            config_dict = load_jsonc(f)
            expanded_dict = _expand_default_into_nodes(config_dict)
            return LLMConfig.model_validate(expanded_dict)
    except Exception as e:
        logger.error(f"Failed to load or parse llm config: {config_path}. Error: {e}")
        raise


def initialize_llm_config() -> LLMConfig:
    """Parse and validate credentials for LLMConfig."""
    llm_config = parse_llm_config()
    llm_config.validate_providers()
    logger.success("LLM config initialized")
    return llm_config


def get_default_llm_config() -> LLMConfig:
    """Returns default LLMConfig parsed from standard configuration file."""
    return parse_llm_config()


def deep_merge_llm_config(base: LLMConfig, overrides: dict) -> LLMConfig:
    """Recursively merge dictionary overrides into an existing LLMConfig object."""
    base_dict = base.model_dump()

    def merge(d1: dict, d2: dict) -> None:
        for k, v in d2.items():
            if k in d1 and isinstance(d1[k], dict) and isinstance(v, dict):
                merge(d1[k], v)
            else:
                d1[k] = v

    merge(base_dict, overrides)
    return LLMConfig.model_validate(base_dict)


def load_llm_config_override(path: Path | str) -> LLMConfig:
    """Load custom LLM configuration JSON/JSONC overrides on top of default configuration."""
    default_config = get_default_llm_config()

    resolved_path = Path(path)
    if not resolved_path.exists():
        try:
            resolved_path = get_config_path(str(path))
        except Exception:
            pass

    override_config_dict = {}
    if resolved_path.exists():
        logger.info(f"Loading custom LLM config from {resolved_path.resolve()}...")
        with open(resolved_path, encoding="utf-8") as f:
            override_config_dict = load_jsonc(f)
    else:
        logger.warning(f"Custom LLM config not found at {path} - using default config")

    try:
        return deep_merge_llm_config(default_config, override_config_dict)
    except ValidationError as e:
        logger.error(f"Invalid LLM config: {e}")
        logger.info("Falling back to default config")
        return default_config
