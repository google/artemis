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

from pathlib import Path
from typing import Any, Literal

from artemis.config import (
    ExplorerConfig,
    FlashProfileConfig,
    OutputterConfig,
    ProProfileConfig,
    VideoAnalyzerConfig,
)
from artemis.context import DevicePlatform
from artemis.utils.video import detect_video_tools_enabled
from third_party.mobile_use.sdk.types.agent import (
    AgentConfigBase,
    ApiBaseUrl,
    ServerConfig,
)
from third_party.mobile_use.sdk.types.task import AgentProfile
from pydantic import Field

__all__ = ["AgentConfig", "AgentProfile", "ApiBaseUrl", "DevicePlatform", "ServerConfig"]


def _default_video_recording_tools(data: dict[str, Any]) -> bool:
    """Detect the recording toolchain for the configured device platform."""
    platform = data.get("device_platform")
    platform_name = getattr(platform, "value", platform) or DevicePlatform.ANDROID.value
    return detect_video_tools_enabled(str(platform_name))


class AgentConfig(AgentConfigBase):
    """ARTEMIS agent configuration.

    Adds the Checker, Outputter, Explorer, video-analysis and concurrency
    settings to :class:`AgentConfigBase`.
    """

    video_recording_tools_enabled: bool = Field(
        default_factory=lambda data: _default_video_recording_tools(data)
    )
    force_web_accessibility: bool = False
    disable_checker: bool = False
    disable_midway_checks: bool = True
    disable_final_check: bool = False
    checker_max_iterations: int = 20
    final_check_max_attempts: int = 3
    checkpoint_max_repairs: int = 2
    max_concurrent_checkpoints: int = 3
    checkpoint_timeout: float = 180.0
    settlement_timeout: float = 120.0
    assert_failure_policy: Literal["continue", "halt"] = "continue"
    disable_device_probes: bool = False
    disable_planner_validation: bool = False
    enable_committee: bool = False
    committee_debate_rounds: int = 2
    disable_outputter: bool = False
    outputter: OutputterConfig = Field(default_factory=OutputterConfig)

    flash: FlashProfileConfig = Field(default_factory=FlashProfileConfig)
    pro: ProProfileConfig = Field(default_factory=ProProfileConfig)
    explorer: ExplorerConfig = Field(default_factory=ExplorerConfig)
    # Advanced per-agent tier override; empty so the per-profile knobs
    # (``explorer.flash_mode`` / ``explorer.pro_mode``) decide by default.
    explorer_versions: dict[str, Literal["flash", "pro", "ultra"]] = Field(default_factory=dict)
    denylisted_tools: dict[str, list[str]] = Field(default_factory=dict)
    enable_video_ledger: bool = True
    video_analyzer: VideoAnalyzerConfig = Field(default_factory=VideoAnalyzerConfig)
    concurrency_mode: Literal["global", "per_device"] = "per_device"
    max_concurrency: int | None = None
    ios_workspace_path: Path | None = Field(
        default=None,
        description="Optional existing Xcode project/workspace for local iOS first-run approval; no permission grants.",
    )

    model_config = {"arbitrary_types_allowed": True}

    def get_explorer_version(
        self,
        explicit_version: str | None = None,
        agent_name: str | None = "operator",
        profile: str | None = None,
    ) -> Literal["flash", "pro", "ultra"]:
        """Resolves active Explorer version using underlying ExplorerConfig and role overrides."""
        return self.explorer.resolve(
            explicit_version=explicit_version,
            agent_name=agent_name,
            profile=profile,
            per_agent_overrides=self.explorer_versions,
        )
