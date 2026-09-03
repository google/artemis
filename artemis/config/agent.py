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
        default=True,
        description=(
            "Whether every top-level milestone text change is reviewed by the"
            " lightweight async planner validator. The review is advisory: a"
            " flagged change is never rolled back, the Operator only receives"
            " the concern and its reason as feedback."
        ),
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
        default=True,
        description=(
            "Master switch (compat alias): False disables BOTH midway"
            " checkpoints and the final check."
        ),
    )
    midway_checks: bool = Field(
        default=False,
        description=(
            "Whether plan-declared midway checkpoints run (requires enabled=True)."
            " Off in the factory layering: final check on, midway checks off."
        ),
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


#: Coarse verification presets exposed to the CLI (``--verification-level``)
#: and the admin console (``verification_level`` on ``/api/run``). Each preset
#: is a partial :class:`CheckerConfig` override; unspecified fields keep the
#: values from ``artemis.jsonc``. ``final`` mirrors the factory layering.
VerificationLevel = Literal["off", "final", "checkpoints", "strict"]
DEFAULT_VERIFICATION_LEVEL: VerificationLevel = "final"
VERIFICATION_LEVEL_PRESETS: dict[str, dict[str, Any]] = {
    # No Checker at all: the Operator self-reports completion, nothing audits it.
    "off": {"enabled": False},
    # Factory layering: one exit audit against the user's goal, no midway checkpoints.
    "final": {"enabled": True, "midway_checks": False, "final_check": True},
    # Every plan-declared checkpoint runs at its subgoal's completion, plus the exit audit.
    "checkpoints": {"enabled": True, "midway_checks": True, "final_check": True},
    # Checkpoints + exit audit with a larger repair budget; a failed assert halts the run.
    "strict": {
        "enabled": True,
        "midway_checks": True,
        "final_check": True,
        "assert_failure_policy": "halt",
        "checkpoint_max_repairs": 4,
        "final_check_max_attempts": 5,
        "max_iterations": 30,
    },
}


def checker_overrides_for_level(level: str | None) -> dict[str, Any]:
    """Return the :class:`CheckerConfig` field overrides for a verification level.

    Args:
        level: One of ``off``, ``final``, ``checkpoints`` or ``strict``
            (case-insensitive, surrounding whitespace ignored).

    Raises:
        ValueError: when ``level`` is not a known preset.
    """
    key = str(level or "").strip().lower()
    preset = VERIFICATION_LEVEL_PRESETS.get(key)
    if preset is None:
        known = ", ".join(VERIFICATION_LEVEL_PRESETS)
        raise ValueError(f"Unknown verification level {level!r}; expected one of: {known}")
    return dict(preset)


def verification_level_for_checker(checker: "CheckerConfig") -> VerificationLevel:
    """Classify an effective :class:`CheckerConfig` back onto the coarse ladder.

    Used by launcher UIs to show where the configured defaults sit; the inverse of
    :func:`checker_overrides_for_level` for the fields the presets control.
    """
    if not checker.enabled or (not checker.midway_checks and not checker.final_check):
        return "off"
    if checker.midway_checks:
        return "strict" if checker.assert_failure_policy == "halt" else "checkpoints"
    return "final"


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
    caching: bool | None = Field(
        default=None,
        description=(
            "Gemini explicit context caching for multi-turn Explorer tiers. ``None``"
            " (default) uses the tier's own default: off for pro, on for ultra."
        ),
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
    retry_limit: int = Field(
        default=3,
        ge=0,
        description=(
            "Maximum retries per step summary after the first attempt; on"
            " exhaustion the step is marked summary_status=failed instead of"
            " retrying forever."
        ),
    )
    model_config = {"extra": "allow"}


class MemoryRuntimeConfig(BaseModel):
    """Scheduling options for the shared step-memory runtime (agent.memory.runtime)."""

    max_concurrency: int = Field(
        default=2,
        ge=1,
        le=16,
        description=(
            "Maximum background summary attempts running concurrently."
            " Default 2 per the 2026-09-01 on-device baseline"
            " (history-baseline-2026-09-01.md §6): the serial queue at 1 pushed"
            " summary-ready P90 past the scrub-edge grace window; lens calls"
            " are independent lightweight-model requests, so 2 is low-risk."
        ),
    )
    retry_limit: int = Field(
        default=3,
        ge=0,
        description=(
            "Maximum retries per summary job after the first attempt; on"
            " exhaustion the job enters an explicit failed state."
        ),
    )
    flush_timeout_s: float = Field(
        default=30.0,
        gt=0,
        description="Upper bound in seconds for draining in-flight summary jobs at shutdown.",
    )
    model_config = {"extra": "allow"}


class MemoryTranscriptConfig(BaseModel):
    """Scrub-edge options for the message transcript (agent.memory.transcript)."""

    enabled: bool = Field(
        default=True,
        description=(
            "Whether the Pro operator builds its prompt from the session"
            " transcript ledger (S/F/A regions + per-turn tail) instead of the"
            " legacy per-turn 2-message rebuild. On by default since M5"
            " (2026-09-01, per the on-device A/B in"
            " history-baseline-2026-09-01.md); this is also the rollback"
            " switch — setting it to false restores the legacy path"
            " byte-for-byte, and L2/L3 chunk compression rides the same flag."
        ),
    )
    image_scrub_depth: int = Field(
        default=3,
        ge=1,
        description=(
            "Depth K of the screenshot scrub edge: the K-th most recent"
            " historical screenshot is replaced by its visual summary."
        ),
    )
    pending_grace_steps: int = Field(
        default=3,
        ge=0,
        description=(
            "Extra image-depths a step may retain its screenshot past the"
            " scrub edge while its summary is still pending; afterwards the"
            " image is replaced by a placeholder referencing the DataEngine step."
        ),
    )
    xml_scrub_depth: int = Field(
        default=1,
        ge=1,
        description=(
            "How many of the most recent observations keep their UI Element"
            " list; older lists are stripped (depth 1 matches the legacy"
            " prune_history_xml semantics)."
        ),
    )
    context_budget_tokens: int = Field(
        default=80000,
        ge=1024,
        description=(
            "Measured-token budget for the transcript context (provisional"
            " default pending the on-device baseline; §3.4). The soft/hard"
            " ratios below are applied against this budget."
        ),
    )
    soft_ratio: float = Field(
        default=0.7,
        gt=0.0,
        lt=1.0,
        description=(
            "Soft threshold: when the last measured prompt size reaches"
            " budget*soft_ratio, the oldest open segment is chunk-compressed"
            " (L2)."
        ),
    )
    hard_ratio: float = Field(
        default=0.9,
        gt=0.0,
        le=1.0,
        description=(
            "Hard threshold: when the last measured prompt size reaches"
            " budget*hard_ratio, the frozen region collapses to the L3 session"
            " snapshot (chunk headers merged; per-step index retained)."
        ),
    )
    min_active_steps: int = Field(
        default=5,
        ge=1,
        description=(
            "Sliding-window floor: no compression trigger may shrink the"
            " active region below this many most-recent raw turns."
        ),
    )
    similarity_hint: bool = Field(
        default=True,
        description=(
            "Whether the operator prompt injects a local historical-state hint"
            " when the current screen's perceptual hash closely matches a much"
            " older step's post-action screen (no model call, no historical"
            " image). Silent when the match is within the recent 3 steps —"
            " that regime belongs to the pixel-level same-screen note."
        ),
    )
    similarity_max_distance: int = Field(
        default=5,
        ge=0,
        le=64,
        description=(
            "Maximum Hamming distance between 64-bit dHash values for the"
            " historical-state hint to fire. Calibrated 2026-09-01 from 460"
            " dHash-stamped on-device steps (16 sessions): same-screen"
            " re-captures cluster at distance <=4, genuinely different screens"
            " mass at >=7 (valley at 5-6); cross-app pairs (guaranteed"
            " different screens) score <=5 in only 0.14% of cases vs 0.24% at"
            " the previous provisional 8."
        ),
    )
    model_config = {"extra": "allow"}


class MemoryRecallConfig(BaseModel):
    """History recall tool options (agent.memory.recall)."""

    enabled: bool = Field(
        default=True,
        description=(
            "Whether the Pro operator gets the recall_history tool for"
            " deterministic lookups into cold (compressed/evicted) history."
        ),
    )
    max_results: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Upper bound on results per recall_history call.",
    )
    max_text_tokens: int = Field(
        default=2000,
        ge=128,
        description=(
            "Estimated-token cap (char/4) on one recall_history text response;"
            " excerpts are truncated to fit."
        ),
    )
    max_image_steps: int = Field(
        default=1,
        ge=0,
        le=1,
        description=(
            "Maximum number of steps whose real stored screenshots may be"
            " returned as data URLs when include_images is requested."
        ),
    )
    model_config = {"extra": "allow"}


class MemoryChunkingConfig(BaseModel):
    """Segment (L2) chunk compression options (agent.memory.chunking)."""

    max_steps: int = Field(
        default=12,
        ge=1,
        description=(
            "Size-threshold trigger: an open segment of this many steps is"
            " chunk-compressed even without a milestone switch. Also the"
            " maximum steps per chunk when a longer segment is split."
        ),
    )
    target_source_tokens: int = Field(
        default=2000,
        ge=128,
        description=(
            "Size-threshold trigger: estimated source tokens (char/4 over the"
            " open segment's transcript text) that force a chunk compression."
        ),
    )
    model: str = Field(
        default="gemini-3.8-flash",
        description="Model used for the chunk-level StepCapsuleLens (bands ①+②).",
    )
    max_chunks: int = Field(
        default=8,
        ge=1,
        description=(
            "Maximum full three-band chunk blocks kept in the frozen region;"
            " older chunks merge into eras (headers set-merged, ledgers kept)."
        ),
    )
    max_eras: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Maximum eras kept with their per-segment ledgers; older eras"
            " collapse their ledgers to recall_history marker lines. None"
            " (default) follows max_chunks, preserving the pre-M5 equal-value"
            " behavior."
        ),
    )
    model_config = {"extra": "allow"}


class MemoryConfig(BaseModel):
    """Unified step-memory configuration block (agent.memory)."""

    runtime: MemoryRuntimeConfig = Field(
        default_factory=MemoryRuntimeConfig,
        description="Shared background summarization runtime scheduling options.",
    )
    transcript: MemoryTranscriptConfig = Field(
        default_factory=MemoryTranscriptConfig,
        description="Transcript scrub-edge options.",
    )
    chunking: MemoryChunkingConfig = Field(
        default_factory=MemoryChunkingConfig,
        description="Segment chunk (L2) compression options.",
    )
    recall: MemoryRecallConfig = Field(
        default_factory=MemoryRecallConfig,
        description="History recall tool options.",
    )
    policies: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description=(
            "Per-agent overrides of the compiled-history ContextPolicy table"
            " (artemis.memory.context_policy); keys are agent names, values"
            " are policy field overrides (e.g. {'planner': {'last_n_detailed': 3}})."
        ),
    )
    model_config = {"extra": "allow"}


class FlashProfileConfig(BaseModel):
    """Configuration options specific to the ⚡ Flash execution profile (FlashRunner/ReactiveRunner)."""

    max_turns: int = Field(
        default=0,
        ge=0,
        description=(
            "Maximum reactive turns for Flash execution; 0 (the default) means"
            " unlimited. Context growth is bounded by the transcript ledger's"
            " scrub edge and chunk compression, not by a turn cap, so a long"
            " task simply runs until it reports its status."
        ),
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


class ExecutionConfig(BaseModel):
    """Validator execution tiers for the Pro profile."""

    max_burst_actions: int = Field(
        default=4,
        ge=2,
        le=10,
        description=(
            "Maximum turn-ending actions the Operator may chain into one fast-action"
            " burst (executed back to back without the safety net). A longer turn is"
            " rejected before execution and fed back to the Operator."
        ),
    )
    plan_ledger_gate: bool = Field(
        default=True,
        description=(
            "Require active plan sub-goals and periodically prompt the Operator to"
            " update stale plan progress."
        ),
    )
    plan_ledger_stale_turns: int = Field(
        default=4,
        ge=0,
        description=(
            "Action turns allowed without a task_plan change before the ledger gate"
            " prompts for an update. Set to 0 to disable staleness checks."
        ),
    )


class ProProfileConfig(BaseModel):
    """Configuration options specific to the 🚀 Pro execution profile (LangGraph / Multi-Agent Closed-Loop)."""

    execution: ExecutionConfig = Field(
        default_factory=ExecutionConfig,
        description="Validator execution tiers (vetted single action vs fast-action burst).",
    )
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
        description="Checker checkpoint verification and exit final review settings.",
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
        default_factory=dict,
        description=(
            "Advanced per-agent override of the Explorer tier (e.g."
            ' {"validator": "ultra"}). Empty by default so that'
            " ``explorer.flash_mode`` / ``explorer.pro_mode`` decide."
        ),
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
        description="Checker checkpoint verification and exit final review runtime options.",
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
    memory: MemoryConfig = Field(
        default_factory=MemoryConfig,
        description="Unified step-memory runtime and transcript scrub options.",
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

        # 3b. Legacy alias: an explicit flash.step_summarizer.retry_limit keeps
        # working by seeding memory.runtime.retry_limit when the new key is
        # absent (an explicit new key wins over the legacy one).
        flash_block = data.get("flash")
        flash_ss = flash_block.get("step_summarizer") if isinstance(flash_block, dict) else None
        if isinstance(flash_ss, dict) and "retry_limit" in flash_ss:
            mem_block = data.setdefault("memory", {})
            if isinstance(mem_block, dict):
                runtime_block = mem_block.setdefault("runtime", {})
                if isinstance(runtime_block, dict):
                    runtime_block.setdefault("retry_limit", flash_ss["retry_limit"])

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
    """Resolves the active Explorer tier for a calling agent or profile.

    Precedence: explicit argument, ``ARTEMIS_EXPLORER_VERSION`` environment
    override, then the user's agent configuration (per-agent override, profile
    mode, default version) found on ``ctx.agent_config`` or, failing that, on
    ``ctx.execution_setup``.  The tier is a user setting: calling agents never
    pass it themselves.
    """
    for source in (
        getattr(ctx, "agent_config", None),
        getattr(ctx, "execution_setup", None),
    ):
        explorer_cfg = getattr(source, "explorer", None)
        if explorer_cfg is not None and hasattr(explorer_cfg, "resolve"):
            return explorer_cfg.resolve(
                explicit_version=explicit_version,
                agent_name=agent_or_profile_name,
                per_agent_overrides=getattr(source, "explorer_versions", None),
            )

    return ExplorerConfig().resolve(
        explicit_version=explicit_version, agent_name=agent_or_profile_name
    )


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
