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

"""Agent tools, sub-agent capabilities, and explorer settings configuration."""

import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from artemis.config.constants import (
    AGENT_CONFIG_FILENAME,
    DEFAULT_EXPLORER_VERSION,
    ExplorerVersion,
)
from artemis.config.paths import ROOT_DIR, get_config_path
from artemis.utils.file import load_jsonc
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


class VideoAnalyzerConfig(BaseModel):
    """Configuration specific to VideoAnalyzer and screen recording."""

    enabled: bool | None = Field(
        default=None,
        description=(
            "Whether continuous screen video recording and video analyzer tools "
            "are enabled. If None, auto-detected based on scrcpy & ffmpeg availability."
        ),
    )
    enable_ledger: bool = Field(
        default=True,
        description="Whether to enable video action ledger state tracking.",
    )
    chunk_size_seconds: float = Field(
        default=60.0,
        ge=5.0,
        le=300.0,
        description="Maximum duration of an initial video-analysis chunk.",
    )
    min_chunk_seconds: float = Field(
        default=4.0,
        ge=1.0,
        le=30.0,
        description="Smallest chunk produced by failure-driven bisection.",
    )
    max_split_depth: int = Field(
        default=4,
        ge=0,
        le=8,
        description="Maximum recursive chunk bisection depth after failures.",
    )
    circuit_breaker_threshold: int = Field(
        default=3,
        ge=1,
        le=20,
        description="Consecutive transient model failures before opening the circuit.",
    )
    circuit_breaker_cooldown_seconds: float = Field(
        default=60.0,
        ge=1.0,
        le=900.0,
        description="How long an unhealthy video model remains bypassed.",
    )
    action_window_seconds: float = Field(
        default=2.0,
        ge=0.0,
        le=10.0,
        description="Dense-perception window on each side of a recorded mobile action.",
    )
    dense_action_fps: float = Field(
        default=4.0,
        ge=1.0,
        le=15.0,
        description="Frame sampling rate around recorded mobile actions.",
    )
    max_dense_action_frames: int = Field(
        default=24,
        ge=0,
        le=120,
        description="Maximum additional action-proximal keyframes per chunk.",
    )
    native_max_retries: int = Field(
        default=1,
        ge=0,
        le=5,
        description=(
            "Retries against the native video provider before switching to the "
            "configured universal fallback. A value of 1 means two total attempts."
        ),
    )
    model_call_timeout_seconds: float = Field(
        default=120.0,
        ge=15.0,
        le=600.0,
        description="Hard timeout for one native or universal video-model response.",
    )
    model_config = {"extra": "allow"}


class PlannerValidationConfig(BaseModel):
    """Configuration specific to Planner validation of milestone changes."""

    enabled: bool = Field(
        default=False,
        description="Whether to enable async planner validation when task plan milestones change.",
    )
    similarity_threshold: float = Field(
        default=0.85,
        description="Difflib similarity threshold below which milestone text changes trigger re-validation.",
    )
    model_config = {"extra": "allow"}


class CommitteeConfig(BaseModel):
    """Configuration specific to Multi-Agent Committee / Council."""

    enabled: bool = Field(
        default=False,
        description="Whether to equip Operator with the ask_committee tool.",
    )
    debate_rounds: int = Field(
        default=2,
        ge=1,
        le=5,
        description="Number of debate rounds among council members (1-5).",
    )
    model_config = {"extra": "allow"}


class CheckerConfig(BaseModel):
    """Configuration for plan-driven checkpoint verification and the final review."""

    enabled: bool = Field(
        default=False,
        description=(
            "Master switch (compat alias): False disables BOTH midway"
            " checkpoints and the final check."
        ),
    )
    midway_checks: bool = Field(
        default=True,
        description="Whether plan-declared midway checkpoints run (requires enabled=True).",
    )
    final_check: bool = Field(
        default=True,
        description="Whether the exit final review runs (requires enabled=True).",
    )
    max_iterations: int = Field(
        default=20,
        ge=1,
        le=50,
        description="Maximum verification iterations for Checker LLM reasoning and inspection.",
    )
    final_check_max_attempts: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum final-review attempts before ending with a blocked outcome.",
    )
    checkpoint_max_repairs: int = Field(
        default=2,
        ge=0,
        le=10,
        description="Per-checkpoint repair quota for failed verify criteria.",
    )
    max_concurrent_checkpoints: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum concurrently running checkpoint attempts.",
    )
    checkpoint_timeout: float = Field(
        default=180.0,
        gt=0,
        description="Per-attempt timeout (seconds) for one checkpoint check.",
    )
    settlement_timeout: float = Field(
        default=120.0,
        gt=0,
        description="Aggregate timeout (seconds) for the exit settlement barrier.",
    )
    assert_failure_policy: Literal["continue", "halt"] = Field(
        default="continue",
        description="Whether an assert failure lets execution continue or halts the run.",
    )
    device_probes: bool = Field(
        default=True,
        description="Whether the Checker's enumerated read-only device probes are registered.",
    )
    model_config = {"extra": "allow"}


class OutputterConfig(BaseModel):
    """Configuration specific to Outputter post-execution report synthesis agent."""

    enabled: bool = Field(
        default=True,
        description="Whether Outputter agent is mounted for post-execution report/structured output synthesis.",
    )
    force_synthesis: bool = Field(
        default=False,
        description="Whether to force Outputter synthesis even when no structured schema is explicitly requested.",
    )
    model_config = {"extra": "allow"}


class ExplorerConfig(BaseModel):
    """Configuration specific to UI Explorer visual sub-agent (Flash / Pro / Ultra)."""

    default_version: ExplorerVersion = Field(
        default=DEFAULT_EXPLORER_VERSION,
        description="Default Explorer version mode ('flash', 'pro', or 'ultra').",
    )
    flash_mode: ExplorerVersion = Field(
        default="flash",
        description="Explorer version mode when main system runs under Flash profile (FlashRunner).",
    )
    pro_mode: ExplorerVersion = Field(
        default="flash",
        description="Explorer version mode when main system runs under Pro profile (Graph / Operator / Validator).",
    )
    caching: bool = Field(
        default=True,
        description="Whether to enable context caching for multi-turn pro/ultra Explorer.",
    )
    model_config = {"extra": "allow"}

    def resolve(
        self,
        explicit_version: str | None = None,
        agent_name: str | None = None,
        profile: str | None = None,
        per_agent_overrides: dict[str, str] | None = None,
    ) -> ExplorerVersion:
        """Resolves active Explorer version with clean precedence rules."""
        if explicit_version:
            v = str(explicit_version).strip().lower()
            if v in ("flash", "pro", "ultra"):
                return v  # type: ignore

        env_v = os.getenv("ARTEMIS_EXPLORER_VERSION", "").strip().lower()
        if env_v in ("flash", "pro", "ultra"):
            return env_v  # type: ignore

        if per_agent_overrides and agent_name and agent_name in per_agent_overrides:
            v = str(per_agent_overrides[agent_name]).strip().lower()
            if v in ("flash", "pro", "ultra"):
                return v  # type: ignore

        if profile in ("flash", "flash_runner") or agent_name in ("flash", "flash_runner"):
            return self.flash_mode
        if profile in ("pro", "operator", "validator") or agent_name in (
            "pro",
            "operator",
            "validator",
        ):
            return self.pro_mode

        return self.default_version


class StepSummarizerConfig(BaseModel):
    """Configuration specific to Flash asynchronous step visual summarization."""

    enabled: bool = Field(
        default=True,
        description="Whether to asynchronously summarize historical steps to replace pruned images.",
    )
    model: str = Field(
        default="gemini-2.5-flash-lite",
        description="Lightweight model used for background step state summarization.",
    )
    prune_history_xml: bool = Field(
        default=True,
        description="Whether to prune outdated heavy UI XML trees from historical turns.",
    )
    model_config = {"extra": "allow"}


class FlashProfileConfig(BaseModel):
    """Configuration options specific to the ⚡ Flash execution profile (FlashRunner/ReactiveRunner)."""

    max_turns: int = Field(
        default=30,
        ge=1,
        le=100,
        description="Maximum reactive turns for Flash execution before timeout.",
    )
    explorer_mode: ExplorerVersion = Field(
        default="flash",
        description="Perception mode for Flash runner ('flash' for 1-shot detection).",
    )
    step_summarizer: StepSummarizerConfig = Field(
        default_factory=StepSummarizerConfig,
        description="Asynchronous visual context compressor settings.",
    )
    model_config = {"extra": "allow"}


class ProExplorerConfig(BaseModel):
    """Explorer perception settings under Pro profile."""

    mode: ExplorerVersion = Field(
        default="flash",
        description="Explorer perception mode under Pro profile ('flash', 'pro', or 'ultra').",
    )
    caching: bool = Field(
        default=True,
        description="Whether to enable context caching for multi-turn Pro/Ultra exploration.",
    )
    model_config = {"extra": "allow"}


class ProProfileConfig(BaseModel):
    """Configuration options specific to the 🚀 Pro execution profile (LangGraph / Multi-Agent Closed-Loop)."""

    explorer: ProExplorerConfig = Field(
        default_factory=ProExplorerConfig,
        description="Explorer perception settings for Pro profile.",
    )
    planner_validation: PlannerValidationConfig = Field(
        default_factory=PlannerValidationConfig,
        description="Planner milestone consistency validation settings.",
    )
    committee: CommitteeConfig = Field(
        default_factory=CommitteeConfig,
        description="Multi-Agent Committee council debate settings.",
    )
    checker: CheckerConfig = Field(
        default_factory=CheckerConfig,
        description="Checker visual verification and rollback settings.",
    )
    video_analyzer: VideoAnalyzerConfig = Field(
        default_factory=VideoAnalyzerConfig,
        description="Screen video action ledger tracking settings.",
    )
    model_config = {"extra": "allow"}


class AgentGlobalConfig(BaseModel):
    """Global configuration parsed from artemis.jsonc / agent_config.json."""

    # ⚡ Profile-categorized configurations
    flash: FlashProfileConfig = Field(
        default_factory=FlashProfileConfig,
        description="Options for ⚡ Flash reactive runner execution.",
    )
    pro: ProProfileConfig = Field(
        default_factory=ProProfileConfig,
        description="Options for 🚀 Pro multi-agent graph execution.",
    )

    # 🌐 Core Component Configurations
    explorer: ExplorerConfig = Field(
        default_factory=ExplorerConfig,
        description="UI Explorer sub-agent runtime options.",
    )
    explorer_versions: dict[str, str] = Field(
        default_factory=lambda: {"operator": "flash", "validator": "flash"},
        description="Per-agent mapping of UI explorer version (e.g. flash, pro, ultra).",
    )
    planner_validation: PlannerValidationConfig = Field(
        default_factory=PlannerValidationConfig,
        description="Planner milestone validation runtime options.",
    )
    committee: CommitteeConfig = Field(
        default_factory=CommitteeConfig,
        description="Committee council runtime options.",
    )
    checker: CheckerConfig = Field(
        default_factory=CheckerConfig,
        description="Checker subgoal verification and rollback runtime options.",
    )
    outputter: OutputterConfig = Field(
        default_factory=OutputterConfig,
        description="Outputter synthesis agent runtime options.",
    )
    denylisted_tools: dict[str, list[str]] = Field(
        default_factory=dict,
        description="List of denylisted tools per agent.",
    )
    video_analyzer: VideoAnalyzerConfig = Field(
        default_factory=VideoAnalyzerConfig,
        description="Video analyzer runtime options.",
    )

    model_config = {"extra": "allow"}

    @model_validator(mode="before")
    @classmethod
    def synchronize_profiles_and_components(cls, data: Any) -> Any:
        """Bidirectionally synchronize between categorized 'flash'/'pro' blocks and component fields."""
        if not isinstance(data, dict):
            return data

        # 1. Sync from pro block to root components if provided
        if "pro" in data and isinstance(data["pro"], dict):
            pro_data = data["pro"]
            if "checker" in pro_data and "checker" not in data:
                data["checker"] = pro_data["checker"]
            if "committee" in pro_data and "committee" not in data:
                data["committee"] = pro_data["committee"]
            if "planner_validation" in pro_data and "planner_validation" not in data:
                data["planner_validation"] = pro_data["planner_validation"]
            if "video_analyzer" in pro_data and "video_analyzer" not in data:
                data["video_analyzer"] = pro_data["video_analyzer"]
            if "explorer" in pro_data and isinstance(pro_data["explorer"], dict):
                exp_pro = pro_data["explorer"]
                if "explorer" not in data:
                    data["explorer"] = {}
                if isinstance(data["explorer"], dict):
                    if "mode" in exp_pro and "pro_mode" not in data["explorer"]:
                        data["explorer"]["pro_mode"] = exp_pro["mode"]
                    if "caching" in exp_pro and "caching" not in data["explorer"]:
                        data["explorer"]["caching"] = exp_pro["caching"]

        # 2. Sync from flash block to explorer.flash_mode
        if "flash" in data and isinstance(data["flash"], dict):
            flash_data = data["flash"]
            if "explorer_mode" in flash_data:
                if "explorer" not in data:
                    data["explorer"] = {}
                if isinstance(data["explorer"], dict) and "flash_mode" not in data["explorer"]:
                    data["explorer"]["flash_mode"] = flash_data["explorer_mode"]

        # 3. Sync from root components to pro/flash blocks if pro/flash block is omitted
        if "checker" in data and ("pro" not in data or "checker" not in data.get("pro", {})):
            data.setdefault("pro", {})["checker"] = data["checker"]
        if "committee" in data and ("pro" not in data or "committee" not in data.get("pro", {})):
            data.setdefault("pro", {})["committee"] = data["committee"]
        if "planner_validation" in data and (
            "pro" not in data or "planner_validation" not in data.get("pro", {})
        ):
            data.setdefault("pro", {})["planner_validation"] = data["planner_validation"]
        if "video_analyzer" in data and (
            "pro" not in data or "video_analyzer" not in data.get("pro", {})
        ):
            data.setdefault("pro", {})["video_analyzer"] = data["video_analyzer"]

        # 4. Environment variable overrides (highest precedence for runtime switches)
        def _parse_bool(var_name: str) -> bool | None:
            v = os.getenv(var_name)
            if v is None:
                return None
            v_clean = v.strip().lower()
            if v_clean in ("1", "true", "yes", "on"):
                return True
            if v_clean in ("0", "false", "no", "off"):
                return False
            return None

        chk_env = _parse_bool("ARTEMIS_CHECKER_ENABLED")
        if chk_env is not None:
            data.setdefault("checker", {})["enabled"] = chk_env
            data.setdefault("pro", {}).setdefault("checker", {})["enabled"] = chk_env

        comm_env = _parse_bool("ARTEMIS_COMMITTEE_ENABLED")
        if comm_env is not None:
            data.setdefault("committee", {})["enabled"] = comm_env
            data.setdefault("pro", {}).setdefault("committee", {})["enabled"] = comm_env

        pv_env = _parse_bool("ARTEMIS_PLANNER_VALIDATION_ENABLED")
        if pv_env is not None:
            data.setdefault("planner_validation", {})["enabled"] = pv_env
            data.setdefault("pro", {}).setdefault("planner_validation", {})["enabled"] = pv_env

        out_env = _parse_bool("ARTEMIS_OUTPUTTER_ENABLED")
        if out_env is not None:
            data.setdefault("outputter", {})["enabled"] = out_env

        vid_rec_env = _parse_bool("ARTEMIS_VIDEO_RECORDING_ENABLED") or _parse_bool(
            "ARTEMIS_WITH_VIDEO_RECORDING_TOOLS"
        )
        if vid_rec_env is not None:
            data.setdefault("video_analyzer", {})["enabled"] = vid_rec_env
            data.setdefault("pro", {}).setdefault("video_analyzer", {})["enabled"] = vid_rec_env

        vid_env = _parse_bool("ARTEMIS_VIDEO_LEDGER_ENABLED")
        if vid_env is not None:
            data.setdefault("video_analyzer", {})["enable_ledger"] = vid_env
            data.setdefault("pro", {}).setdefault("video_analyzer", {})["enable_ledger"] = vid_env

        return data

    def get_explorer_version(
        self,
        explicit_version: str | None = None,
        agent_name: str | None = "operator",
        profile: str | None = None,
    ) -> ExplorerVersion:
        """Resolve Explorer version from this configuration."""
        return self.explorer.resolve(
            explicit_version=explicit_version,
            agent_name=agent_name,
            profile=profile,
            per_agent_overrides=self.explorer_versions,
        )


def resolve_explorer_version(
    ctx: Any | None = None,
    explicit_version: str | None = None,
    agent_or_profile_name: str | None = "operator",
) -> ExplorerVersion:
    """Resolves active Explorer version based on explicit args, env vars, agent_config, and execution setup."""
    if explicit_version:
        v = str(explicit_version).strip().lower()
        if v in ("flash", "pro", "ultra"):
            return v  # type: ignore

    env_v = os.getenv("ARTEMIS_EXPLORER_VERSION", "").strip().lower()
    if env_v in ("flash", "pro", "ultra"):
        return env_v  # type: ignore

    if ctx:
        agent_cfg = getattr(ctx, "agent_config", None)
        exec_setup = getattr(ctx, "execution_setup", None)

        if agent_cfg:
            if hasattr(agent_cfg, "get_explorer_version"):
                return agent_cfg.get_explorer_version(
                    explicit_version=explicit_version,
                    agent_name=agent_or_profile_name,
                )
            if hasattr(agent_cfg, "explorer") and hasattr(agent_cfg.explorer, "resolve"):
                overrides = getattr(agent_cfg, "explorer_versions", None)
                return agent_cfg.explorer.resolve(
                    explicit_version=explicit_version,
                    agent_name=agent_or_profile_name,
                    per_agent_overrides=overrides,
                )

        if (
            exec_setup
            and hasattr(exec_setup, "explorer")
            and hasattr(exec_setup.explorer, "resolve")
        ):
            overrides = getattr(exec_setup, "explorer_versions", None)
            return exec_setup.explorer.resolve(
                explicit_version=explicit_version,
                agent_name=agent_or_profile_name,
                per_agent_overrides=overrides,
            )

    return "flash"


def load_agent_config(
    config_path: Path | None = None,
) -> AgentGlobalConfig:
    """Load and validate AgentGlobalConfig from artemis.jsonc or agent_config.json.

    Args:
        config_path: Optional explicit file path to configuration file.

    Returns:
        Validated AgentGlobalConfig instance.
    """
    resolved_path = config_path
    if not resolved_path:
        for candidate in ("artemis.jsonc", "artemis.json", AGENT_CONFIG_FILENAME):
            try:
                resolved_path = get_config_path(candidate)
                break
            except FileNotFoundError:
                continue

    if not resolved_path:
        resolved_path = get_config_path(AGENT_CONFIG_FILENAME, ROOT_DIR / AGENT_CONFIG_FILENAME)

    try:
        with open(resolved_path, encoding="utf-8") as f:
            data = load_jsonc(f)
            if isinstance(data, dict) and "agent" in data:
                return AgentGlobalConfig.model_validate(data["agent"])
            return AgentGlobalConfig.model_validate(data)
    except Exception as e:
        logger.error(f"Failed to load agent config from '{resolved_path}': {e}")
        raise ValueError(f"Failed to parse '{resolved_path}': {e}") from e
