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

"""LogAnalyzerNode's ReAct loop: a helper tool's failure is structural
(``ToolFailure``), never sniffed from the words of its text."""

from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
import pytest

from artemis.agents.log_analyzer.log_analyzer import LogAnalyzerNode
from artemis.context import ArtemisContext
from artemis.core.tool_failure import ToolFailure

_STATUS_CASES = [
    pytest.param(ToolFailure("Error searching logs: device gone"), "error", id="tool_failure"),
    pytest.param(
        "Error: 12 matching log lines\nE/Foo: Error opening file", "success", id="plain_error_text"
    ),
]


def _text_tool(name: str, result):
    async def _run(keyword: str) -> str:
        return result

    return StructuredTool.from_function(coroutine=_run, name=name, description=name)


@pytest.mark.asyncio
@pytest.mark.parametrize("result, expected_status", _STATUS_CASES)
async def test_agent_loop_tool_message_status_is_structural(result, expected_status):
    ctx = MagicMock(spec=ArtemisContext)
    ctx.data_engine = None
    node = LogAnalyzerNode(ctx=ctx)
    tool = _text_tool("search_logs", result)

    base_llm = MagicMock()
    base_llm.bind_tools.return_value = base_llm
    tool_turn = AIMessage(
        content="", tool_calls=[{"name": "search_logs", "args": {"keyword": "Error"}, "id": "c1"}]
    )
    final_turn = AIMessage(content="Summary.", tool_calls=[])
    messages = [SystemMessage(content="s"), HumanMessage(content="h")]

    with patch(
        "artemis.agents.log_analyzer.log_analyzer.acomplete",
        new=AsyncMock(side_effect=[tool_turn, final_turn]),
    ):
        outcome = await node._run_agent_loop(
            base_llm=base_llm,
            tools_for_binding=[tool],
            tools=[tool],
            current_messages=messages,
            state=MagicMock(),
            max_iterations=5,
            agent_name="Log Analyzer",
        )

    assert outcome == "Summary."
    tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_call_id == "c1"
    assert tool_msgs[0].status == expected_status
    assert tool_msgs[0].content == str(result)
