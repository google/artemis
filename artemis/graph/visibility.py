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

"""Per-node State visibility manifests with test-time enforcement.

``extra="forbid"`` on :class:`~artemis.graph.state.State` catches ghost
*writes*; ghost *reads* (``getattr(state, key, default)`` silently returning
the default) can only be caught by policing reads against a declared manifest.

The manifest below is the declared visible domain of every graph node — the
authoritative answer to "which State fields does this agent see / produce".
Enforcement is zero-cost in production: :func:`strict_state` returns the state
unchanged unless ``ARTEMIS_STRICT_STATE=1`` (set in tests), in which case field
reads outside the node's manifest raise :class:`VisibilityError`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from artemis.graph.state import State


class VisibilityError(Exception):
    """A node touched a State field outside its declared visible domain."""


@dataclass(frozen=True)
class NodeVisibility:
    reads: frozenset[str]
    writes: frozenset[str]

    @property
    def visible(self) -> frozenset[str]:
        return self.reads | self.writes


def _vis(reads: set[str], writes: set[str]) -> NodeVisibility:
    return NodeVisibility(reads=frozenset(reads), writes=frozenset(writes))


#: Declared visible domain per node. Reads include fields touched by the tools
#: a node invokes with ``InjectedState`` (they run inside the node's turn).
#: Start generous, tighten over time — an entry here is a documented decision.
NODE_VISIBILITY: dict[str, NodeVisibility] = {
    "perception": _vis(
        reads={"structured_decisions"},
        writes={
            "latest_screenshot",
            "latest_ui_hierarchy",
            "operator_raw_data",
            "injected_instruction",
            "user_stop_requested",
        },
    ),
    "planner": _vis(
        reads={"initial_goal", "latest_screenshot"},
        # Mutates latest_screenshot in place when it captures its own frame
        # (planner runs before perception on the first turn).
        writes={"latest_screenshot"},
    ),
    "operator": _vis(
        reads={
            "initial_goal",
            "injected_instruction",
            "user_stop_requested",
            "operator_feedback",
            "operator_raw_data",
            "latest_ui_hierarchy",
            "latest_screenshot",
            "indexed_points",
            "indexed_elements",
            "short_term_memory",
            "subagent_calls",
            "current_step_id",
            "operator_tool_limit_exceeded",
            "structured_decisions",
            "operator_raw_thinking",
            "operator_native_thinking",
        },
        writes={
            "structured_decisions",
            "operator_raw_thinking",
            "operator_native_thinking",
            "short_term_memory",
            "indexed_points",
            "indexed_elements",
            "current_step_id",
            "subagent_calls",
            "operator_tool_limit_exceeded",
        },
    ),
    "execution_check": _vis(
        reads={
            "operator_raw_data",
            "structured_decisions",
            "operator_raw_thinking",
            "operator_native_thinking",
            "user_stop_requested",
            "initial_goal",
        },
        writes={
            "checker_success",
            "structured_decisions",
            "current_step_id",
            "operator_feedback",
            "subagent_calls",
        },
    ),
    "validator": _vis(
        reads={
            "initial_goal",
            "structured_decisions",
            "latest_screenshot",
            "latest_ui_hierarchy",
            "current_step_id",
            "operator_raw_data",
            "operator_raw_thinking",
            "operator_native_thinking",
            "indexed_points",
            "indexed_elements",
        },
        writes={
            "last_execution_result",
            # In-place turn-scoped mutations (formalized as TurnWorkspace in
            # Phase 2): post-action screenshot and re-derived index mappings.
            "latest_screenshot",
            "indexed_points",
            "indexed_elements",
        },
    ),
    "summarizer": _vis(
        reads={
            "current_step_id",
            "structured_decisions",
            "operator_raw_thinking",
            "operator_native_thinking",
            "last_execution_result",
        },
        writes=set(),
    ),
    "convergence": _vis(
        reads={"checker_success", "user_stop_requested"},
        writes=set(),
    ),
    "exit_settlement": _vis(
        reads={"user_stop_requested", "initial_goal"},
        writes={"exit_settlement_route", "operator_feedback", "run_outcome"},
    ),
}

_STATE_FIELDS = frozenset(State.model_fields.keys())


def strict_enabled() -> bool:
    return os.environ.get("ARTEMIS_STRICT_STATE", "") == "1"


class StateView:
    """Read-policing proxy handed to nodes under ``ARTEMIS_STRICT_STATE=1``.

    Only declared State *fields* are policed; methods and pydantic internals
    pass through. Attribute writes delegate to the underlying State (in-place
    turn mutations are Phase 2 scope) but are still checked against the
    node's write set.
    """

    __slots__ = ("_state", "_node", "_visibility")

    def __init__(self, state: State, node: str):
        object.__setattr__(self, "_state", state)
        object.__setattr__(self, "_node", node)
        object.__setattr__(self, "_visibility", NODE_VISIBILITY[node])

    def __getattr__(self, name: str) -> Any:
        if name in _STATE_FIELDS and name not in self._visibility.visible:
            raise VisibilityError(
                f"Node '{self._node}' read undeclared State field '{name}'."
                " Declare it in NODE_VISIBILITY or stop reading it."
            )
        return getattr(self._state, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _STATE_FIELDS and name not in self._visibility.writes:
            raise VisibilityError(
                f"Node '{self._node}' mutated undeclared State field '{name}'."
                " Declare it in NODE_VISIBILITY or stop writing it."
            )
        setattr(self._state, name, value)


def strict_state(state: State, node: str) -> State | StateView:
    """Wraps ``state`` in a policing view when strict mode is on (tests)."""
    if strict_enabled() and isinstance(state, State):
        return StateView(state, node)
    return state


def check_update(node: str, update: dict | None) -> None:
    """Validates a node's returned update keys against its write manifest."""
    if not update:
        return
    illegal = set(update) & _STATE_FIELDS - NODE_VISIBILITY[node].writes
    if illegal:
        raise VisibilityError(f"Node '{node}' returned undeclared update keys: {sorted(illegal)}.")


def validate_manifest() -> list[str]:
    """Returns problems in the manifest itself (ghost fields). Empty = clean."""
    problems = []
    for node, vis in NODE_VISIBILITY.items():
        for field in vis.visible - _STATE_FIELDS:
            problems.append(f"{node}: '{field}' is not a declared State field")
    return problems
