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

"""Execution failures passed from the Validator to the Operator.

Incidents are stored in ``state.open_incident`` and the step's
``last_execution_result``. Consecutive failures increment the count; a
successful terminal action closes the incident. Prompt rendering lives in
``artemis.agents.operator.prompts.ExecutionIncidentPromptComponent``.
"""

from dataclasses import asdict, dataclass, field
import time
from typing import Any

from artemis.agents.validator.categories import ValidationErrorCategory

#: The safety net refused to execute the action (target missing/shifted/covered).
KIND_SAFETY_NET = "safety_net"
#: The action was dispatched but the device/executor reported a failure.
KIND_EXEC_ERROR = "exec_error"

#: Keys of an action item that are bulky or internal and never useful to the
#: Operator when the incident is rendered or persisted.
_ACTION_ITEM_DROP_KEYS = frozenset(
    {
        "attempts",
        "safety_net_evidence",
        "thought",
    }
)


@dataclass
class ExecutionIncident:
    """One blocked turn-ending action, as reported to the Operator."""

    kind: str
    category: str
    reason: str
    action: dict[str, Any]
    action_description: str
    action_index: int = 0
    burst_size: int = 1
    step_number: int | None = None
    consecutive_failures: int = 1
    evidence: dict[str, Any] = field(default_factory=dict)
    opened_at: float = field(default_factory=time.time)

    @property
    def is_burst(self) -> bool:
        return self.burst_size > 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ExecutionIncident | None":
        if not isinstance(data, dict) or not data.get("kind"):
            return None
        known = {f for f in cls.__dataclass_fields__}
        # Older step ledgers may lack these fields.
        kwargs: dict[str, Any] = {
            "category": str(ValidationErrorCategory.GENERAL),
            "reason": "",
            "action": {},
            "action_description": "",
        }
        kwargs.update({k: v for k, v in data.items() if k in known})
        return cls(**kwargs)


def sanitize_action_item(action_item: dict[str, Any]) -> dict[str, Any]:
    """A copy of the action item without bulky/internal keys."""
    return {k: v for k, v in (action_item or {}).items() if k not in _ACTION_ITEM_DROP_KEYS}


def open_incident(
    *,
    previous: dict[str, Any] | None,
    kind: str,
    category: ValidationErrorCategory | str,
    reason: str,
    action_item: dict[str, Any],
    action_description: str,
    action_index: int = 0,
    burst_size: int = 1,
    step_number: int | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Builds the incident dict for a failed action.

    Continue the failure count from ``previous`` if an incident is still open.
    """
    prior = ExecutionIncident.from_dict(previous)
    consecutive = (prior.consecutive_failures + 1) if prior else 1
    incident = ExecutionIncident(
        kind=kind,
        category=str(category.value if isinstance(category, ValidationErrorCategory) else category),
        reason=(reason or "").strip(),
        action=sanitize_action_item(action_item),
        action_description=action_description,
        action_index=action_index,
        burst_size=burst_size,
        step_number=step_number,
        consecutive_failures=consecutive,
        evidence=dict(evidence or {}),
    )
    return incident.to_dict()
