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

from typing import Annotated

from pydantic import BaseModel, ConfigDict

from artemis.utils.logger import get_logger

logger = get_logger(__name__)


def take_last(a, b):
    """Reducer function keeping the latest value."""
    return b


def sticky_or(a, b):
    """Reducer that latches True: once set, the flag survives later updates."""
    return bool(a) or bool(b)


class State(BaseModel):
    """Graph channel state.

    Every field is explicit (``extra="forbid"``): an undeclared write or an
    unknown constructor key fails loudly instead of becoming a silent ghost
    key. Undeclared *reads* (``getattr(state, k, default)``) are guarded by the
    node visibility manifest in ``artemis.graph.visibility`` instead.
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="forbid",
    )

    # ── Control plane (cross-turn signals & routing) ─────────────────────
    initial_goal: Annotated[str, "Initial goal given by the user"]
    injected_instruction: Annotated[
        str | None,
        "Injected instruction from user for micro-adjustment",
        take_last,
    ] = None
    user_stop_requested: Annotated[
        bool,
        "Latched True once the user externally signals stop (release_loop),"
        " unlocking completion of [Loop:continuous] milestones",
        sticky_or,
    ] = False
    checker_success: Annotated[
        bool | None,
        "True if checker succeeded or not triggered, False if failed",
        take_last,
    ] = None
    operator_feedback: Annotated[
        list[str] | None,
        "Source-tagged findings ([checker]/[planner]/[final check]) injected"
        " append-only into the Operator's next prompt (never switches the"
        " prompt template)",
        take_last,
    ] = None
    run_outcome: Annotated[
        dict | None,
        "Machine-readable run outcome (task_status + test summary), populated"
        " by exit settlement before END",
        take_last,
    ] = None
    exit_settlement_route: Annotated[
        str | None,
        "Routing decision produced by exit_settlement_node ('continue' | 'end')",
        take_last,
    ] = None

    # ── Perception (moves behind PerceptionStore in Phase 2) ─────────────
    latest_ui_hierarchy: Annotated[
        list[dict] | None, "Latest UI hierarchy of the device", take_last
    ] = None
    latest_screenshot: Annotated[str | None, "Path to the latest screenshot", take_last] = None
    operator_raw_data: Annotated[
        dict | None,
        "Raw perception data from operator (screenshot, xml, ocr)",
        take_last,
    ] = None
    indexed_points: Annotated[
        list[list[int]] | None,
        "Active coordinate mappings matching indexed text elements",
        take_last,
    ] = None
    indexed_elements: Annotated[
        list[dict] | None,
        "Active interactable elements matching indexed text elements,"
        " containing text, bounds, class, and center coordinates.",
        take_last,
    ] = None

    # ── Turn products (previous turn's decisions & results) ──────────────
    current_step_id: Annotated[str | None, "Current step ID from data engine", take_last] = None
    structured_decisions: Annotated[
        str | None,
        "Structured decisions made by the operator, for the validator to follow",
        take_last,
    ] = None
    operator_raw_thinking: Annotated[
        str | None,
        "Raw thinking process of the operator",
        take_last,
    ] = None
    operator_native_thinking: Annotated[
        str | None,
        "Native/implicit thinking process of the operator",
        take_last,
    ] = None
    last_execution_result: Annotated[
        dict | None, "Last execution result from validator", take_last
    ] = None
    subagent_calls: Annotated[
        list[str], "List of sub-agent calls in current session", take_last
    ] = []
    operator_tool_limit_exceeded: Annotated[
        bool | None,
        "True if operator exceeded tool call limit in the previous turn",
        take_last,
    ] = None

    @classmethod
    def initial(cls, goal: str) -> "State":
        """Single source for the graph's initial state.

        Every entrypoint (SDK, engine runners, tests) must construct the
        first State through here so required fields stay in one place.
        """
        return cls(initial_goal=goal)
