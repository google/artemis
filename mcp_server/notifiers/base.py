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

"""Base interface for MCP notification adapters."""

from abc import ABC, abstractmethod
from typing import Any


class BaseNotifier(ABC):
    """Abstract base class for environment notification providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Name of the notifier adapter."""
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """Checks if the notifier is configured and available in the current runtime environment."""
        pass

    @abstractmethod
    def notify(
        self,
        conversation_id: str,
        message: str,
        title: str | None = None,
        event_type: str = "completed",
        payload: dict[str, Any] | None = None,
    ) -> bool:
        """Sends a notification to the target conversation / client."""
        pass
