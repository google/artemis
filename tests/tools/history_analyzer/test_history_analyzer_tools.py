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

from artemis.agents.history_analyzer.history_analyzer import HistoryAnalyzer
from artemis.config import parse_llm_config
import pytest


@pytest.mark.asyncio
async def test_replay_steps_tool_with_fixture(artemis_context):
    """The analyzer's replay_steps tool against real steps loaded from the
    inputs/data_engine.db fixture."""
    if not artemis_context.data_engine:
        pytest.skip("DataEngine fixture not initialized")

    artemis_context.data_engine.current_session_id = "1a7fc344-ea69-4b1a-b066-d8a667502b8c"
    steps = artemis_context.data_engine.get_agent_friendly_steps()
    assert len(steps) > 0

    analyzer = HistoryAnalyzer(artemis_context)
    tools = {t.name: t for t in analyzer._build_tools()}
    assert {
        "search_history",
        "replay_steps",
        "get_step_screenshot",
        "list_notes",
        "read_note",
    } <= set(tools)

    result = tools["replay_steps"].invoke({"start_step": 1, "end_step": 3})
    assert isinstance(result, str)
    assert not result.startswith("Error")
    assert "- **Step 1 (" in result
    assert "- **Step 3 (" in result
    assert "YouTube" in result


@pytest.mark.asyncio
@pytest.mark.integration
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
