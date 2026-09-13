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

"""The structured verdict request must never end on a model turn (Gemini
rejects such requests with 400 "Requests ending with a model turn are not
supported"), which is the natural state after the loop's tool-free reply."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from artemis.agents.checker.checker import CheckReport, _structured_report


def _llm(report: CheckReport):
    structured = MagicMock()
    structured.ainvoke = AsyncMock(return_value=report)
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    return llm, structured


@pytest.mark.asyncio
async def test_structured_report_appends_user_turn_after_model_reply():
    llm, structured = _llm(CheckReport(verdicts=[]))
    messages = [SystemMessage(content="s"), HumanMessage(content="h"), AIMessage(content="done")]

    await _structured_report(llm, messages)

    sent = structured.ainvoke.call_args.args[0]
    assert isinstance(sent[-1], HumanMessage)
    assert sent[-2] is messages[-1]
    assert len(messages) == 3  # caller's list untouched


@pytest.mark.asyncio
async def test_structured_report_keeps_user_ending_conversation_as_is():
    llm, structured = _llm(CheckReport(verdicts=[]))
    messages = [SystemMessage(content="s"), HumanMessage(content="final iteration")]

    await _structured_report(llm, messages)

    assert structured.ainvoke.call_args.args[0] is messages
