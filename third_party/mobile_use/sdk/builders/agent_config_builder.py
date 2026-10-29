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

"""Builder for AgentConfig objects using a fluent interface."""

import copy
from typing import Any, ClassVar, Self

from langchain_core.callbacks.base import Callbacks
from artemis.config import get_default_llm_config
from artemis.sdk.constants import DEFAULT_PROFILE_NAME
from third_party.mobile_use.context import DevicePlatform
from third_party.mobile_use.sdk.types.agent import AgentConfigBase, ServerConfig
from third_party.mobile_use.sdk.types.task import AgentProfile, TaskRequestCommon


class AgentConfigBuilderBase:
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

    Subclasses extend the built configuration by setting ``config_class`` and
    overriding :meth:`_extra_config_fields`.
    """

    config_class: ClassVar[type[AgentConfigBase]] = AgentConfigBase

    def __init__(self, servers: ServerConfig):
        """Initialize an empty AgentConfigBuilder."""
        self._agent_profiles: dict[str, AgentProfile] = {}
        self._task_request_defaults: TaskRequestCommon | None = None
        self._default_profile: str | AgentProfile | None = None
        self._device_id: str | None = None
        self._device_platform: DevicePlatform | None = None
        self._servers: ServerConfig = servers
        self._graph_config_callbacks: Callbacks = None
        self._cloud_mobile_id_or_ref: str | None = None

    def add_profile(self, profile: AgentProfile, validate: bool = True) -> Self:
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
    ) -> Self:
        """Add multiple agent profiles to the ARTEMIS agent.

        Args:
            profiles: List of agent profiles to add
        """
        for profile in profiles:
            self.add_profile(profile=profile, validate=validate)
        return self

    def with_default_profile(self, profile: str | AgentProfile) -> Self:
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
    ) -> Self:
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

    def with_default_task_config(self, config: TaskRequestCommon) -> Self:
        """Set the default task configuration.

        Args:
            config: The task configuration to use as default
        """
        self._task_request_defaults = copy.deepcopy(config)
        return self

    def with_adb_server(self, host: str, port: int | None = None) -> Self:
        """Set the ADB server host and port.

        Args:
            host: The ADB server host
            port: The ADB server port
        """
        self._servers.adb_host = host
        if port is not None:
            self._servers.adb_port = port
        return self

    def with_servers(self, servers: ServerConfig) -> Self:
        """Set the server settings.

        Args:
            servers: The server settings to use
        """
        self._servers = copy.deepcopy(servers)
        return self

    def with_graph_config_callbacks(self, callbacks: Callbacks) -> Self:
        """Set the graph config callbacks.

        Args:
            callbacks: The graph config callbacks to use
        """
        self._graph_config_callbacks = callbacks
        return self

    def _extra_config_fields(self) -> dict[str, Any]:
        """Fields a subclass adds to (or overrides in) the built configuration."""
        return {}

    def build(self, validate_profiles: bool = True) -> AgentConfigBase:
        """Build the AgentConfig object.

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

        fields: dict[str, Any] = {
            "agent_profiles": self._agent_profiles,
            "task_request_defaults": self._task_request_defaults or TaskRequestCommon(),
            "default_profile": default_profile,
            "device_id": self._device_id,
            "device_platform": self._device_platform,
            "servers": self._servers,
            "graph_config_callbacks": self._graph_config_callbacks,
        }
        fields.update(self._extra_config_fields())
        return self.config_class(**fields)
