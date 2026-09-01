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

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field


class _CyFunctionDetectorMeta(type):
    def __instancecheck__(self, instance):
        name = type(instance).__name__
        return (
            name
            in (
                "cyfunction",
                "cython_function_or_method",
                "builtin_function_or_method",
            )
            or "cyfunction" in name.lower()
        )


class CyFunctionDetector(metaclass=_CyFunctionDetectorMeta):
    pass


class SpawnLogReaderArgs(BaseModel):
    model_config = {"ignored_types": (CyFunctionDetector,)}
    specific_query: str = Field(
        ...,
        description=(
            "Specific instructions for the sub-agent, including what to look for and analyze."
        ),
    )


from langgraph.prebuilt import InjectedState

from artemis.context import ArtemisContext
from artemis.data_engine.trace import (
    CURRENT_TRACE_ID,
    TraceSpan,
    trace,
)
from artemis.graph.state import State
from artemis.services.llm import acomplete, get_llm, invoke_llm_with_timeout_message
from artemis.tools.index import get_tool_by_name
from artemis.tools.mobile.log_utils import fetch_and_filter_logs
from artemis.tools.mobile.read_logs import get_read_logs_tool
from artemis.tools.mobile.search_logs import (
    get_search_logs_tool,
    search_and_merge_logs,
)
from artemis.tools.tool_wrapper import (
    get_tool_result_content,
    invoke_tool_with_injection,
)
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


class LogAnalyzerNode:
    def __init__(self, ctx: ArtemisContext):
        self.ctx = ctx
        self._log_cache = {}
        self._raw_log_cache = {}

    def _wrap_with_caching(self, tool: BaseTool) -> BaseTool:
        """Wraps a tool with caching logic based on tool name and arguments."""
        original_coroutine = tool.coroutine

        async def cached_coroutine(**kwargs):
            if tool.name == "read_logs":
                # Intercept read_logs to cache raw logs
                lines = kwargs.get("lines", 200)
                # Since we might have since_time/until_time, let's keep it simple or key by all.
                # The instruction says: "cache the returned string based on the lines parameter."
                # We can also include since_time and until_time in the cache key to be safe, but let's follow the lines-based requirement.
                cache_key = (
                    lines,
                    kwargs.get("since_time"),
                    kwargs.get("until_time"),
                )
                if cache_key in self._raw_log_cache:
                    logger.info(f"Raw log cache hit for read_logs with key {cache_key}")
                    return self._raw_log_cache[cache_key]

                result = await original_coroutine(**kwargs)
                if isinstance(result, str) and not result.startswith("Failed to read logs"):
                    self._raw_log_cache[cache_key] = result
                return result

            elif tool.name == "search_logs":
                lines = kwargs.get("lines", 10000)
                since_time = kwargs.get("since_time")
                until_time = kwargs.get("until_time")
                raw_cache_key = (lines, since_time, until_time)

                # Check if we have the raw logs cached
                if raw_cache_key in self._raw_log_cache:
                    logger.info(
                        "Raw log cache hit for search_logs with key"
                        f" {raw_cache_key}. Running local search."
                    )
                    raw_logs = self._raw_log_cache[raw_cache_key]
                    # Run search_and_merge_logs locally

                    try:
                        return search_and_merge_logs(
                            logs=raw_logs,
                            keyword=kwargs.get("keyword"),
                            context_lines=kwargs.get("context_lines", 0),
                            is_regex=kwargs.get("is_regex", False),
                        )
                    except Exception as e:
                        logger.error(f"Failed to search logs locally: {e}")
                        return f"Error searching logs: {e}"

                # If not cached, fetch them once via read_logs (or the original tool, which will fetch them)
                # Note: search_logs itself fetches and filters. We can let the original_coroutine run,
                # but to cache the raw logs, we need the raw logs.
                # Wait, search_logs returns the SEARCHED results, not the raw logs.
                # So if we just run original_coroutine, we don't get the raw logs to cache them!
                # To cache them, we should fetch them using get_read_logs_tool or fetch_and_filter_logs,
                # cache them, and then run search_and_merge_logs.
                logger.info(
                    "Raw log cache miss for search_logs with key"
                    f" {raw_cache_key}. Fetching raw logs first."
                )

                try:
                    raw_logs = fetch_and_filter_logs(
                        ctx=self.ctx,
                        lines=lines,
                        since_time=since_time,
                        until_time=until_time,
                    )
                    self._raw_log_cache[raw_cache_key] = raw_logs

                    return search_and_merge_logs(
                        logs=raw_logs,
                        keyword=kwargs.get("keyword"),
                        context_lines=kwargs.get("context_lines", 0),
                        is_regex=kwargs.get("is_regex", False),
                    )
                except Exception as e:
                    logger.error(f"Failed to fetch and search logs: {e}")
                    return f"Error searching logs: {e}"

            # Fallback to standard tool caching for other tools
            serialized_args = json.dumps(kwargs, sort_keys=True)
            cache_key = f"{tool.name}:{serialized_args}"
            if cache_key in self._log_cache:
                logger.info(f"Cache hit for tool {tool.name} with args {kwargs}")
                return self._log_cache[cache_key]

            logger.info(
                f"Cache miss for tool {tool.name} with args {kwargs}. Invoking original tool."
            )
            result = await original_coroutine(**kwargs)
            self._log_cache[cache_key] = result
            return result

        return StructuredTool.from_function(
            coroutine=cached_coroutine,
            name=tool.name,
            description=tool.description,
            args_schema=tool.args_schema,
        )

    async def _run_agent_loop(
        self,
        base_llm: Any,
        tools_for_binding: list[Any],
        tools: list[BaseTool],
        current_messages: list[Any],
        state: State,
        max_iterations: int = 10,
        agent_name: str = "Agent",
    ) -> str:
        outcome = "No result"

        for i in range(max_iterations):
            if i == max_iterations - 1:
                current_messages.append(
                    HumanMessage(
                        content=(
                            "[WARNING] This is your final iteration; all tools"
                            " are stripped, and you must provide your final"
                            " answer/summary directly."
                        )
                    )
                )
                llm = base_llm.bind_tools(tools=[])
            else:
                if i > 0:
                    current_messages.append(
                        HumanMessage(
                            content=(
                                "[WARNING] You have not completed the analysis"
                                f" yet (iteration {i + 1} of {max_iterations})."
                            )
                        )
                    )
                llm = base_llm.bind_tools(tools=tools_for_binding)

            response = await invoke_llm_with_timeout_message(acomplete(llm, current_messages))

            if response.content:
                outcome = response.content

            if not response.tool_calls:
                break

            if i == max_iterations - 1:
                tool_names = ", ".join(tc["name"] for tc in response.tool_calls)
                warning = (
                    f"\n\n[Warning: {agent_name} reached the maximum iteration"
                    f" limit of {max_iterations} and could not execute tool(s):"
                    f" {tool_names}]"
                )
                if outcome == "No result":
                    outcome = (
                        f"{agent_name} reached the maximum iteration limit of"
                        f" {max_iterations} while attempting to call tools:"
                        f" {tool_names}"
                    )
                else:
                    outcome += warning
                break

            current_messages.append(response)

            # Handle tool calls in parallel
            async def run_tool(tc) -> ToolMessage:
                tool_name = tc["name"].split(":")[-1] if ":" in tc["name"] else tc["name"]
                logger.info(f"{agent_name} requested tool: {tool_name}")

                result = ""
                status = "success"
                try:
                    if ":" in tool_name:
                        tool_to_run = get_tool_by_name(tool_name, tools)
                    else:
                        tool_to_run = next((t for t in tools if t.name == tool_name), None)
                    if tool_to_run:
                        args = dict(tc["args"])
                        with TraceSpan(name=tool_name, ctx=self.ctx) as span:
                            span.payload = {"args": args}
                            try:
                                result_obj = await invoke_tool_with_injection(
                                    tool=tool_to_run,
                                    args=args,
                                    tool_call_id=tc["id"],
                                    state=state,
                                )
                                result = get_tool_result_content(result_obj)
                                if isinstance(result, list):
                                    result = "\n".join(map(str, result))
                                elif not isinstance(result, str):
                                    result = str(result)
                                span.result = result
                                if result.startswith("Error"):
                                    status = "error"
                                    span.status = "failed"
                                    span.error = result
                            except Exception as e:
                                status = "error"
                                span.status = "failed"
                                span.error = str(e)
                                raise e
                    else:
                        result = f"Error: Tool {tool_name} not supported"
                        status = "error"
                except Exception as e:
                    logger.error(f"Error running tool {tool_name}: {e}")
                    result = f"Error running tool {tool_name}: {e}"
                    status = "error"

                return ToolMessage(
                    tool_call_id=tc["id"],
                    content=result,
                    status=status,
                )

            active_tool_calls = [
                tc
                for tc in response.tool_calls
                if (tc["name"].split(":")[-1] if ":" in tc["name"] else tc["name"])
                != "google_search"
            ]
            tool_outputs = await asyncio.gather(*(run_tool(tc) for tc in active_tool_calls))

            for tm in tool_outputs:
                current_messages.append(tm)

        return outcome

    @trace(type="agent", name="log_analyzer")
    async def run(self, prompt: str, state: State) -> str:
        """Runs the main Log Analyzer and returns its summary."""
        prompt_path = Path(__file__).parent.joinpath("log_analyzer.md")
        if not prompt_path.exists():
            raise FileNotFoundError(f"System prompt file not found at {prompt_path}")

        system_prompt = prompt_path.read_text(encoding="utf-8")

        llm = get_llm(ctx=self.ctx, name="log_analyzer")

        all_tools = [
            self._wrap_with_caching(get_read_logs_tool(self.ctx)),
            self._wrap_with_caching(get_search_logs_tool(self.ctx)),
            self._get_spawn_log_reader_tool(),
        ]

        current_messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=prompt),
        ]

        return await self._run_agent_loop(
            base_llm=llm,
            tools_for_binding=all_tools,
            tools=all_tools,
            current_messages=current_messages,
            state=state,
            max_iterations=10,
            agent_name="Log Analyzer",
        )

    @trace(type="agent", name="log_reader_sub_agent")
    async def _run_log_reader(self, specific_query: str, state: State) -> str:
        """Runs the Log Reader Sub-Agent and returns its summary."""
        sub_prompt_path = Path(__file__).parent.joinpath("log_reader_sub_agent.md")
        if not sub_prompt_path.exists():
            sub_prompt_path.write_text(
                "# Log Reader Sub-Agent\nYou are a specialized agent for"
                " reading and analyzing Android logs.\nYour job is to fulfill"
                " the specific prompt given by the Main Agent.\nYou have access"
                " to tools: `read_logs`.\nAnalyze the logs carefully and"
                " provide a clear, structured summary of your findings.\n",
                encoding="utf-8",
            )

        sub_system_prompt = sub_prompt_path.read_text(encoding="utf-8")

        llm = get_llm(ctx=self.ctx, name="log_reader_sub_agent")

        sub_tools = [self._wrap_with_caching(get_read_logs_tool(self.ctx))]

        # Extract the list of keys (metadata) currently stored in _log_cache.
        cache_keys = list(self._log_cache.keys())

        current_messages = [
            SystemMessage(content=sub_system_prompt),
        ]

        # Append a system message listing the available cache keys so the sub-agent knows what data can be retrieved instantly.
        if cache_keys:
            current_messages.append(
                SystemMessage(
                    content=(
                        "The following log reads are already cached and can be"
                        " retrieved instantly without cost:\n"
                    )
                    + "\n".join(f"- {k}" for k in cache_keys)
                )
            )

        current_messages.append(HumanMessage(content=specific_query))

        return await self._run_agent_loop(
            base_llm=llm,
            tools_for_binding=sub_tools,
            tools=sub_tools,
            current_messages=current_messages,
            state=state,
            max_iterations=10,
            agent_name="Log Reader Sub-Agent",
        )

    def _get_spawn_log_reader_tool(self) -> BaseTool:

        async def spawn_log_reader(
            specific_query: str,
            state: Annotated[State, InjectedState] = None,
        ) -> str:
            """[SPAWN SUB-AGENT] Delegates complex log analysis to a sub-agent.

            - Use when: Task requires deep analysis, cross-referencing, or
            reading huge log volumes. - Note: The sub-agent has no search tools
            and can only read raw logs. - Input: specific_query (str):
            Instructions for the sub-agent. - Output: A structured summary
            report from the sub-agent.
            """
            logger.info(f"Spawning Log Reader Sub-Agent with query: '{specific_query}'")
            return await self._run_log_reader(specific_query, state)

        return StructuredTool.from_function(
            coroutine=spawn_log_reader,
            name="spawn_log_reader",
            args_schema=SpawnLogReaderArgs,
        )
