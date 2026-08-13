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

"""Unit tests for Base Agent Protocol & Lifecycle Registry."""

import pytest
from typing import Any
from artemis.agents.base import AgentConfig, AgentRegistry, BaseAgent
from artemis.agents.validator.failure_analyzer_agent import FailureAnalyzerAgent
from artemis.agents.validator.recovery_strategies import (
    RecoveryStrategyRegistry,
)
from artemis.drivers.mock.mock_driver import MockDeviceDriver


class DummyTestAgent(BaseAgent):
    """Simple test agent implementing BaseAgent protocol."""

    def __init__(self, config: AgentConfig | None = None, ctx: Any | None = None, **kwargs: Any):
        super().__init__(
            name="dummy_test_agent",
            role="Test Worker",
            description="Agent used for testing lifecycle transitions.",
            config=config,
            ctx=ctx,
        )

    async def process(self, state: Any) -> dict[str, Any]:
        return {"status": "success", "processed_state": True}


@pytest.mark.asyncio
async def test_base_agent_lifecycle():
    """Verify BaseAgent initialization, execution, and cleanup lifecycle."""
    agent = DummyTestAgent()
    assert agent.name == "dummy_test_agent"
    assert not agent._is_initialized

    # 1. Initialize
    mock_ctx = object()
    await agent.initialize(mock_ctx)
    assert agent._is_initialized
    assert agent.ctx is mock_ctx

    # 2. Process
    result = await agent.process({})
    assert result.get("status") == "success"
    assert result.get("processed_state") is True

    # 3. Cleanup
    await agent.cleanup()
    assert not agent._is_initialized


def test_agent_registry():
    """Verify AgentRegistry registers and instantiates agents correctly."""
    registered = AgentRegistry.list_agents()
    assert "planner" in registered
    assert "summarizer" in registered
    assert "operator" in registered
    assert "failure_analyzer" in registered

    # Instantiate PlannerAgent via registry
    planner = AgentRegistry.create_agent("planner", ctx=None)
    assert planner.name == "planner"
    assert planner.role == "Strategic Task Planner"

    # Instantiate SummarizerAgent via registry
    summarizer = AgentRegistry.create_agent("summarizer", ctx=None)
    assert summarizer.name == "summarizer"
    assert summarizer.role == "Execution History Summarizer"

    # Instantiate OperatorAgent via registry
    operator = AgentRegistry.create_agent("operator", ctx=None)
    assert operator.name == "operator"
    assert operator.role == "Mobile UI Interaction Operator"

    # Instantiate FailureAnalyzerAgent via registry
    fa = AgentRegistry.create_agent("failure_analyzer", ctx=None)
    assert fa.name == "failure_analyzer"
    assert fa.role == "Autonomous Self-Healing Failure Analyzer"


def test_agent_config():
    """Verify AgentConfig parameters can be customized."""
    config = AgentConfig(
        model_name="gemini-2.5-pro",
        temperature=0.7,
        max_tokens=2048,
        enabled_tools=["click", "swipe"],
    )
    agent = DummyTestAgent(config=config)
    assert agent.config.model_name == "gemini-2.5-pro"
    assert agent.config.temperature == 0.7
    assert agent.config.enabled_tools == ["click", "swipe"]


@pytest.mark.asyncio
async def test_recovery_strategies_and_failure_analyzer():
    """Verify self-healing recovery strategies and FailureAnalyzerAgent execution."""
    driver = MockDeviceDriver(device_id="mock-rec-1")

    # 1. Test dialog dismissal strategy
    dialog_ctx = {"category": "ui_obstruction", "error_message": "Unexpected permission popup"}
    dialog_res = await RecoveryStrategyRegistry.attempt_recovery(driver, dialog_ctx)
    assert dialog_res is not None
    assert dialog_res.success
    assert dialog_res.action_type.value == "dismiss_dialog"
    assert driver.action_history[-1].get("key") == "back"

    # 2. Test navigate back strategy
    nav_ctx = {"category": "wrong_screen"}
    nav_res = await RecoveryStrategyRegistry.attempt_recovery(driver, nav_ctx)
    assert nav_res is not None
    assert nav_res.success
    assert nav_res.action_type.value == "navigate_back"

    # 3. Test scroll and search strategy
    scroll_ctx = {"category": "element_not_found", "scroll_direction": "down"}
    scroll_res = await RecoveryStrategyRegistry.attempt_recovery(driver, scroll_ctx)
    assert scroll_res is not None
    assert scroll_res.success
    assert scroll_res.action_type.value == "scroll_and_retry"

    # 4. Test FailureAnalyzerAgent process
    class MockContext:
        pass

    ctx = MockContext()
    setattr(ctx, "_active_driver", driver)

    fa_agent = FailureAnalyzerAgent(ctx=ctx)
    fa_result = await fa_agent.process({"failure_context": {"category": "wrong_screen"}})
    assert fa_result["status"] == "recovered"
    assert fa_result["recovery_action"] == "navigate_back"
