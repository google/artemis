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

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from artemis.agents.operator.operator import OperatorNode
from artemis.context import ArtemisContext
from artemis.graph.graph import execution_check_node
from artemis.graph.state import State
import pytest


@pytest.mark.asyncio
async def test_snapshot_rollback_flow(tmp_path):
    # Setup dummy directories
    base_dir = tmp_path / "trace"
    notes_dir = base_dir / "notes"
    notes_dir.mkdir(parents=True)

    task_plan_path = notes_dir / "task_plan.md"
    task_plan_path.write_text("Initial Plan", encoding="utf-8")

    # Mock Context
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = MagicMock()
    mock_ctx.data_engine.base_dir = base_dir
    mock_ctx.execution_setup = None

    # Mock State
    mock_state = MagicMock(spec=State)
    mock_state.subagent_calls = []
    mock_state.asanitize_update = AsyncMock(return_value={"status": "success"})
    mock_state.initial_goal = "Test goal"
    mock_state.short_term_memory = ""
    mock_state.operator_raw_data = {
        "screenshot_b64": "YmFzZTY0",
        "xml_hierarchy": "<hierarchy></hierarchy>",
        "ocr_results": [],
    }
    mock_state.current_step_id = "test-step-id"

    # Mock Controller and LLM to let Operator run minimally
    mock_controller = MagicMock()
    mock_controller.take_screenshot = AsyncMock(return_value="YmFzZTY0")
    mock_device_data = MagicMock()
    mock_device_data.elements = []
    mock_controller.get_screen_data = AsyncMock(return_value=mock_device_data)

    mock_llm = MagicMock()
    mock_response = MagicMock()
    mock_response.tool_calls = []

    async def mock_astream(*args, **kwargs):
        yield mock_response

    mock_llm.astream.side_effect = mock_astream
    mock_llm.bind_tools.return_value = mock_llm

    # 1. Run Operator to trigger snapshot
    with patch("artemis.agents.operator.operator.get_llm", return_value=mock_llm):
        node = OperatorNode(mock_ctx)
        await node(mock_state)

    # Verify snapshot was created
    snapshot_dir = notes_dir.with_name("notes_snapshot")
    assert snapshot_dir.exists()
    assert (snapshot_dir / "task_plan.md").read_text(encoding="utf-8") == "Initial Plan"
    assert mock_ctx.task_plan_snapshot == snapshot_dir

    # 2. Simulate Operator optimistic modification
    task_plan_path.write_text("Modified Plan", encoding="utf-8")

    # 3. Run execution_check_node with failure to trigger rollback
    future = asyncio.Future()
    future.set_result({"status": "failed"})
    mock_ctx.checker_task = future

    await execution_check_node(mock_state, mock_ctx)

    # Verify rollback restored the file
    assert task_plan_path.read_text(encoding="utf-8") == "Initial Plan"

    # 4. Run Operator again to create new snapshot (simulate next attempt)
    # First modify it again to simulate operator working on restored plan
    task_plan_path.write_text("Modified Plan 2", encoding="utf-8")

    with patch("artemis.agents.operator.operator.get_llm", return_value=mock_llm):
        await node(mock_state)

    assert (snapshot_dir / "task_plan.md").read_text(encoding="utf-8") == "Modified Plan 2"

    # 5. Run execution_check_node with success to trigger commit (cleanup)
    future_success = asyncio.Future()
    future_success.set_result({"status": "success"})
    mock_ctx.checker_task = future_success

    await execution_check_node(mock_state, mock_ctx)

    # Verify snapshot was deleted
    assert not snapshot_dir.exists()
