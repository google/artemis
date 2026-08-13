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
from unittest.mock import MagicMock

from artemis.context import ArtemisContext
from artemis.data_engine.trace import (
    TraceSpan,
    smart_serialize,
)
import pytest


def test_smart_serialize():
    # Test dict
    d = {"a": 1, "b": "string"}
    assert smart_serialize(d) == json.dumps(d, ensure_ascii=False)

    # Test list
    items_list = [1, 2, "three"]
    assert smart_serialize(items_list) == json.dumps(items_list, ensure_ascii=False)

    # Test bytes
    b = b"hello world"
    import hashlib

    expected_sha = hashlib.sha256(b).hexdigest()[:8]
    assert smart_serialize(b) == f"<Bytes length={len(b)} sha256={expected_sha}>"

    # Test other types
    assert smart_serialize(123) == "123"
    assert smart_serialize("just a string") == "just a string"


@pytest.mark.asyncio
async def test_trace_span():
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = MagicMock()

    with TraceSpan(name="test_span", ctx=mock_ctx) as span:
        span.result = "span_result"
        # Simulate work

    # TraceSpan writes twice: start (running) and end (success)
    assert mock_ctx.data_engine.record_trace.call_count == 2

    # Verify the final success call (which is the last call)
    args, kwargs = mock_ctx.data_engine.record_trace.call_args
    assert kwargs["type"] == "span"
    assert kwargs["name"] == "test_span"
    assert kwargs["status"] == "success"
    assert kwargs["payload"]["result"] == "span_result"


@pytest.mark.asyncio
async def test_trace_span_failure():
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = MagicMock()

    try:
        with TraceSpan(name="failed_span", ctx=mock_ctx):
            raise ValueError("Span error")
    except ValueError:
        pass

    # TraceSpan writes twice: start (running) and end (failed)
    assert mock_ctx.data_engine.record_trace.call_count == 2

    # Verify the final failed call (which is the last call)
    args, kwargs = mock_ctx.data_engine.record_trace.call_args
    assert kwargs["type"] == "span"
    assert kwargs["name"] == "failed_span"
    assert kwargs["status"] == "failed"
    assert "error" in kwargs["payload"]
    assert "Span error" in kwargs["payload"]["error"]


def test_data_engine_callback_handler():
    from uuid import uuid4
    from langchain_core.messages import HumanMessage, AIMessage
    from artemis.data_engine.trace import DataEngineCallbackHandler

    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = MagicMock()

    mock_step_id = uuid4()
    mock_ctx.data_engine.current_step_id = mock_step_id

    handler = DataEngineCallbackHandler(mock_ctx)
    run_id = uuid4()

    # 1. Simulate on_chat_model_start
    serialized = {"name": "test_model"}
    messages = [[HumanMessage(content="Hello LLM")]]
    handler.on_chat_model_start(serialized, messages, run_id=run_id)

    assert mock_ctx.data_engine.record_trace.call_count == 1
    call1_args, call1_kwargs = mock_ctx.data_engine.record_trace.call_args_list[0]
    assert call1_kwargs["type"] == "llm_call"
    assert call1_kwargs["name"] == "test_model"
    assert call1_kwargs["status"] == "running"
    assert call1_kwargs["step_id"] == mock_step_id
    assert call1_kwargs["payload"]["messages"][0]["content"] == "Hello LLM"

    # 2. Simulate on_llm_end (successful complete response)
    class MockGeneration:
        def __init__(self, message):
            self.message = message

    class MockLLMResult:
        def __init__(self, generations):
            self.generations = generations

    response_message = AIMessage(content="Hello User")
    mock_response = MockLLMResult(generations=[[MockGeneration(response_message)]])

    handler.on_llm_end(mock_response, run_id=run_id)

    assert mock_ctx.data_engine.record_trace.call_count == 3
    call2_args, call2_kwargs = mock_ctx.data_engine.record_trace.call_args_list[1]
    assert call2_kwargs["type"] == "llm_call"
    assert call2_kwargs["status"] == "success"
    assert call2_kwargs["step_id"] == mock_step_id
    assert call2_kwargs["payload"]["messages"][0]["content"] == "Hello LLM"
    assert call2_kwargs["payload"]["response"][0]["content"] == "Hello User"
    assert isinstance(call2_kwargs.get("duration"), float)
    assert call2_kwargs["duration"] >= 0.0

    call3_args, call3_kwargs = mock_ctx.data_engine.record_trace.call_args_list[2]
    assert call3_kwargs["type"] == "raw_thinking"
    assert call3_kwargs["parent_trace_id"] == run_id
    assert call3_kwargs["payload"]["thought"] == "Hello User"


def test_callback_handler_chain_parenting():
    from uuid import uuid4
    from langchain_core.messages import HumanMessage
    from artemis.data_engine.trace import DataEngineCallbackHandler, CURRENT_TRACE_ID

    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = MagicMock()

    handler = DataEngineCallbackHandler(mock_ctx)

    agent_run_id = uuid4()
    chain1_run_id = uuid4()
    chain2_run_id = uuid4()
    llm_run_id = uuid4()

    token = CURRENT_TRACE_ID.set(agent_run_id)
    try:
        # Simulate chain start (top level, parent is agent_run_id)
        handler.on_chain_start({}, {}, run_id=chain1_run_id, parent_run_id=None)
        # Simulate nested chain start (parent is chain1_run_id)
        handler.on_chain_start({}, {}, run_id=chain2_run_id, parent_run_id=chain1_run_id)

        # Simulate LLM start inside nested chain (parent is chain2_run_id)
        messages = [[HumanMessage(content="Hello Nested LLM")]]
        handler.on_chat_model_start(
            {"name": "test_nested_model"},
            messages,
            run_id=llm_run_id,
            parent_run_id=chain2_run_id,
        )

        # Verify parent trace ID resolves to agent_run_id (since chains are not recorded)
        assert mock_ctx.data_engine.record_trace.call_count == 1
        args, kwargs = mock_ctx.data_engine.record_trace.call_args
        assert kwargs["parent_trace_id"] == agent_run_id

        # Cleanup
        handler.on_chain_end({}, run_id=chain2_run_id)
        handler.on_chain_end({}, run_id=chain1_run_id)

        assert len(handler.chain_parents) == 0
    finally:
        CURRENT_TRACE_ID.reset(token)
