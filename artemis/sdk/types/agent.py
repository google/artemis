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

from typing import Literal
from urllib.parse import urlparse

from langchain_core.callbacks.base import Callbacks
from artemis.config import (
    ExplorerConfig,
    FlashProfileConfig,
    OutputterConfig,
    ProProfileConfig,
)
from artemis.context import DevicePlatform
from artemis.sdk.types.task import AgentProfile, TaskRequestCommon
from artemis.utils.video import detect_video_tools_enabled
from pydantic import AliasChoices, BaseModel, Field


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
    pass


class ApiBaseUrl(BaseModel):
    """Defines an API base URL."""

    model_config = {"ignored_types": (CyFunctionDetector,)}
    scheme: Literal["http", "https"]
    host: str
    port: int | None = None

    def __eq__(self, other):
        if not isinstance(other, ApiBaseUrl):
            return False
        return self.to_url() == other.to_url()

    def to_url(self):
        return (
            f"{self.scheme}://{self.host}:{self.port}"
            if self.port is not None
            else f"{self.scheme}://{self.host}"
        )

    @classmethod
    def from_url(cls, url: str) -> "ApiBaseUrl":
        parsed_url = urlparse(url)
        if parsed_url.scheme not in ["http", "https"]:
            raise ValueError(f"Invalid scheme: {parsed_url.scheme}")
        if parsed_url.hostname is None:
            raise ValueError("Invalid hostname")
        return cls(
            scheme=parsed_url.scheme,  # type: ignore
            host=parsed_url.hostname,
            port=parsed_url.port,
        )


class ServerConfig(BaseModel):
    """Configuration for the required servers."""

    model_config = {"ignored_types": (CyFunctionDetector,)}

    adb_host: str
    adb_port: int


class AgentConfig(BaseModel):
    """ARTEMIS agent configuration.

    Attributes:
        agent_profiles: Map an agent profile name to its configuration.
        task_config_defaults: Default task request configuration.
        default_profile: default profile to use for tasks
        device_id: Specific device to target (if None, first available is used).
        device_platform: Platform of the device to target.
        servers: Custom server configurations.
    """

    agent_profiles: dict[str, AgentProfile]
    task_request_defaults: TaskRequestCommon
    default_profile: AgentProfile
    device_id: str | None = None
    device_platform: DevicePlatform | None = None
    servers: ServerConfig
    graph_config_callbacks: Callbacks = None
    video_recording_tools_enabled: bool = Field(default_factory=detect_video_tools_enabled)
    force_web_accessibility: bool = False
    disable_checker: bool = True
    checker_max_iterations: int = 20
    checker_max_chat_rounds: int = 4
    disable_planner_validation: bool = True
    planner_validation_threshold: float = 0.85
    enable_committee: bool = False
    committee_debate_rounds: int = 2
    disable_outputter: bool = False
    outputter: OutputterConfig = Field(default_factory=OutputterConfig)

    flash: FlashProfileConfig = Field(default_factory=FlashProfileConfig)
    pro: ProProfileConfig = Field(default_factory=ProProfileConfig)

    explorer: ExplorerConfig = Field(default_factory=ExplorerConfig)
    explorer_versions: dict[str, Literal["flash", "pro", "ultra"]]
    denylisted_tools: dict[str, list[str]] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("denylisted_tools", "blocklisted_tools"),
    )
    enable_video_ledger: bool = True

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
