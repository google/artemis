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

"""Builder for AgentConfig objects using a fluent interface."""

import copy
import os
from typing import Any

from langchain_core.callbacks.base import Callbacks
from artemis.config import (
    ExplorerVersion,
    checker_overrides_for_level,
    get_default_llm_config,
    load_agent_config,
    settings,
)
from artemis.context import DevicePlatform
from artemis.sdk.constants import DEFAULT_PROFILE_NAME
from artemis.sdk.types.agent import (
    AgentConfig,
    AgentProfile,
    ServerConfig,
)
from artemis.sdk.types.task import TaskRequestCommon
from artemis.utils.video import detect_video_tools_enabled


class AgentConfigBuilder:
    """Builder class providing a fluent interface for creating AgentConfig objects.

    This builder allows for step-by-step construction of an AgentConfig with
    clear methods that make the configuration process intuitive and type-safe.

    Examples:
        >>> builder = AgentConfigBuilder()
        >>> config = (builder
        ...     .add_profile(AgentProfile(name="HighReasoning",
        llm_config=LLMConfig(...)))
        ...     .add_profile(AgentProfile(name="LowReasoning",
        llm_config=LLMConfig(...)))
        ...     .for_device(DevicePlatform.ANDROID, "device123")
        ...     .with_default_task_config(TaskRequestCommon(max_steps=30))
        ...     .with_default_profile("HighReasoning")
        ...     .build()
        ... )
    """

    def __init__(self):
        """Initialize an empty AgentConfigBuilder."""
        self._agent_profiles: dict[str, AgentProfile] = {}
        self._task_request_defaults: TaskRequestCommon | None = None
        self._default_profile: str | AgentProfile | None = None
        self._device_id: str | None = None
        self._device_platform: DevicePlatform | None = None
        self._servers: ServerConfig = get_default_servers()
        self._graph_config_callbacks: Callbacks = None
        self._video_recording_tools_enabled: bool = detect_video_tools_enabled()
        self._force_web_accessibility: bool = False
        self._disable_checker: bool = False
        self._cloud_mobile_id_or_ref: str | None = None
        self._concurrency_mode: str = "per_device"
        self._max_concurrency: int | None = None

        agent_cfg = load_agent_config()
        self._explorer = agent_cfg.explorer
        self._explorer_versions = agent_cfg.explorer_versions
        self._denylisted_tools = agent_cfg.denylisted_tools
        self._video_analyzer = agent_cfg.video_analyzer
        self._enable_video_ledger = agent_cfg.video_analyzer.enable_ledger
        if agent_cfg.video_analyzer.enabled is not None:
            self._video_recording_tools_enabled = agent_cfg.video_analyzer.enabled
        else:
            self._video_recording_tools_enabled = detect_video_tools_enabled()
        self._disable_planner_validation = not agent_cfg.planner_validation.enabled
        self._enable_committee = agent_cfg.committee.enabled
        self._committee_debate_rounds = agent_cfg.committee.debate_rounds
        self._disable_checker = not agent_cfg.checker.enabled
        self._checker_max_iterations = agent_cfg.checker.max_iterations
        self._disable_midway_checks = not agent_cfg.checker.midway_checks
        self._disable_final_check = not agent_cfg.checker.final_check
        self._final_check_max_attempts = agent_cfg.checker.final_check_max_attempts
        self._checkpoint_max_repairs = agent_cfg.checker.checkpoint_max_repairs
        self._max_concurrent_checkpoints = agent_cfg.checker.max_concurrent_checkpoints
        self._checkpoint_timeout = agent_cfg.checker.checkpoint_timeout
        self._settlement_timeout = agent_cfg.checker.settlement_timeout
        self._assert_failure_policy = agent_cfg.checker.assert_failure_policy
        self._disable_device_probes = not agent_cfg.checker.device_probes
        self._outputter = agent_cfg.outputter
        self._disable_outputter = not agent_cfg.outputter.enabled
        self._flash = agent_cfg.flash
        self._pro = agent_cfg.pro

    def add_profile(self, profile: AgentProfile, validate: bool = True) -> "AgentConfigBuilder":
        """Add an agent profile to the ARTEMIS agent.

        Args:
            profile: The agent profile to add
        """
        self._agent_profiles[profile.name] = profile
        if validate:
            profile.llm_config.validate_providers()
        return self

    def add_profiles(
        self,
        profiles: list[AgentProfile],
        validate: bool = True,
    ) -> "AgentConfigBuilder":
        """Add multiple agent profiles to the ARTEMIS agent.

        Args:
            profiles: List of agent profiles to add
        """
        for profile in profiles:
            self.add_profile(profile=profile, validate=validate)
        return self

    def with_default_profile(self, profile: str | AgentProfile) -> "AgentConfigBuilder":
        """Set the default agent profile used for tasks.

        Args:
            profile: The name or instance of the default agent profile
        """
        self._default_profile = profile
        return self

    def for_device(
        self,
        platform_or_device_id: DevicePlatform | str,
        device_id: str | None = None,
    ) -> "AgentConfigBuilder":
        """Configure the ARTEMIS agent for a specific device.

        Supports both:
            builder.for_device(DevicePlatform.ANDROID, "emulator-5554")
        and:
            builder.for_device("emulator-5554")  (defaults to DevicePlatform.ANDROID)

        Args:
            platform_or_device_id: DevicePlatform or unique identifier for the device
            device_id: The unique identifier for the device (if platform was passed first)
        """
        if self._cloud_mobile_id_or_ref is not None:
            raise ValueError(
                "Device ID cannot be set when a cloud mobile is already"
                " configured.\n> for_device() and for_cloud_mobile() are"
                " mutually exclusive"
            )
        if isinstance(platform_or_device_id, DevicePlatform):
            self._device_platform = platform_or_device_id
            self._device_id = device_id
        else:
            self._device_platform = DevicePlatform.ANDROID
            self._device_id = str(platform_or_device_id)
        return self

    def for_device_serial(self, device_serial: str) -> "AgentConfigBuilder":
        """Target a specific Android device by its ADB serial number."""
        return self.for_device(DevicePlatform.ANDROID, device_serial)

    def with_concurrency_mode(self, mode: str) -> "AgentConfigBuilder":
        """Configure concurrency mode: 'global' (1 task globally) or 'per_device' (1 task per device)."""
        self._concurrency_mode = str(mode).strip().lower()
        return self

    def with_max_concurrency(self, max_concurrency: int) -> "AgentConfigBuilder":
        """Configure max concurrent tasks limit."""
        self._max_concurrency = max_concurrency
        return self

    def with_default_task_config(self, config: TaskRequestCommon) -> "AgentConfigBuilder":
        """Set the default task configuration.

        Args:
            config: The task configuration to use as default
        """
        self._task_request_defaults = copy.deepcopy(config)
        return self

    def with_adb_server(self, host: str, port: int | None = None) -> "AgentConfigBuilder":
        """Set the ADB server host and port.

        Args:
            host: The ADB server host
            port: The ADB server port
        """
        self._servers.adb_host = host
        if port is not None:
            self._servers.adb_port = port
        return self

    def with_servers(self, servers: ServerConfig) -> "AgentConfigBuilder":
        """Set the server settings.

        Args:
            servers: The server settings to use
        """
        self._servers = copy.deepcopy(servers)
        return self

    def with_graph_config_callbacks(self, callbacks: Callbacks) -> "AgentConfigBuilder":
        """Set the graph config callbacks.

        Args:
            callbacks: The graph config callbacks to use
        """
        self._graph_config_callbacks = callbacks
        return self

    def with_video_recording_tools(self, enabled: bool = True) -> "AgentConfigBuilder":
        """Enable or disable video recording tools.

        Args:
            enabled: Whether to enable video recording tools
        """
        self._video_recording_tools_enabled = enabled
        return self

    def with_web_accessibility(self, enabled: bool = True) -> "AgentConfigBuilder":
        """Enable or disable forcing web accessibility for WebViews.

        Args:
            enabled: Whether to force web accessibility
        """
        self._force_web_accessibility = enabled
        return self

    def with_disable_checker(self, disable: bool = True) -> "AgentConfigBuilder":
        """Temporarily disable the background Checker task (useful for debugging).

        Args:
            disable: Whether to disable the checker
        """
        self._disable_checker = disable
        return self

    def with_checker(
        self,
        enabled: bool = True,
        max_iterations: int | None = None,
        midway_checks: bool | None = None,
        final_check: bool | None = None,
        final_check_max_attempts: int | None = None,
        checkpoint_max_repairs: int | None = None,
        max_concurrent_checkpoints: int | None = None,
        checkpoint_timeout: float | None = None,
        settlement_timeout: float | None = None,
        assert_failure_policy: str | None = None,
        device_probes: bool | None = None,
    ) -> "AgentConfigBuilder":
        """Configure plan-driven checkpoint verification and the final review.

        Args:
            enabled: Master switch (compat alias); False disables both gates
            max_iterations: Optional maximum iterations for Checker reasoning (1-50)
            midway_checks: Whether plan-declared midway checkpoints run
            final_check: Whether the exit final review runs
            final_check_max_attempts: Max final-review attempts before a blocked exit
            checkpoint_max_repairs: Per-checkpoint verify-fail repair quota
            max_concurrent_checkpoints: Concurrency cap for checkpoint attempts
            checkpoint_timeout: Per-attempt timeout in seconds
            settlement_timeout: Aggregate exit-settlement timeout in seconds
            assert_failure_policy: 'continue' or 'halt' on assert failures
            device_probes: Whether Checker read-only device probes are registered
        """
        self._disable_checker = not enabled
        if max_iterations is not None:
            self._checker_max_iterations = max_iterations
        if midway_checks is not None:
            self._disable_midway_checks = not midway_checks
        if final_check is not None:
            self._disable_final_check = not final_check
        if final_check_max_attempts is not None:
            self._final_check_max_attempts = final_check_max_attempts
        if checkpoint_max_repairs is not None:
            self._checkpoint_max_repairs = checkpoint_max_repairs
        if max_concurrent_checkpoints is not None:
            self._max_concurrent_checkpoints = max_concurrent_checkpoints
        if checkpoint_timeout is not None:
            self._checkpoint_timeout = checkpoint_timeout
        if settlement_timeout is not None:
            self._settlement_timeout = settlement_timeout
        if assert_failure_policy is not None:
            self._assert_failure_policy = assert_failure_policy
        if device_probes is not None:
            self._disable_device_probes = not device_probes
        return self

    def with_verification_level(self, level: str) -> "AgentConfigBuilder":
        """Apply a coarse Checker preset (the ``--verification-level`` knob).

        Presets are defined once in :data:`artemis.config.VERIFICATION_LEVEL_PRESETS`:
        ``off`` (no Checker), ``final`` (exit review only, the factory default),
        ``checkpoints`` (every plan checkpoint + exit review) and ``strict``
        (checkpoints with a larger repair budget; a failed assert halts).
        Fields the preset leaves unspecified keep their current values, so an
        explicit :meth:`with_checker` call afterwards still wins.

        Args:
            level: One of ``off``, ``final``, ``checkpoints`` or ``strict``
                (case-insensitive, surrounding whitespace ignored).

        Raises:
            ValueError: when ``level`` is not a known preset.
        """
        return self.with_checker(**checker_overrides_for_level(level))

    def with_midway_checks(self, enabled: bool = True) -> "AgentConfigBuilder":
        """Enable or disable plan-declared midway checkpoints."""
        self._disable_midway_checks = not enabled
        return self

    def with_final_check(self, enabled: bool = True) -> "AgentConfigBuilder":
        """Enable or disable the exit final review."""
        self._disable_final_check = not enabled
        return self

    def with_disable_planner_validation(self, disable: bool = True) -> "AgentConfigBuilder":
        """Temporarily disable the background Planner validation task.

        Args:
            disable: Whether to disable the planner validation
        """
        self._disable_planner_validation = disable
        return self

    def with_planner_validation(self, enabled: bool = True) -> "AgentConfigBuilder":
        """Configure the advisory async Planner validation on milestone changes.

        Every top-level milestone text change is reviewed by the lightweight
        validator; a flagged change only yields a hint plus reason for the
        Operator (no rollback, no action suppression).

        Args:
            enabled: Whether planner validation is enabled
        """
        self._disable_planner_validation = not enabled
        return self

    def with_enable_committee(self, enabled: bool = True) -> "AgentConfigBuilder":
        """Enable or disable the Committee council debate tool for Operator."""
        self._enable_committee = enabled
        return self

    def with_committee(self, enabled: bool = True, debate_rounds: int = 2) -> "AgentConfigBuilder":
        """Configure Multi-Agent Committee council debate settings.

        Args:
            enabled: Whether committee debate tool is enabled
            debate_rounds: Number of debate rounds (1-5)
        """
        self._enable_committee = enabled
        self._committee_debate_rounds = debate_rounds
        return self

    def with_outputter(
        self,
        enabled: bool = True,
        force_synthesis: bool = False,
    ) -> "AgentConfigBuilder":
        """Configure Outputter post-execution synthesis agent options.

        Args:
            enabled: Whether Outputter is mounted to synthesize final outputs
            force_synthesis: Force Outputter synthesis even if no structured output schema is specified
        """
        self._outputter = self._outputter.model_copy(
            update={"enabled": enabled, "force_synthesis": force_synthesis}
        )
        self._disable_outputter = not enabled
        return self

    def with_disable_outputter(self, disable: bool = True) -> "AgentConfigBuilder":
        """Disable Outputter agent execution."""
        self._disable_outputter = disable
        self._outputter = self._outputter.model_copy(update={"enabled": not disable})
        return self

    def with_flash_config(
        self,
        max_turns: int | None = None,
        explorer_mode: ExplorerVersion | None = None,
        step_summarizer: bool | None = None,
        step_summarizer_model: str | None = None,
        prune_history_xml: bool | None = None,
    ) -> "AgentConfigBuilder":
        """Configure ⚡ Flash execution profile options.

        Args:
            max_turns: Maximum reactive loop turns (0 = unlimited)
            explorer_mode: Explorer tier used by the Flash runner's ``ask_explorer``
                ('flash' one-shot detection, 'pro' short reasoning loop, 'ultra'
                deep loop); mirrored to ``explorer.flash_mode``
            step_summarizer: Enable/disable asynchronous visual context compressor
            step_summarizer_model: Lightweight model for background step summarization
            prune_history_xml: Whether to prune outdated XML trees from historical steps
        """
        updates: dict[str, Any] = {}
        if max_turns is not None:
            updates["max_turns"] = max_turns
        if explorer_mode is not None:
            updates["explorer_mode"] = explorer_mode
            self._explorer = self._explorer.model_copy(update={"flash_mode": explorer_mode})
        if (
            step_summarizer is not None
            or step_summarizer_model is not None
            or prune_history_xml is not None
        ):
            sum_updates: dict[str, Any] = {}
            if step_summarizer is not None:
                sum_updates["enabled"] = step_summarizer
            if step_summarizer_model is not None:
                sum_updates["model"] = step_summarizer_model
            if prune_history_xml is not None:
                sum_updates["prune_history_xml"] = prune_history_xml
            updates["step_summarizer"] = self._flash.step_summarizer.model_copy(update=sum_updates)
        if updates:
            self._flash = self._flash.model_copy(update=updates)
        return self

    def with_flash_step_summarizer(
        self,
        enabled: bool = True,
        model: str | None = None,
        prune_history_xml: bool | None = None,
    ) -> "AgentConfigBuilder":
        """Configure Flash asynchronous step state summarizer options."""
        return self.with_flash_config(
            step_summarizer=enabled,
            step_summarizer_model=model,
            prune_history_xml=prune_history_xml,
        )

    def with_pro_config(
        self,
        explorer_mode: ExplorerVersion | None = None,
        planner_validation: bool | None = None,
        committee: bool | None = None,
        checker: bool | None = None,
        video_ledger: bool | None = None,
        verification_level: str | None = None,
    ) -> "AgentConfigBuilder":
        """Configure 🚀 Pro execution profile options.

        Args:
            explorer_mode: Explorer tier used by the Pro profile's Operator / Validator
                ('flash', 'pro', or 'ultra'); mirrored to ``explorer.pro_mode``
            planner_validation: Enable/disable the advisory review of plan milestone edits
            committee: Enable/disable multi-agent committee debate tool
            checker: Enable/disable the Checker (plan checkpoint verification + exit final review)
            video_ledger: Enable/disable screen video action ledger tracking
            verification_level: Coarse Checker preset ('off', 'final', 'checkpoints',
                'strict'); applied before the explicit ``checker`` switch so the
                switch still wins when both are given
        """
        if explorer_mode is not None:
            self._pro = self._pro.model_copy(
                update={"explorer": self._pro.explorer.model_copy(update={"mode": explorer_mode})}
            )
            self._explorer = self._explorer.model_copy(update={"pro_mode": explorer_mode})
        if planner_validation is not None:
            self.with_planner_validation(enabled=planner_validation)
        if committee is not None:
            self.with_committee(enabled=committee)
        if verification_level is not None:
            self.with_verification_level(verification_level)
        if checker is not None:
            self.with_checker(enabled=checker)
        if video_ledger is not None:
            self._enable_video_ledger = video_ledger
            self._pro = self._pro.model_copy(
                update={
                    "video_analyzer": self._pro.video_analyzer.model_copy(
                        update={"enable_ledger": video_ledger}
                    )
                }
            )
        return self

    def with_explorer(
        self,
        version: ExplorerVersion | None = None,
        default_version: ExplorerVersion | None = None,
        flash_mode: ExplorerVersion | None = None,
        pro_mode: ExplorerVersion | None = None,
        caching: bool | None = None,
        versions: dict[str, ExplorerVersion] | None = None,
    ) -> "AgentConfigBuilder":
        """Configure the Explorer tier behind ``ask_explorer``.

        The tier is a user setting resolved per execution profile; calling agents
        never see it. Precedence at run time: ``ARTEMIS_EXPLORER_VERSION``, then
        ``versions`` (per-agent override), then the profile knob (``flash_mode``
        for the Flash runner, ``pro_mode`` for the Pro Operator / Validator), then
        ``default_version``.

        Args:
            version: Fallback tier when no profile knob applies ('flash', 'pro', 'ultra')
            default_version: Alias for version
            flash_mode: Tier for the Flash execution profile (FlashRunner)
            pro_mode: Tier for the Pro execution profile (Operator / Validator)
            caching: Gemini explicit context caching for the multi-turn tiers;
                None keeps the per-tier default (off for pro, on for ultra)
            versions: Advanced per-agent override, e.g. ``{"validator": "ultra"}``;
                empty by default so the profile knobs decide
        """
        target_version = version if version is not None else default_version
        updates = {}
        if target_version is not None:
            updates["default_version"] = target_version
        if flash_mode is not None:
            updates["flash_mode"] = flash_mode
        if pro_mode is not None:
            updates["pro_mode"] = pro_mode
        if caching is not None:
            updates["caching"] = caching
        if updates:
            self._explorer = self._explorer.model_copy(update=updates)
        if versions is not None:
            self._explorer_versions = versions
        return self

    def with_explorer_version(self, version: ExplorerVersion) -> "AgentConfigBuilder":
        """Configure the fallback Explorer tier ('flash', 'pro', or 'ultra').

        Only applies when no profile knob matches; prefer :meth:`with_flash_config`
        / :meth:`with_pro_config` (or :meth:`with_explorer`) for the per-profile tiers.
        """
        self._explorer = self._explorer.model_copy(update={"default_version": version})
        return self

    def with_explorer_versions(self, versions: dict[str, ExplorerVersion]) -> "AgentConfigBuilder":
        """Configure the advanced per-agent Explorer tier override (empty by default)."""
        self._explorer_versions = versions
        return self

    def with_denylisted_tools(self, tools: dict[str, list[str]]) -> "AgentConfigBuilder":
        """Configure denylisted tools."""
        self._denylisted_tools = tools
        return self

    def build(self, validate_profiles: bool = True) -> AgentConfig:
        """Build the ARTEMIS AgentConfig object.

        Args:
            default_profile: Name of the default agent profile to use

        Returns:
            A configured AgentConfig object

        Raises:
            ValueError: If default_profile is specified but not found in
            configured profiles
        """
        nb_profiles = len(self._agent_profiles)

        if isinstance(self._default_profile, str):
            profile_name = self._default_profile
            default_profile = self._agent_profiles.get(profile_name, None)
            if default_profile is None:
                raise ValueError(f"Profile '{profile_name}' not found in configured agents")
        elif isinstance(self._default_profile, AgentProfile):
            default_profile = self._default_profile
            if default_profile.name not in self._agent_profiles:
                self.add_profile(default_profile, validate=validate_profiles)
        elif nb_profiles <= 0:
            llm_config = get_default_llm_config()
            default_profile = AgentProfile(
                name=DEFAULT_PROFILE_NAME,
                llm_config=llm_config,
            )
            self.add_profile(default_profile, validate=validate_profiles)
        elif nb_profiles == 1:
            # Select the only one available
            default_profile = next(iter(self._agent_profiles.values()))
        else:
            available_profiles = ", ".join(self._agent_profiles.keys())
            raise ValueError(
                f"You must call with_default_profile() to select one among: {available_profiles}"
            )

        device_id = (
            self._device_id
            or os.environ.get("ARTEMIS_DEVICE_ID")
            or os.environ.get("ADB_DEVICE_SERIAL")
        )

        return AgentConfig(
            agent_profiles=self._agent_profiles,
            task_request_defaults=self._task_request_defaults or TaskRequestCommon(),
            default_profile=default_profile,
            device_id=device_id,
            device_platform=self._device_platform,
            servers=self._servers,
            graph_config_callbacks=self._graph_config_callbacks,
            video_recording_tools_enabled=self._video_recording_tools_enabled,
            force_web_accessibility=self._force_web_accessibility,
            disable_checker=self._disable_checker,
            disable_midway_checks=self._disable_midway_checks,
            disable_final_check=self._disable_final_check,
            checker_max_iterations=self._checker_max_iterations,
            final_check_max_attempts=self._final_check_max_attempts,
            checkpoint_max_repairs=self._checkpoint_max_repairs,
            max_concurrent_checkpoints=self._max_concurrent_checkpoints,
            checkpoint_timeout=self._checkpoint_timeout,
            settlement_timeout=self._settlement_timeout,
            assert_failure_policy=self._assert_failure_policy,
            disable_device_probes=self._disable_device_probes,
            disable_planner_validation=self._disable_planner_validation,
            enable_committee=self._enable_committee,
            committee_debate_rounds=self._committee_debate_rounds,
            disable_outputter=self._disable_outputter,
            outputter=self._outputter,
            flash=self._flash,
            pro=self._pro,
            explorer=self._explorer,
            explorer_versions=self._explorer_versions,
            denylisted_tools=self._denylisted_tools,
            enable_video_ledger=self._enable_video_ledger,
            video_analyzer=self._video_analyzer.model_copy(
                update={"enable_ledger": self._enable_video_ledger}
            ),
            concurrency_mode=self._concurrency_mode,
            max_concurrency=self._max_concurrency,
        )


def get_default_agent_config():
    return AgentConfigBuilder().build()


def get_default_servers():

    host = settings.ADB_HOST or os.environ.get("ADB_HOST", "localhost")
    port_val = settings.ADB_PORT
    if not port_val:
        port_str = os.environ.get("ADB_PORT", "5037")
        port_val = int(port_str) if port_str.isdigit() else 5037

    return ServerConfig(
        adb_host=host,
        adb_port=port_val,
    )
