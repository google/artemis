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

"""Planner Agent implementation adhering to BaseAgent lifecycle."""

from typing import Any
from artemis.agents.base import AgentConfig, AgentRegistry, BaseAgent
from artemis.agents.planner.planner import PlannerNode
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


@AgentRegistry.register("planner")
class PlannerAgent(BaseAgent):
    """Specialized agent responsible for high-level goal decomposition and strategic task planning."""

    def __init__(
        self,
        config: AgentConfig | None = None,
        ctx: Any | None = None,
        **kwargs: Any,
    ):
        super().__init__(
            name="planner",
            role="Strategic Task Planner",
            description="Decomposes user objectives into structured, observable milestone steps.",
            config=config,
            ctx=ctx,
        )
        self._node = PlannerNode(ctx=ctx, **kwargs) if ctx else None

    async def initialize(self, ctx: Any) -> None:
        await super().initialize(ctx)
        self._node = PlannerNode(ctx=ctx)

    async def process(self, state: Any) -> dict[str, Any]:
        if not self._node and self.ctx:
            self._node = PlannerNode(ctx=self.ctx)
        if not self._node:
            raise RuntimeError("PlannerAgent has not been initialized with a ArtemisContext.")
        return await self._node(state)
