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

"""History Analyzer: answers natural-language questions about the session history.

Mounts the shared history tools (``search_history`` / ``replay_steps`` /
``get_step_screenshot``) plus the read-only note tools; tool results enter the
conversation through ``tool_result_messages`` so a fetched screenshot travels
in the carrier the model's provider accepts.
"""

from pathlib import Path

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool

from artemis.context import ArtemisContext
from artemis.data_engine.trace import TraceSpan, trace
from artemis.memory.context_policy import build_history_for
from artemis.services.llm import acomplete, get_llm, invoke_llm_with_timeout_message
from artemis.tools.history import get_history_tools
from artemis.tools.scratchpad import (
    get_list_notes_tool_pure,
    get_read_note_tool_pure,
)
from artemis.tools.tool_wrapper import (
    invoke_tool_with_injection,
    split_multimodal_result,
    tool_result_messages,
)
from artemis.utils.logger import get_logger
from artemis.utils.notes import get_note_file_path
from artemis.utils.task_tree import get_active_subgoal_hashes

logger = get_logger(__name__)

_FALLBACK_SYSTEM_PROMPT = (
    "You are a History Analyzer. Your role is to analyze the execution history"
    " of a session and answer user queries in natural language.\nIf you need"
    " the full record of specific steps, use the `replay_steps` tool; use"
    " `search_history` to locate steps by keyword and `get_step_screenshot` to"
    " look at a step's screen."
)


class HistoryAnalyzer:
    def __init__(self, ctx: ArtemisContext):
        self.ctx = ctx

    def _build_tools(self) -> list[BaseTool]:
        return [
            *get_history_tools(self.ctx),
            get_list_notes_tool_pure(self.ctx),
            get_read_note_tool_pure(self.ctx),
        ]

    @trace(type="agent", name="history_analyzer")
    async def run(self, query: str) -> str:
        if not self.ctx.data_engine:
            return "Error: DataEngine is not available to retrieve history."

        # 1. Fetch all steps from Data Engine
        history_steps = self.ctx.data_engine.get_agent_friendly_steps()
        if not history_steps:
            return "No history recorded for this session yet."

        # 2. Build the plan and history or step list
        plan_and_history = ""
        try:
            current_plan = ""
            current_path = get_note_file_path(self.ctx.data_engine.base_dir, "task_plan")
            if current_path.exists():
                current_plan = current_path.read_text(encoding="utf-8")

            if current_plan:
                active_subgoal_hash, _ = get_active_subgoal_hashes(current_plan)
                plan_and_history = build_history_for(
                    "history_analyzer",
                    current_plan,
                    history_steps,
                    active_subgoal_hash,
                    engine=self.ctx.data_engine,
                )
        except Exception as e:
            logger.error(f"Failed to build plan and history in HistoryAnalyzer: {e}")

        if not plan_and_history:
            # Fallback to a simple list
            plan_and_history = "\n".join(
                [
                    f"- Step {s.get('step_number')} ({s.get('relative_time')}):"
                    f" {s.get('summary') or 'No summary'}"
                    for s in history_steps
                ]
            )

        # 3. Load system prompt
        prompt_path = Path(__file__).parent.joinpath("history_analyzer.md")
        if prompt_path.exists():
            system_prompt_template = prompt_path.read_text(encoding="utf-8")
        else:
            system_prompt_template = _FALLBACK_SYSTEM_PROMPT

        system_message_content = (
            system_prompt_template + f"\n\n### Task Plan and Execution History:\n{plan_and_history}"
        )

        # 4. Prepare LLM and bind tools
        llm = get_llm(ctx=self.ctx, name="history_analyzer")
        tools = self._build_tools()
        llm = llm.bind_tools(tools=tools)

        # 5. ReAct Loop
        messages: list[BaseMessage] = [
            SystemMessage(content=system_message_content),
            HumanMessage(content=query),
        ]

        max_turns = 5
        for turn in range(max_turns):
            logger.info(f"HistoryAnalyzer ReAct turn {turn + 1}")

            response = await invoke_llm_with_timeout_message(acomplete(llm, messages))
            messages.append(response)

            if not response.tool_calls:
                return (
                    response.content.strip()
                    if isinstance(response.content, str)
                    else str(response.content)
                )

            # Process tool calls
            for tc in response.tool_calls:
                tool_name = tc["name"].split(":")[-1] if ":" in tc["name"] else tc["name"]
                args = dict(tc.get("args") or {})
                tool = next((t for t in tools if t.name == tool_name), None)

                with TraceSpan(name=tool_name, ctx=self.ctx) as span:
                    span.payload = {"args": args}
                    if tool is None:
                        result = f"Error: Tool {tool_name} is not supported."
                        status = "error"
                        span.status = "failed"
                        span.error = result
                    else:
                        logger.info(f"HistoryAnalyzer executing tool {tool_name} with args: {args}")
                        try:
                            result = await invoke_tool_with_injection(
                                tool=tool, args=args, tool_call_id=tc["id"]
                            )
                            text, _ = split_multimodal_result(result)
                            status = "error" if text.startswith("Error") else "success"
                            span.result = text
                            if status == "error":
                                span.status = "failed"
                                span.error = text
                        except Exception as e:
                            span.status = "failed"
                            span.error = str(e)
                            result = f"Error running {tool_name}: {e}"
                            status = "error"

                # A step screenshot travels in the carrier the model's provider
                # accepts; text results stay a single ToolMessage.
                messages.extend(
                    tool_result_messages(tc["id"], result, name=tool_name, status=status, llm=llm)
                )

        return "Error: HistoryAnalyzer failed to resolve the query within maximum turns."
