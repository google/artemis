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

"""Pro-graph step summarizer node (M2: visual-transition lens dispatch).

Per the history-module redesign §5 (single step-level lens, confirmed
2026-08-31) this node no longer generates a per-step text capsule with its own
LLM call. It dispatches the step to the shared
:class:`~artemis.memory.step_memory.StepMemoryService` visual-transition lens
(``ctx.step_memory``): pre/post screenshots are pulled from the DataEngine by
step id, the red action-overlay and the ``flash_summarizer.md`` neutral-wording
contract apply unchanged, and the result lands as a versioned
``source="visual_transition"`` summary write (M0 semantics).

The auxiliary agents' compiled view (``build_plan_and_history``) is untouched:
a step whose summary is not ready yet falls back to detailed rendering
(``task_tree.py`` semantics), so the transition is safe. The intent/strategy
capsule moves up to the chunk-level ``StepCapsuleLens`` in M3.
"""

import json
from typing import Any

from artemis.context import ArtemisContext
from artemis.data_engine.trace import trace
from artemis.graph.state import State
from artemis.graph.visibility import strict_state
from artemis.memory import ensure_step_memory
from artemis.utils.logger import get_logger
from artemis.utils.task_tree import format_result_clean

logger = get_logger(__name__)


class SummarizerNode:
    def __init__(self, ctx: ArtemisContext):
        self.ctx = ctx

    @trace(type="agent", name="summarizer")
    async def __call__(self, state: State):
        state = strict_state(state, "summarizer")
        if not (self.ctx.data_engine and state.current_step_id):
            return {}
        try:
            self._dispatch_visual_summary(state)
        except Exception as e:
            logger.error(f"Failed to dispatch visual step summary: {e}")
        return {}

    def _dispatch_visual_summary(self, state: State) -> None:
        engine = self.ctx.data_engine
        step_id = state.current_step_id

        step_number = None
        try:
            step_number = engine.get_step_number(step_id)
        except Exception as e:
            logger.warning(f"Could not resolve step number for {step_id}: {e}")

        record = None
        if isinstance(step_number, int):
            try:
                record = engine.get_step_record(step_number)
            except Exception as e:
                logger.warning(f"Could not load step record {step_number}: {e}")

        # Graceful degradation: a missing pre/post image simply dispatches with
        # None bytes — the lens renders a text-only transition account.
        pre_bytes = self._read_step_image(step_number, "pre")
        post_bytes = self._read_step_image(step_number, "post")

        action_name, action_args = self._extract_action(record, state)
        result = getattr(state, "last_execution_result", None)
        # format_result_clean reports only errors/repairs; a clean run keeps
        # the controller's bare status word.
        exec_outcome = format_result_clean(result)
        if not exec_outcome:
            if isinstance(result, dict) and result.get("status"):
                exec_outcome = str(result["status"])
            else:
                exec_outcome = "unknown"

        service = ensure_step_memory(self.ctx)
        service.dispatch(
            step_number=step_number if isinstance(step_number, int) else 0,
            action_name=action_name,
            action_args=action_args,
            pre_img_bytes=pre_bytes,
            post_img_bytes=post_bytes,
            exec_outcome=exec_outcome,
            data_engine_step_id=step_id,
        )
        logger.info(f"Dispatched visual transition summary for step {step_number} ({step_id})")

    def _read_step_image(self, step_number: int | None, which: str) -> bytes | None:
        if not isinstance(step_number, int):
            return None
        try:
            path = self.ctx.data_engine.get_step_image_path(step_number, which)
            if path is None:
                return None
            return path.read_bytes()
        except Exception as e:
            logger.warning(f"Could not read {which} image for step {step_number}: {e}")
            return None

    @staticmethod
    def _extract_action(record: Any, state: State) -> tuple[str, dict]:
        actions = getattr(record, "action_taken", None) if record is not None else None
        if not actions:
            decisions = getattr(state, "structured_decisions", None)
            if decisions:
                try:
                    actions = json.loads(decisions)
                except Exception:
                    actions = None
        if isinstance(actions, dict):
            actions = [actions]
        if not isinstance(actions, list) or not actions or not isinstance(actions[0], dict):
            return "no_op", {}
        first = actions[0]
        action_name = first.get("action") or "unknown"
        action_args = {k: v for k, v in first.items() if k != "action"}
        if len(actions) > 1:
            action_args["additional_actions"] = actions[1:]
        return action_name, action_args
