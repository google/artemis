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

"""Modular Failure Analyzer Agent adhering to BaseAgent lifecycle."""

from typing import Any
from artemis.agents.base import AgentConfig, AgentRegistry, BaseAgent
from artemis.agents.validator.recovery_strategies import RecoveryStrategyRegistry
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


@AgentRegistry.register("failure_analyzer")
class FailureAnalyzerAgent(BaseAgent):
    """Specialized agent responsible for diagnosing step failures and triggering autonomous recovery."""

    def __init__(
        self,
        config: AgentConfig | None = None,
        ctx: Any | None = None,
        **kwargs: Any,
    ):
        super().__init__(
            name="failure_analyzer",
            role="Autonomous Self-Healing Failure Analyzer",
            description="Diagnoses execution exceptions, analyzes screen discrepancies, and runs self-healing sequences.",
            config=config,
            ctx=ctx,
        )

    async def process(self, state: Any) -> dict[str, Any]:
        """Analyzes failure context and executes appropriate recovery strategy."""
        if not self.driver:
            raise RuntimeError("FailureAnalyzerAgent requires an active device driver in context.")

        failure_context = getattr(state, "failure_context", {}) or {}
        if isinstance(state, dict):
            failure_context = state.get("failure_context", {})

        logger.info(f"FailureAnalyzer diagnosing failure context: {failure_context}")

        recovery_result = await RecoveryStrategyRegistry.attempt_recovery(
            driver=self.driver,
            failure_context=failure_context,
        )

        if recovery_result and recovery_result.success:
            return {
                "status": "recovered",
                "recovery_action": recovery_result.action_type.value,
                "reason": recovery_result.reason,
            }

        return {
            "status": "unrecoverable",
            "reason": "No applicable recovery strategy succeeded.",
        }
