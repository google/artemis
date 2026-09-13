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

"""A detached lens call (segment capsule, visual summary) must never be
recorded as the current step's reasoning: the callback handler attaches
``thinking``/``raw_thinking`` traces by the DataEngine's global
``current_step_id``, which is independent of the trace context, so the
handler itself has to recognise the detached lens context."""

import uuid
from unittest.mock import Mock

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from artemis.data_engine.context_vars import CURRENT_NODE_NAME, CURRENT_TRACE_ID
from artemis.data_engine.trace import DataEngineCallbackHandler, detached_trace

CAPSULE = '{"doing": "scrolling", "did": "swiped", "entry_state": "home"}'


def _handler():
    ctx = Mock()
    ctx.data_engine = Mock()
    ctx.data_engine.current_step_id = uuid.uuid4()
    ctx.data_engine.record_trace = Mock()
    return DataEngineCallbackHandler(ctx), ctx


def _result(text: str) -> LLMResult:
    return LLMResult(generations=[[ChatGeneration(message=AIMessage(content=text))]])


def _recorded(ctx) -> dict[str, dict]:
    return {c.kwargs["type"]: c.kwargs for c in ctx.data_engine.record_trace.call_args_list}


def test_agent_call_reply_is_recorded_as_raw_thinking_under_the_step():
    handler, ctx = _handler()
    span = uuid.uuid4()
    token = CURRENT_TRACE_ID.set(span)
    token_name = CURRENT_NODE_NAME.set("operator")
    try:
        handler.on_llm_end(_result("I see the settings list."), run_id=uuid.uuid4())
    finally:
        CURRENT_TRACE_ID.reset(token)
        CURRENT_NODE_NAME.reset(token_name)
    recorded = _recorded(ctx)
    assert recorded["llm_call"]["step_id"] == ctx.data_engine.current_step_id
    assert recorded["raw_thinking"]["step_id"] == ctx.data_engine.current_step_id
    assert recorded["raw_thinking"]["payload"]["thought"] == "I see the settings list."


def test_detached_lens_reply_is_neither_thinking_nor_attached_to_the_step():
    handler, ctx = _handler()
    span = uuid.uuid4()
    token = CURRENT_TRACE_ID.set(span)
    token_name = CURRENT_NODE_NAME.set("operator")
    try:
        with detached_trace("lens:step_capsule"):
            handler.on_llm_end(_result(CAPSULE), run_id=uuid.uuid4())
    finally:
        CURRENT_TRACE_ID.reset(token)
        CURRENT_NODE_NAME.reset(token_name)
    recorded = _recorded(ctx)
    assert "raw_thinking" not in recorded
    assert "thinking" not in recorded
    assert recorded["llm_call"]["step_id"] is None
