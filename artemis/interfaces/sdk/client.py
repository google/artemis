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

from typing import Any, Literal
from collections.abc import AsyncGenerator, Callable
from artemis.engine.pipeline import Pipeline
from artemis.interfaces.sdk.task import StreamEvent, StreamEventType, TaskResult
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


class ArtemisClient:
    """The official high-level developer entrypoint for interacting with ARTEMIS."""

    def __init__(
        self, device_id: str = "default-device", default_profile: Literal["flash", "pro"] = "pro"
    ):
        self.device_id = device_id
        self.default_profile = default_profile

    async def run(
        self,
        goal: str,
        profile: Literal["flash", "pro"] | None = None,
        max_turns: int = 30,
        **kwargs: Any,
    ) -> TaskResult:
        """Executes a single autonomous task and returns the final TaskResult."""
        target_profile = profile or self.default_profile
        logger.info(f"ArtemisClient running task '{goal}' with profile '{target_profile}'...")

        state = await Pipeline.execute(
            goal=goal,
            profile=target_profile,
            device_id=self.device_id,
        )

        return TaskResult(
            trace_id=state.trace_id,
            status=state.status.value,
            turns=state.current_turn,
            error=state.error_message,
        )

    async def stream_run(
        self,
        goal: str,
        profile: Literal["flash", "pro"] | None = None,
        max_turns: int = 30,
        callbacks: list[Callable[[StreamEvent], Any]] | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Executes an autonomous task while yielding real-time StreamEvents asynchronously."""
        target_profile = profile or self.default_profile
        logger.info(f"ArtemisClient streaming task '{goal}' with profile '{target_profile}'...")

        # 1. Emit initial starting event
        start_event = StreamEvent(
            event_type=StreamEventType.STATUS,
            step_number=0,
            payload={
                "status": "starting",
                "goal": goal,
                "profile": target_profile,
                "device_id": self.device_id,
            },
        )
        if callbacks:
            for cb in callbacks:
                try:
                    cb(start_event)
                except Exception as e:
                    logger.debug(f"Callback error: {e}")
        yield start_event

        # 2. Execute task pipeline
        try:
            state = await Pipeline.execute(
                goal=goal,
                profile=target_profile,
                device_id=self.device_id,
            )

            # 3. Emit intermediate step events
            for step in state.steps:
                ev = StreamEvent(
                    event_type=StreamEventType.STEP_END,
                    step_number=step.step_number,
                    payload={
                        "thought": step.thought,
                        "action": step.action_name,
                        "params": step.action_params,
                        "result": step.result,
                        "duration": step.duration_seconds,
                    },
                )
                if callbacks:
                    for cb in callbacks:
                        try:
                            cb(ev)
                        except Exception as e:
                            logger.debug(f"Callback error: {e}")
                yield ev

            # 4. Emit completion event
            final_ev = StreamEvent(
                event_type=StreamEventType.STATUS,
                step_number=state.current_turn,
                payload={
                    "status": state.status.value,
                    "turns": state.current_turn,
                    "error": state.error_message,
                },
            )
            if callbacks:
                for cb in callbacks:
                    try:
                        cb(final_ev)
                    except Exception as e:
                        logger.debug(f"Callback error: {e}")
            yield final_ev

        except Exception as e:
            err_ev = StreamEvent(
                event_type=StreamEventType.ERROR,
                step_number=0,
                payload={"error": str(e)},
            )
            if callbacks:
                for cb in callbacks:
                    try:
                        cb(err_ev)
                    except Exception:
                        pass
            yield err_ev
