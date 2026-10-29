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

"""Bound consecutive Operator turns that produce no executable decision."""

from dataclasses import dataclass

from artemis.utils.plan_grammar import parse_plan


class OperatorNoActionLimitError(RuntimeError):
    """A non-success result, never evidence that the task was completed."""

    reason_code = "operator_no_action_limit"


@dataclass
class OperatorProgressGuard:
    max_no_action_turns: int = 4
    consecutive_no_action_turns: int = 0

    def record_turn(
        self,
        actions: list[dict] | None,
        plan_text: str | None,
        *,
        user_stopped: bool = False,
    ) -> None:
        if actions:
            self.consecutive_no_action_turns = 0
            return
        snapshot = parse_plan(plan_text or "")
        # A completed plan still goes through the graph's normal settlement
        # and assertions. A continuous task is not complete without a stop.
        if snapshot.all_top_level_done and (not snapshot.continuous_top_level or user_stopped):
            self.consecutive_no_action_turns = 0
            return
        self.consecutive_no_action_turns += 1
        if self.consecutive_no_action_turns >= self.max_no_action_turns:
            raise OperatorNoActionLimitError(
                "operator_no_action_limit: stopped after "
                f"{self.consecutive_no_action_turns} consecutive turns without an executable "
                "action or a completed plan. The task is not successful; review the last "
                "ordinary assistant response and any missing input or execution constraint."
            )


def configured_progress_guard() -> OperatorProgressGuard:
    from artemis.config import load_agent_config

    return OperatorProgressGuard(
        max_no_action_turns=load_agent_config().pro.execution.max_no_action_turns
    )
