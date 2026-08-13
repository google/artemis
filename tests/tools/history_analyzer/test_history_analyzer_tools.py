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

import json
from artemis.agents.history_analyzer.history_analyzer import HistoryAnalyzer
from artemis.config import parse_llm_config
import pytest


@pytest.mark.asyncio
async def test_exec_get_step_details(artemis_context):
    """Test the exec_get_step_details method with manually configured steps."""
    analyzer = HistoryAnalyzer(artemis_context)
    analyzer.history_steps = [
        {
            "step_number": 0,
            "relative_time": 0.0,
            "summary": "Initial step",
            "action_taken": "none",
            "operator_raw_thinking": "Starting up",
            "last_execution_result": "Success",
        },
        {
            "step_number": 1,
            "relative_time": 5.0,
            "summary": "Second step",
            "action_taken": "click",
            "operator_raw_thinking": "Clicking button",
            "last_execution_result": "Success",
        },
    ]

    first_step_num = 0
    result = analyzer.exec_get_step_details(first_step_num, first_step_num)

    assert "Error:" not in result
    assert "No steps found" not in result

    parsed = json.loads(result)
    assert isinstance(parsed, list)
    assert len(parsed) == 1
    assert parsed[0]["step_number"] == first_step_num
    assert "summary" in parsed[0]


@pytest.mark.asyncio
async def test_get_step_details_tool_with_fixture(artemis_context):
    """Test _get_step_details_tool against real steps loaded from the inputs/data_engine.db fixture."""
    if not artemis_context.data_engine:
        pytest.skip("DataEngine fixture not initialized")

    artemis_context.data_engine.current_session_id = "1a7fc344-ea69-4b1a-b066-d8a667502b8c"
    steps = artemis_context.data_engine.get_agent_friendly_steps()
    assert len(steps) > 0

    analyzer = HistoryAnalyzer(artemis_context)
    tool = analyzer._get_step_details_tool(steps)

    result = tool.invoke({"start_step": 1, "end_step": 3})
    assert isinstance(result, str)
    assert "Error:" not in result
    assert "No steps found" not in result

    parsed = json.loads(result)
    assert isinstance(parsed, list)
    assert len(parsed) == 3
    assert parsed[0]["step_number"] == 1
    assert "YouTube" in parsed[0]["summary"]


@pytest.mark.asyncio
async def test_history_analyzer_blackbox_run(artemis_context):
    """End-to-end blackbox test of HistoryAnalyzer.run(query) using real steps from inputs/data_engine.db and real LLM calls."""
    if not artemis_context.data_engine:
        pytest.skip("DataEngine fixture not initialized")

    artemis_context.llm_config = parse_llm_config()
    artemis_context.data_engine.current_session_id = "1a7fc344-ea69-4b1a-b066-d8a667502b8c"

    analyzer = HistoryAnalyzer(artemis_context)
    query = (
        "What application did the operator open and what query did they search"
        " for? Briefly summarize the session steps."
    )
    result = await analyzer.run(query)

    assert isinstance(result, str)
    assert len(result) > 0
    assert "Error:" not in result
    assert "No history recorded" not in result
