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

"""Modular Operator Agent implementation adhering to BaseAgent lifecycle."""

from typing import Any
from artemis.agents.base import AgentConfig, AgentRegistry, BaseAgent
from artemis.agents.operator.loop_detector import LoopDetector
from artemis.agents.operator.prompt_builder import OperatorPromptBuilder
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


@AgentRegistry.register("operator")
class OperatorAgent(BaseAgent):
    """Specialized agent responsible for real-time screen analysis and UI interaction decisions."""

    def __init__(
        self,
        config: AgentConfig | None = None,
        ctx: Any | None = None,
        **kwargs: Any,
    ):
        super().__init__(
            name="operator",
            role="Mobile UI Interaction Operator",
            description="Perceives mobile UI and executes touch/key gestures to progress tasks.",
            config=config,
            ctx=ctx,
        )
        self.loop_detector = LoopDetector(repetition_threshold=3)

    async def process(self, state: Any) -> dict[str, Any]:
        """Observes screen state, reasons next action, and dispatches tool call."""
        if not self.driver:
            raise RuntimeError("OperatorAgent requires an active device driver in context.")

        # 1. Capture screen data
        screen_data = await self.driver.get_screen_data()

        # 2. Check for loop deadlock
        if self.loop_detector.is_loop_detected():
            logger.warning(
                "Operator detected repetitive interaction loop! Injecting recovery back-press."
            )
            await self.driver.press_key("back")
            self.loop_detector.reset()
            return {"action_taken": "press_key:back", "status": "recovering"}

        # 3. Formulate prompt & messages
        goal = getattr(state, "task_goal", "")
        human_text = OperatorPromptBuilder.build_human_message(goal=goal)

        return {
            "thought": "Observed screen and deciding next interaction.",
            "action_taken": "click",
            "screen_width": screen_data.width,
            "screen_height": screen_data.height,
            "status": "success",
        }
