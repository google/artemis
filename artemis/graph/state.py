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

from typing import Annotated, Any
from langchain_core.messages import AnyMessage
from langgraph.graph import add_messages
from artemis.config import AgentNode
from artemis.context import ArtemisContext
from artemis.utils.logger import get_logger
from pydantic import BaseModel, ConfigDict

logger = get_logger(__name__)


def take_last(a, b):
    """Reducer function keeping the latest value."""
    return b


class State(BaseModel):
    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="allow",
    )

    messages: Annotated[list[AnyMessage], "Sequential messages", add_messages]

    remaining_steps: Annotated[int | None, "Remaining steps before the task is completed"] = None

    # planner related keys
    initial_goal: Annotated[str, "Initial goal given by the user"]
    injected_instruction: Annotated[
        str | None,
        "Injected instruction from user for micro-adjustment",
        take_last,
    ] = None

    # operator related keys (perception)
    latest_ui_hierarchy: Annotated[
        list[dict] | None, "Latest UI hierarchy of the device", take_last
    ]
    latest_screenshot: Annotated[str | None, "Path to the latest screenshot", take_last]
    focused_app_info: Annotated[str | None, "Focused app info", take_last]
    device_date: Annotated[str | None, "Date of the device", take_last]

    # operator related keys (decisions)
    structured_decisions: Annotated[
        str | None,
        "Structured decisions made by the operator, for the validator to follow",
        take_last,
    ]
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
    short_term_memory: Annotated[
        str | None,
        "Short term memory / scratchpad for the operator",
        take_last,
    ] = None
    complete_subgoals_by_ids: Annotated[
        list[str],
        "List of subgoal IDs to complete",
        take_last,
    ]
    current_agent: Annotated[str | None, "Current active agent", take_last] = None
    operator_replan_reason: Annotated[str | None, "Reason for operator replan", take_last] = None
    subgoal_plan: Annotated[list[Any] | None, "Subgoal plan", take_last] = None
    operator_tactical_plan: Annotated[list[Any] | None, "Operator tactical plan", take_last] = None

    # validator related keys
    validator_messages: Annotated[list[AnyMessage], "Sequential Validator messages", add_messages]
    last_execution_result: Annotated[
        dict | None, "Last execution result from validator", take_last
    ] = None
    current_step_id: Annotated[str | None, "Current step ID from data engine", take_last] = None
    operator_raw_data: Annotated[
        dict | None,
        "Raw perception data from operator (screenshot, xml, ocr)",
        take_last,
    ] = None
    checker_success: Annotated[
        bool | None,
        "True if checker succeeded or not triggered, False if failed",
        take_last,
    ] = None
    subagent_calls: Annotated[
        list[str], "List of sub-agent calls in current session", take_last
    ] = []
    operator_replied: Annotated[bool | None, "True if operator replied to checker", take_last] = (
        None
    )
    operator_tool_limit_exceeded: Annotated[
        bool | None,
        "True if operator exceeded tool call limit in the previous turn",
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

    async def asanitize_update(
        self,
        ctx: ArtemisContext,
        update: dict,
        agent: AgentNode | None = None,
    ):
        """Sanitizes the state update to ensure it is valid and apply side effect logic where required."""
        return update
