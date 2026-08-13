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

"""Base Agent Protocol & Lifecycle Framework for ARTEMIS.

Provides a unified interface and lifecycle management for all specialized
multimodal agents (Planner, Operator, FailureAnalyzer, Explorer, Summarizer, etc.).
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from artemis.drivers.factory import get_driver
from artemis.utils.logger import get_logger

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)


class AgentConfig(BaseModel):
    """Configuration options for an individual agent."""

    model_name: str | None = Field(default=None, description="LLM/VLM model override")
    temperature: float = Field(default=0.0, description="Sampling temperature")
    max_tokens: int = Field(default=4096, description="Maximum output tokens")
    system_prompt_override: str | None = Field(
        default=None, description="Custom system prompt override"
    )
    enabled_tools: list[str] = Field(
        default_factory=list, description="Tool names enabled for this agent"
    )


class AgentResponse(BaseModel):
    """Standardized structured response from an agent invocation."""

    status: str = Field(
        default="success", description="Execution status ('success', 'retry', 'fail', 'done')"
    )
    thought: str | None = Field(default=None, description="Reasoning and internal monologue")
    action_taken: str | None = Field(default=None, description="Action or tool called")
    output: Any = Field(default=None, description="Output payload or state updates")
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Diagnostic and profiling metadata"
    )


class BaseAgent(ABC):
    """Abstract Base Class defining the standard lifecycle for all ARTEMIS agents."""

    def __init__(
        self,
        name: str,
        role: str,
        description: str,
        config: AgentConfig | None = None,
        ctx: Any | None = None,
    ):
        self.name = name
        self.role = role
        self.description = description
        self.config = config or AgentConfig()
        self.ctx: Any | None = ctx
        self._is_initialized = False

    @property
    def driver(self) -> Any | None:
        """Helper to get the active device driver from the context."""
        if self.ctx:
            return get_driver(self.ctx)
        return None

    async def initialize(self, ctx: Any) -> None:
        """Lifecycle hook: Called once when the agent is loaded into the workflow."""
        self.ctx = ctx
        self._is_initialized = True
        logger.info(f"Agent '{self.name}' ({self.role}) initialized.")

    @abstractmethod
    async def process(self, state: Any) -> dict[str, Any]:
        """Main execution hook: Receives state, reasons, interacts with tools, and returns state updates."""
        ...

    async def cleanup(self) -> None:
        """Lifecycle hook: Called upon session or workflow termination."""
        self._is_initialized = False
        logger.debug(f"Agent '{self.name}' cleanup complete.")

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name!r} role={self.role!r}>"


class AgentRegistry:
    """Central registry and factory for ARTEMIS agents."""

    _registry: dict[str, type[BaseAgent]] = {}

    @classmethod
    def register(cls, name: str | None = None):
        """Decorator to register an agent class with a unique identifier."""

        def decorator(subclass: type[BaseAgent]) -> type[BaseAgent]:
            reg_name = name or subclass.__name__.lower()
            cls._registry[reg_name] = subclass
            return subclass

        return decorator

    @classmethod
    def get_agent_class(cls, name: str) -> type[BaseAgent] | None:
        return cls._registry.get(name.lower())

    @classmethod
    def create_agent(
        cls,
        name: str,
        ctx: Any | None = None,
        config: AgentConfig | None = None,
        **kwargs: Any,
    ) -> BaseAgent:
        """Instantiates an agent by registered name."""
        agent_cls = cls.get_agent_class(name)
        if not agent_cls:
            raise KeyError(
                f"Agent '{name}' is not registered. Available: {list(cls._registry.keys())}"
            )
        return agent_cls(ctx=ctx, config=config, **kwargs)

    @classmethod
    def list_agents(cls) -> list[str]:
        return list(cls._registry.keys())
