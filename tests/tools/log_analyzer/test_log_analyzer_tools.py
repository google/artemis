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

from artemis.agents.log_analyzer.log_analyzer import LogAnalyzerNode
import pytest


@pytest.mark.asyncio
@pytest.mark.integration
async def test_spawn_log_reader(artemis_context, mock_state):
    """Test the spawn_log_reader tool exposed by LogAnalyzerNode."""
    from artemis.sdk.utils import load_llm_config_override
    from pathlib import Path

    node = LogAnalyzerNode(ctx=artemis_context)
    artemis_context.llm_config = load_llm_config_override(Path("llm-config.json"))
    if artemis_context.data_engine:
        artemis_context.data_engine.start_session("test_session")
    tool = node._get_spawn_log_reader_tool()

    # Let the tool run its lifecycle. It will use the real LLM APIs.
    # Note: State injection is normally handled by LangGraph/tool invocation,
    # but since we are testing the coroutine directly, we can pass it manually.
    result = await tool.coroutine(
        specific_query=("Find any errors related to network timeout in the logs."),
        state=mock_state,
    )

    assert result is not None
    assert isinstance(result, (str, list))
    assert len(result) > 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_log_analyzer_run(artemis_context, mock_state):
    """Test LogAnalyzerNode.run end-to-end execution with grounding configuration."""
    from artemis.config.llm import parse_llm_config

    node = LogAnalyzerNode(ctx=artemis_context)
    artemis_context.llm_config = parse_llm_config()
    if artemis_context.data_engine:
        artemis_context.data_engine.start_session("test_log_analyzer_session")

    result = await node.run(
        prompt="Analyze recent error logs for potential crash or exception tags.",
        state=mock_state,
    )

    assert result is not None
    assert isinstance(result, (str, list))
    assert len(result) > 0
