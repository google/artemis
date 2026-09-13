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

"""Fake chat model for infrastructure testing (ARTEMIS_FAKE_LLM=1).

Completes any task on the first LLM invocation so that queueing, entry
points, device locking, and status propagation can be exercised without
real model calls:

- Flash (report_task_status bound): immediately reports status=completed.
- Pro planner (save_note bound): saves a task plan whose only subgoal is
  already checked, so convergence_gate ends the graph after planning.
- Anything else: returns a plain "FAKE_LLM_OK" text message.

ARTEMIS_FAKE_LLM_DELAY_S adds an artificial per-call delay so tasks stay
alive long enough to observe queue/concurrency behavior.
"""

import asyncio
import json
import time
from typing import Any, Iterator, AsyncIterator
import uuid

from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

FAKE_DONE_TEXT = "FAKE_LLM: task auto-completed for infrastructure testing."


def _tool_name(tool: Any) -> str | None:
    name = getattr(tool, "name", None)
    if isinstance(name, str):
        return name
    if isinstance(tool, dict):
        if isinstance(tool.get("name"), str):
            return tool["name"]
        fn = tool.get("function")
        if isinstance(fn, dict) and isinstance(fn.get("name"), str):
            return fn["name"]
    return None


class FakeChatModel(BaseChatModel):
    """Deterministic fake model that finishes every task on the first call."""

    bound_tool_names: list[str] = []
    delay_s: float = 0.0

    @property
    def _llm_type(self) -> str:
        return "artemis-fake"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "FakeChatModel":
        names = [n for n in (_tool_name(t) for t in tools) if n]
        return FakeChatModel(bound_tool_names=names, delay_s=self.delay_s)

    def with_structured_output(self, schema: Any, **kwargs: Any) -> "FakeChatModel":
        return self

    def _decide(self, messages: list[BaseMessage]) -> AIMessage:
        call_id = f"fake-{uuid.uuid4().hex[:12]}"
        usage = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}

        if "report_task_status" in self.bound_tool_names:
            return AIMessage(
                content="Task verified complete on first observation.",
                tool_calls=[
                    {
                        "name": "report_task_status",
                        "args": {"status": "completed", "explanation": FAKE_DONE_TEXT},
                        "id": call_id,
                    }
                ],
                usage_metadata=usage,
            )

        if "save_note" in self.bound_tool_names:
            already_planned = any(
                tc.get("name") == "save_note"
                for m in messages
                for tc in (getattr(m, "tool_calls", None) or [])
            )
            if already_planned:
                return AIMessage(content="Plan saved. " + FAKE_DONE_TEXT, usage_metadata=usage)
            return AIMessage(
                content="Saving completed plan.",
                tool_calls=[
                    {
                        "name": "save_note",
                        "args": {
                            "key": "task_plan",
                            "content": "- [x] " + FAKE_DONE_TEXT,
                        },
                        "id": call_id,
                    }
                ],
                usage_metadata=usage,
            )

        return AIMessage(content=FAKE_DONE_TEXT, usage_metadata=usage)

    def _to_chunk(self, msg: AIMessage) -> ChatGenerationChunk:
        tool_call_chunks = [
            {
                "name": tc["name"],
                "args": json.dumps(tc["args"]),
                "id": tc["id"],
                "index": i,
                "type": "tool_call_chunk",
            }
            for i, tc in enumerate(msg.tool_calls or [])
        ]
        chunk = AIMessageChunk(
            content=msg.content,
            tool_call_chunks=tool_call_chunks,
            usage_metadata=msg.usage_metadata,
        )
        return ChatGenerationChunk(message=chunk)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if self.delay_s > 0:
            time.sleep(self.delay_s)
        return ChatResult(generations=[ChatGeneration(message=self._decide(messages))])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if self.delay_s > 0:
            await asyncio.sleep(self.delay_s)
        return ChatResult(generations=[ChatGeneration(message=self._decide(messages))])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        if self.delay_s > 0:
            time.sleep(self.delay_s)
        yield self._to_chunk(self._decide(messages))

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        if self.delay_s > 0:
            await asyncio.sleep(self.delay_s)
        yield self._to_chunk(self._decide(messages))
