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

from pathlib import Path
import shutil
import tempfile
from unittest.mock import MagicMock
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage
from artemis.context import ArtemisContext
from artemis.data_engine.engine import DataEngine
from artemis.data_engine.trace import CURRENT_TRACE_ID, DataEngineCallbackHandler
from artemis.utils.task_tree import build_plan_and_history
import pytest


@pytest.fixture
def temp_workspace():
    tmpdir = tempfile.mkdtemp()
    yield Path(tmpdir)
    shutil.rmtree(tmpdir)


@pytest.mark.asyncio
async def test_end_to_end_thoughts_non_duplication(temp_workspace):
    # 1. Setup mock context and DataEngine
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_execution_setup = MagicMock()
    mock_execution_setup.traces_path = str(temp_workspace / "traces")
    mock_ctx.execution_setup = mock_execution_setup
    mock_ctx.device = None
    mock_ctx.llm_config = MagicMock()

    engine = DataEngine(mock_ctx)
    mock_ctx.data_engine = engine

    # Start session
    engine.start_session("Test initial goal")

    # 2. Allocate step ID
    step_id = engine.allocate_step_id()

    # 3. Simulate Operator Agent run starts
    operator_trace_id = uuid4()
    engine.record_trace(
        type="agent",
        name="operator",
        payload={"args": {}},
        trace_id=operator_trace_id,
        step_id=step_id,
        status="running",
    )

    # Set context variables for parenting
    token = CURRENT_TRACE_ID.set(operator_trace_id)
    try:
        # 4. Simulate LLM Call callback sequence
        handler = DataEngineCallbackHandler(mock_ctx)
        llm_run_id = uuid4()

        # Start model
        handler.on_chat_model_start(
            serialized={"name": "ChatGoogleGenerativeAI"},
            messages=[[HumanMessage(content="What should I do?")]],
            run_id=llm_run_id,
            parent_run_id=None,
        )

        # End model (generating thought + action)
        class MockGeneration:
            def __init__(self, message):
                self.message = message

        class MockLLMResult:
            def __init__(self, generations):
                self.generations = generations

        response_message = AIMessage(
            content=[
                {
                    "type": "thinking",
                    "thinking": "I need to tap the search bar to find books.",
                },
                {"type": "text", "text": "Tapping search bar."},
            ]
        )
        mock_response = MockLLMResult(generations=[[MockGeneration(response_message)]])

        handler.on_llm_end(mock_response, run_id=llm_run_id)
    finally:
        CURRENT_TRACE_ID.reset(token)

    # 5. Finish Operator Agent trace
    engine.record_trace(
        type="agent",
        name="operator",
        payload={"result": "Success"},
        trace_id=operator_trace_id,
        step_id=step_id,
        status="success",
    )

    # 6. Record step
    engine.record_step(
        pre_screenshot_bytes=b"pre_screenshot",
        ui_tree=[],
        ocr_result=[],
        action_taken=[{"action": "tap", "coordinates": [100, 200]}],
        operator_raw_thinking="I need to tap the search bar to find books.",
        operator_native_thinking="I need to tap the search bar to find books.",
        last_execution_result={"status": "success"},
        summary="Tapped search bar",
    )

    # Flush any pending tasks
    await engine.shutdown()

    # 7. Retrieve agent-friendly steps
    steps = engine.get_agent_friendly_steps()

    # Assert step was retrieved
    assert len(steps) == 1
    step_data = steps[0]

    # Assert thoughts are present in interleaved events (since we associated step_id now!)
    events = step_data.get("interleaved_events", [])
    thought_events = [e for e in events if e.get("type") in ("thought", "native_thought")]
    assert len(thought_events) > 0
    assert any(
        e["content"] == "I need to tap the search bar to find books." for e in thought_events
    )

    # 8. Build history and check for duplications
    plan = "- [ ] Search for books"
    history = build_plan_and_history(plan, steps, "default", last_n_detailed=1)

    print("\n--- Generated History Output ---")
    print(history)
    print("--------------------------------")

    # Assert thought is printed
    assert "I need to tap the search bar to find books." in history

    # Assert thought is NOT printed multiple times
    # Count occurrences of the thought string
    count = history.count("I need to tap the search bar to find books.")
    # The thought might appear in native thinking block and raw thinking block if both were stored.
    # In our test we set BOTH operator_raw_thinking and operator_native_thinking to the same string,
    # AND the LLM trace generated both "thinking" block and "text" block.
    # Let's count how many times it was actually rendered in the Operator Decision Loop.
    # It should appear at most 2 times (once as Native Thought, once as Thought/Raw Thought).
    # If there was duplicate appending due to fallback failure, it would appear 4+ times.
    assert count <= 2
