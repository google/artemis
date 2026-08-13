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

"""Summarizer Agent implementation adhering to BaseAgent lifecycle."""

from typing import Any
from artemis.agents.base import AgentConfig, AgentRegistry, BaseAgent
from artemis.agents.summarizer.summarizer import SummarizerNode
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


@AgentRegistry.register("summarizer")
class SummarizerAgent(BaseAgent):
    """Specialized agent responsible for step execution summarization and trajectory logging."""

    def __init__(
        self,
        config: AgentConfig | None = None,
        ctx: Any | None = None,
        **kwargs: Any,
    ):
        super().__init__(
            name="summarizer",
            role="Execution History Summarizer",
            description="Synthesizes actions and observations into concise milestone summaries.",
            config=config,
            ctx=ctx,
        )
        self._node = SummarizerNode(ctx=ctx) if ctx else None

    async def initialize(self, ctx: Any) -> None:
        await super().initialize(ctx)
        self._node = SummarizerNode(ctx=ctx)

    async def process(self, state: Any) -> dict[str, Any]:
        if not self._node and self.ctx:
            self._node = SummarizerNode(ctx=self.ctx)
        if not self._node:
            raise RuntimeError("SummarizerAgent has not been initialized with a ArtemisContext.")
        return await self._node(state)
