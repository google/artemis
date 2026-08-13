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

"""Loop, deadlock, and action repetition detector for Operator Agent."""

from typing import Any
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


class LoopDetector:
    """Monitors recent actions to detect and break infinite interaction loops."""

    def __init__(self, repetition_threshold: int = 3):
        self.repetition_threshold = repetition_threshold
        self._action_signatures: list[str] = []

    def record_action(self, action_name: str, params: dict[str, Any]) -> None:
        sig = f"{action_name}:{sorted(params.items())}"
        self._action_signatures.append(sig)

    def is_loop_detected(self) -> bool:
        """Returns True if the identical action has been repeated >= threshold times."""
        if len(self._action_signatures) < self.repetition_threshold:
            return False
        last_sig = self._action_signatures[-1]
        return all(s == last_sig for s in self._action_signatures[-self.repetition_threshold :])

    def reset(self) -> None:
        self._action_signatures.clear()
