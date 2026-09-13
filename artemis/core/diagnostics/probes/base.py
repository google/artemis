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

"""Abstract Base Probe for Artemis System Diagnostics."""

from abc import ABC, abstractmethod
from artemis.core.diagnostics.schema import ProbeCategory, ProbeResult


class BaseProbe(ABC):
    """Abstract interface representing a system readiness probe."""

    @property
    @abstractmethod
    def probe_id(self) -> str:
        """Unique identifier for this probe."""
        pass

    @property
    @abstractmethod
    def category(self) -> ProbeCategory:
        """Category to which this probe belongs."""
        pass

    @property
    @abstractmethod
    def is_blocker(self) -> bool:
        """Whether a failure in this probe should block autonomous task execution."""
        pass

    @abstractmethod
    async def probe(self) -> ProbeResult:
        """Execute the probe inspection and return a structured ProbeResult."""
        pass
