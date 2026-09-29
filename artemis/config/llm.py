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

from pathlib import Path
from typing import Any


from artemis.config.constants import (
    LLM_CONFIG_FILENAME,
    AgentNode,
)
from artemis.config.paths import ROOT_DIR, get_config_path
from artemis.utils.cython_compat import CyFunctionDetector
from third_party.mobile_use.config import llm as base_llm_config
from third_party.mobile_use.config.llm import (
    LLM,
    AgentNodeWithFallback,
    LLMConfigBase,
    LLMConfigUtils,
    LLMUtilsNodeWithFallback,
    LLMWithFallback,
    validate_vertex_ai_credentials,
)
from third_party.mobile_use.utils.file import load_jsonc
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "LLM",
    "AgentNodeWithFallback",
    "LLMUtilsNodeWithFallback",
    "CyFunctionDetector",
    "LLMConfig",
    "LLMConfigUtils",
    "LLMWithFallback",
    "deep_merge_llm_config",
    "get_default_llm_config",
    "initialize_llm_config",
    "lightweight_judge_default",
    "load_llm_config_override",
    "parse_llm_config",
    "validate_vertex_ai_credentials",
]


def lightweight_judge_default(provider: str = "google") -> "LLMWithFallback":
    """Factory default for the lightweight judge nodes (pixel safety net and
    planner validation): a flash-lite model at temperature 0.

    The provider follows the configured global default (inherited from the
    operator node) so non-Google setups never hard-require a Google key:
    Google keeps the cheap flash-lite models, every other provider rides the
    same provider with model "auto" (resolved by that provider's gateway).
    """
    normalized = str(provider or "google").lower().replace("_", "").replace("-", "")
    if normalized in ("google", "gemini"):
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
    return LLMWithFallback(
        provider=provider,
        model="auto",
        temperature=0.0,
        fallback=LLM(
            provider=provider,
            model="auto",
            temperature=0.0,
        ),
    )


class LLMConfig(LLMConfigBase):
    """Comprehensive LLM configuration mapping every node to primary/fallback models."""

    summarizer: LLMWithFallback
    operator: LLMWithFallback
    operator_summarizer: LLMWithFallback
    log_reader_sub_agent: LLMWithFallback
    log_analyzer: LLMWithFallback
    diagnoser: LLMWithFallback
    checker: LLMWithFallback
    planner_avatar: LLMWithFallback
    history_analyzer_expert: LLMWithFallback
    diagnoser_expert: LLMWithFallback
    explorer: LLMWithFallback
    history_analyzer: LLMWithFallback | None = None
    validator_pixel_safety_net: LLMWithFallback | None = None
    planner_validation: LLMWithFallback | None = None
    output_analyzer: LLMWithFallback | None = None

    def validate_providers(self) -> None:
        """Validate credentials across all configured agent nodes."""
        super().validate_providers()
        self.summarizer.validate_provider("Summarizer")
        self.operator.validate_provider("Operator")
        self.operator_summarizer.validate_provider("OperatorSummarizer")
        self.log_reader_sub_agent.validate_provider("LogReaderSubAgent")
        self.log_analyzer.validate_provider("LogAnalyzer")
        self.diagnoser.validate_provider("Diagnoser")
        self.checker.validate_provider("Checker")
        self.planner_avatar.validate_provider("PlannerAvatar")
        self.history_analyzer_expert.validate_provider("HistoryAnalyzerExpert")
        self.diagnoser_expert.validate_provider("DiagnoserExpert")
        self.explorer.validate_provider("Explorer")
        if self.history_analyzer:
            self.history_analyzer.validate_provider("HistoryAnalyzer")
        if self.validator_pixel_safety_net:
            self.validator_pixel_safety_net.validate_provider("ValidatorPixelSafetyNet")
        if self.planner_validation:
            self.planner_validation.validate_provider("PlannerValidation")
        if self.output_analyzer:
            self.output_analyzer.validate_provider("OutputAnalyzer")

    def get_agent(self, item: AgentNode) -> LLMWithFallback:
        """Retrieve model configuration for a specific agent node with sensible defaults."""
        val = getattr(self, item)
        if val is None:
            if item == "history_analyzer":
                return self.operator
            elif item in ("validator_pixel_safety_net", "planner_validation"):
                # Both are cheap, high-frequency judges: the pixel safety net
                # runs before actions, the planner validator after every
                # milestone edit. They share one lightweight default whose
                # provider follows the configured global default (via the
                # operator node) so custom-provider setups never touch Google.
                try:
                    base_provider = str(
                        getattr(self.operator, "provider", "google") or "google"
                    )
                except Exception:
                    base_provider = "google"
                return lightweight_judge_default(provider=base_provider)
            elif item == "output_analyzer":
                return self.log_analyzer
        return val


def _expand_default_into_nodes(config_dict: dict) -> dict:
    """Expand unified config format with 'default' and 'nodes' into full LLMConfig schema."""
    if "planner" in config_dict and "utils" in config_dict:
        return config_dict

    default_model_cfg = config_dict.get(
        "default",
        {
            "provider": "google",
            "model": "gemini-3.8-flash",
            "fallback": {
                "provider": "google",
                "model": "gemini-3.7-flash",
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
    return base_llm_config.initialize_llm_config(parse_llm_config)


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
    resolved_path = Path(path)
    if not resolved_path.exists():
        try:
            resolved_path = get_config_path(str(path))
        except OSError:
            pass
    return base_llm_config.load_llm_config_override(
        resolved_path, get_default_llm_config, deep_merge_llm_config
    )
