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

"""Standardized exception hierarchy for ARTEMIS."""


class ArtemisException(Exception):
    """Base exception for all ARTEMIS framework errors."""

    pass


class DeviceDriverException(ArtemisException):
    """Raised when hardware, emulator, or ADB interactions fail."""

    pass


class DeviceNotFoundException(DeviceDriverException):
    """Raised when the specified target device cannot be located or connected."""

    pass


class AgentExecutionException(ArtemisException):
    """Raised when an autonomous agent fails during reasoning or execution."""

    pass


class ToolExecutionException(ArtemisException):
    """Raised when a tool action fails or receives invalid arguments."""

    pass


class LLMProviderException(ArtemisException):
    """Raised when an upstream LLM/VLM API call fails or times out."""

    pass


class StateTransitionException(ArtemisException):
    """Raised when graph execution encounters an invalid state transition."""

    pass
