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
from pathlib import Path
from typing import Any

from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool, tool
from artemis.context import ArtemisContext
from artemis.data_engine.trace import CURRENT_TRACE_ID, TraceSpan, trace
from artemis.services.llm import acomplete, get_llm, invoke_llm_with_timeout_message
from artemis.tools.scratchpad import (
    get_list_notes_tool_pure,
    get_read_note_tool_pure,
)
from artemis.utils.logger import get_logger
from artemis.utils.notes import (
    get_note_file_path,
    get_notes_dir,
)
from artemis.utils.task_tree import build_plan_and_history, get_active_subgoal_hashes

logger = get_logger(__name__)


class HistoryAnalyzer:
    def __init__(self, ctx: ArtemisContext):
        self.ctx = ctx

    def exec_get_step_details(self, start_step: int, end_step: int) -> str:
        """Retrieve the detailed, full-granularity information for a range of steps (inclusive).

        Use this when you need to inspect specific actions taken, operator
        thinking, or execution results.
        """
        try:
            s_step = int(start_step)
            e_step = int(end_step)
        except (ValueError, TypeError):
            return (
                f"Error: start_step and end_step must be integers, got {start_step} and {end_step}."
            )

        matched_steps = []
        for s in getattr(self, "history_steps", []):
            step_num = s.get("step_number")
            if step_num is not None and s_step <= step_num <= e_step:
                details = {
                    "step_number": s.get("step_number"),
                    "relative_time": s.get("relative_time"),
                    "summary": s.get("summary"),
                    "action_taken": s.get("action_taken"),
                    "operator_raw_thinking": s.get("operator_raw_thinking"),
                    "last_execution_result": s.get("last_execution_result"),
                }
                matched_steps.append(details)
        if not matched_steps:
            return f"No steps found in range [{s_step}, {e_step}]."
        return json.dumps(matched_steps, indent=2, ensure_ascii=False)

    def _get_step_details_tool(self, history_steps: list[dict[str, Any]]) -> BaseTool:
        self.history_steps = history_steps

        @tool
        def get_step_details(start_step: int, end_step: int) -> str:
            """Retrieve the detailed, full-granularity information for a range of steps (inclusive).

            Use this when you need to inspect specific actions taken, operator
            thinking, or execution results.
            """
            return self.exec_get_step_details(start_step, end_step)

        return get_step_details

    @trace(type="agent", name="history_analyzer")
    async def run(self, query: str) -> str:
        if not self.ctx.data_engine:
            return "Error: DataEngine is not available to retrieve history."

        # 1. Fetch all steps from Data Engine
        self.history_steps = self.ctx.data_engine.get_agent_friendly_steps()
        if not self.history_steps:
            return "No history recorded for this session yet."

        # 2. Build the plan and history or step list
        plan_and_history = ""
        try:
            notes_dir = get_notes_dir(self.ctx.data_engine.base_dir)
            current_plan = ""
            current_path = get_note_file_path(self.ctx.data_engine.base_dir, "task_plan")
            if current_path.exists():
                current_plan = current_path.read_text(encoding="utf-8")

            if current_plan:
                active_subgoal_hash, _ = get_active_subgoal_hashes(current_plan)
                plan_and_history = build_plan_and_history(
                    current_plan,
                    self.history_steps,
                    active_subgoal_hash,
                    last_n_detailed=1,
                    min_summaries=len(self.history_steps),
                )
        except Exception as e:
            logger.error(f"Failed to build plan and history in HistoryAnalyzer: {e}")

        if not plan_and_history:
            # Fallback to a simple list
            plan_and_history = "\n".join(
                [
                    f"- Step {s.get('step_number')} ({s.get('relative_time')}):"
                    f" {s.get('summary') or 'No summary'}"
                    for s in self.history_steps
                ]
            )

        # 3. Load system prompt
        prompt_path = Path(__file__).parent.joinpath("history_analyzer.md")
        if prompt_path.exists():
            system_prompt_template = prompt_path.read_text(encoding="utf-8")
        else:
            system_prompt_template = (
                "You are a History Analyzer. Your role is to analyze the"
                " execution history of a session and answer user queries in"
                " natural language.\nIf you need specific details for a range"
                " of steps, use the `get_step_details` tool."
            )

        system_message_content = (
            system_prompt_template + f"\n\n### Task Plan and Execution History:\n{plan_and_history}"
        )

        # 4. Prepare LLM and bind tools
        llm = get_llm(ctx=self.ctx, name="history_analyzer")
        get_step_details = self._get_step_details_tool(self.history_steps)

        list_notes_tool = get_list_notes_tool_pure(self.ctx)
        read_note_tool = get_read_note_tool_pure(self.ctx)
        llm = llm.bind_tools(tools=[get_step_details, list_notes_tool, read_note_tool])

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
                args = tc["args"]

                with TraceSpan(name=tool_name, ctx=self.ctx) as span:
                    span.payload = {"args": args}
                    if tool_name == "get_step_details":
                        logger.info(
                            f"HistoryAnalyzer executing tool get_step_details with args: {args}"
                        )
                        try:
                            start_step = int(args.get("start_step", 0))
                            end_step = int(args.get("end_step", 0))
                            result = get_step_details.invoke(
                                {"start_step": start_step, "end_step": end_step}
                            )
                            span.result = result
                            status = "success"
                        except Exception as e:
                            span.status = "failed"
                            span.error = str(e)
                            result = f"Error running get_step_details: {e}"
                            status = "error"
                    elif tool_name == "list_notes":
                        logger.info("HistoryAnalyzer executing tool list_notes")
                        try:
                            result = list_notes_tool.invoke({})
                            span.result = result
                            status = "success"
                        except Exception as e:
                            span.status = "failed"
                            span.error = str(e)
                            result = f"Error running list_notes: {e}"
                            status = "error"
                    elif tool_name == "read_note":
                        logger.info(f"HistoryAnalyzer executing tool read_note with args: {args}")
                        try:
                            key = args.get("key", "")
                            result = read_note_tool.invoke({"key": key})
                            span.result = result
                            status = "success"
                        except Exception as e:
                            span.status = "failed"
                            span.error = str(e)
                            result = f"Error running read_note: {e}"
                            status = "error"
                    else:
                        result = f"Error: Tool {tool_name} is not supported."
                        status = "error"
                        span.status = "failed"
                        span.error = result

                messages.append(
                    ToolMessage(
                        tool_call_id=tc["id"],
                        content=result,
                        status=status,
                    )
                )

        return "Error: HistoryAnalyzer failed to resolve the query within maximum turns."
