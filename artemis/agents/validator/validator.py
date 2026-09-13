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

"""ValidatorNode facade: action execution + pre-execution safety net.

The implementation is split by responsibility:

- :mod:`artemis.agents.validator.execution_loop` -- per-turn loop
  orchestration, screenshot/trace bookkeeping, failure-repair handling.
- :mod:`artemis.agents.validator.action_execution` -- decision parsing,
  turn-initial screenshot loading, single-action dispatch.
- :mod:`artemis.agents.validator.precondition_xml` -- XML-hierarchy safety
  net (element matching, coordinate self-healing, failure taxonomy).
- :mod:`artemis.agents.validator.precondition_pixel` -- pixel/VLM safety net.

This module remains the stable import path: ``ValidatorNode``,
``ValidationErrorCategory``, and the patchable seams (``get_action_session``,
``get_llm``, ``VALIDATOR_POLL_TIMEOUT``, ``VALIDATOR_POLL_INTERVAL``) are all
resolved here so existing imports and ``mock.patch`` targets keep working.
"""

import asyncio
import traceback
from uuid import UUID

from artemis.agents.validator import (
    action_execution,
    execution_loop,
    precondition_pixel,
    precondition_xml,
)
from artemis.agents.validator.categories import ValidationErrorCategory
from artemis.constants import (  # noqa: F401  (patchable seams; the UI-change
    # polling path that consumed the poll constants was removed as dead code,
    # but the names stay importable/patchable at this module path.)
    VALIDATOR_POLL_INTERVAL,
    VALIDATOR_POLL_TIMEOUT,
    VALIDATOR_UI_HIERARCHY_TIMEOUT,
)
from artemis.context import ArtemisContext
from artemis.data_engine.trace import trace
from artemis.graph.state import State
from artemis.graph.visibility import strict_state
from artemis.mcp.action_session import ActionSession, get_action_session
from artemis.services.llm import acomplete_structured, get_llm  # noqa: F401
from artemis.utils.decorators import wrap_with_callbacks
from artemis.utils.logger import get_logger

logger = get_logger(__name__)

__all__ = ["ValidatorNode", "ValidationErrorCategory"]


class ValidatorNode:
    """Node responsible for executing actions on the device and verifying the results.

    Despite its name, this node handles:
    1. Parsing structured decisions from the Operator.
    2. Executing these actions via an MCP session (calling tools like tap,
    swipe, etc.).
    3. Validating action preconditions against the live screen (safety net).
    4. Triggering failure analysis and local repair if actions fail.

    Post-hoc goal verification lives elsewhere: the Checker performs the
    checkpoint/final audits at subgoal completion and task exit.
    """

    def __init__(self, ctx: ArtemisContext):
        self.ctx = ctx

    async def _get_mcp_session(self) -> ActionSession:
        """Returns the in-process unified action session (created lazily on ctx).

        The previous stdio-subprocess spawn -- and its local-controller fallback,
        which on Windows was the *only* path that ever ran -- is gone: the server now
        lives in-process, so there is no spawn to fail and exactly one execution path.
        """
        return await get_action_session(self.ctx)

    def _parse_decisions(self, structured_decisions: str) -> tuple[list[dict] | None, str | None]:
        return action_execution.parse_decisions(structured_decisions)

    async def _get_initial_screenshot(self, session, state: State) -> tuple[str, str | None]:
        return await action_execution.read_initial_screenshot(state)

    async def _execute_validation_loop(self, state: State):
        return await execution_loop.run_validation_loop(self, state)

    @wrap_with_callbacks(
        before=lambda: logger.info("Starting Validator Agent..."),
        on_success=lambda _: logger.success("Validator Agent"),
        on_failure=lambda _: logger.error("Validator Agent"),
    )
    @trace(type="agent", name="validator")
    async def __call__(self, state: State):
        state = strict_state(state, "validator")

        step_id = None
        if state.current_step_id:
            step_id = UUID(state.current_step_id)

        original_step_id = getattr(self.ctx.data_engine, "current_step_id", None)
        if self.ctx.data_engine and step_id:
            self.ctx.data_engine.current_step_id = step_id

        try:
            return await self._execute_validation_loop(state)
        except asyncio.CancelledError:
            # In-process session teardown happens in ArtemisContext.__aexit__; there
            # is no subprocess left to kill.
            logger.warning("Validator task cancelled (Stop requested).")
            raise
        except Exception as e:
            err_stack = traceback.format_exc()
            logger.critical(f"CRITICAL ERROR in Validator execution loop: {e}\n{err_stack}")
            if self.ctx.data_engine and step_id:
                try:
                    self.ctx.data_engine.update_step_execution_result(
                        step_id,
                        {
                            "status": "error",
                            "error_msg": str(e),
                            "traceback": err_stack,
                        },
                    )
                except Exception as record_err:
                    logger.debug(
                        "Could not persist validator error status for step %s: %s",
                        step_id,
                        record_err,
                        exc_info=True,
                    )
            raise e
        finally:
            if self.ctx.data_engine:
                self.ctx.data_engine.current_step_id = original_step_id

    async def _exec_action(self, session: ActionSession, action_item: dict) -> tuple[bool, str]:
        """Executes one Operator action item through the unified action session."""
        return await action_execution.exec_action(self.ctx, session, action_item)

    @trace(type="tool", name="safety_net_validation")
    async def _validate_action_precondition(
        self, session, action_item: dict, state: State | None = None
    ) -> tuple[bool, ValidationErrorCategory, str]:
        """Safety Net: XML-hierarchy validation with a short retry loop."""
        return await precondition_xml.validate_action_precondition(
            self, session, action_item, state
        )

    async def _validate_action_precondition_single(
        self, session, action_item: dict, state: State | None = None
    ) -> tuple[bool, ValidationErrorCategory, str]:
        """Safety Net: single-shot XML-hierarchy validation."""
        return await precondition_xml.validate_action_precondition_single(
            self.ctx, session, action_item, state
        )

    @trace(type="tool", name="safety_net_pixel_validation")
    async def _validate_action_precondition_pixel(
        self,
        session,
        action_item: dict,
        pre_screenshot_b64: str,
        original_coords: list[int] | None = None,
        state: State | None = None,
    ) -> tuple[bool, ValidationErrorCategory, str]:
        """Pixel-level Safety Net: VLM comparison of the target crop.

        ``get_llm`` is passed through from this module's namespace so tests
        patching ``artemis.agents.validator.validator.get_llm`` take effect.
        """
        return await precondition_pixel.validate_action_precondition_pixel(
            self.ctx,
            session,
            action_item,
            pre_screenshot_b64,
            original_coords,
            state,
            get_llm_fn=get_llm,
        )
