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

from pathlib import Path
from typing import Annotated
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from artemis.context import ArtemisContext
from artemis.services.llm import get_llm, invoke_llm_with_timeout_message
from artemis.tools.mobile.search_logs import search_and_merge_logs
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


class TaskOutputAnalyzerNode:
    """Agent that analyzes large command/task execution outputs using LLM and tools."""

    def __init__(self, ctx: ArtemisContext):
        self.ctx = ctx

    async def run(
        self,
        command: str,
        output_text: str,
        query: str,
    ) -> str:
        prompt_path = Path(__file__).parent.joinpath("output_analyzer.md")
        if not prompt_path.exists():
            raise FileNotFoundError(f"System prompt file not found at {prompt_path}")

        system_prompt = prompt_path.read_text(encoding="utf-8")

        # Get the LLM for output_analyzer (falls back to log_analyzer)
        llm = get_llm(ctx=self.ctx, name="output_analyzer")

        # Local tools created with closure on output_text
        @tool
        def read_task_output(
            start_line: Annotated[int, "The 1-indexed line number to start reading from."],
            end_line: Annotated[int, "The 1-indexed line number to stop reading at (inclusive)."],
        ) -> str:
            """Reads a specific range of lines from the task output.

            Use this tool when you know the line number range to read a specific
            portion of the output.
            """
            lines = output_text.splitlines()
            if not lines:
                return "Task output is empty."
            start = max(1, start_line)
            end = min(len(lines), end_line)
            if start > end:
                return (
                    f"Error: start_line {start_line} is greater than end_line"
                    f" {end_line} or task output length {len(lines)}."
                )
            segment = lines[start - 1 : end]
            return "\n".join(segment)

        @tool
        def search_task_output(
            keyword: Annotated[
                str,
                "The keyword or regular expression to search for in the output.",
            ],
            is_regex: Annotated[
                bool,
                "Whether to treat the keyword as a regular expression. Default is False.",
            ] = False,
            context_lines: Annotated[
                int,
                "Number of context lines to include before and after the"
                " matched line. Default is 5.",
            ] = 5,
        ) -> str:
            """Searches the task output for a keyword or regular expression, returning matching lines and context.

            Use this tool to quickly locate error messages, warnings, or
            specific key-value pairs in large outputs.
            """
            return search_and_merge_logs(
                logs=output_text,
                keyword=keyword,
                context_lines=context_lines,
                is_regex=is_regex,
            )

        tools = [read_task_output, search_task_output]
        llm = llm.bind_tools(tools=tools)

        # Optimize prompt context with preview if output is large
        lines = output_text.splitlines()
        if len(lines) <= 400:
            log_preview = f"Full Output:\n{output_text}"
        else:
            first_part = "\n".join(lines[:200])
            last_part = "\n".join(lines[-200:])
            log_preview = (
                f"Note: Output is very large ({len(lines)} lines). Here is a"
                " preview of the start and end of the output:\n\n--- START OF"
                f" OUTPUT ---\n{first_part}\n--- TRUNCATED {len(lines) - 400}"
                f" LINES ---\n--- END OF OUTPUT ---\n{last_part}\n\nYou can use"
                " `read_task_output` or `search_task_output` to fetch any"
                " lines in the middle if needed."
            )

        user_content = f"Command Executed: {command}\n\n{log_preview}\n\nQuestion: {query}"

        current_messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_content),
        ]

        max_iterations = 5
        outcome = "Failed to analyze output."

        for _ in range(max_iterations):
            response = await invoke_llm_with_timeout_message(llm.ainvoke(current_messages))

            if response.content:
                outcome = response.content

            if not response.tool_calls:
                break

            current_messages.append(response)

            for tc in response.tool_calls:
                tool_name = tc["name"]
                args = tc["args"]
                logger.info(f"Output Analyzer requested tool: {tool_name}")

                tool_to_run = next((t for t in tools if t.name == tool_name), None)
                if tool_to_run:
                    try:
                        result = tool_to_run.invoke(args)
                    except Exception as e:
                        result = f"Failed to run tool {tool_name}: {e}"
                else:
                    result = f"Error: Tool {tool_name} not found."

                current_messages.append(
                    ToolMessage(
                        tool_call_id=tc["id"],
                        content=str(result),
                        status="success" if not str(result).startswith("Error") else "error",
                    )
                )

        return outcome
