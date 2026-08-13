# pylint: disable=too-many-statements
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

"""Committee tool for multi-agent deliberation and failure recovery."""

import asyncio
import base64
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from artemis.agents.history_analyzer.history_analyzer import HistoryAnalyzer
from artemis.context import ArtemisContext
from artemis.data_engine.trace import (
    CURRENT_TRACE_ID,
    TraceSpan,
    trace_langchain_tool,
)
from artemis.drivers.base import BaseDeviceDriver
from artemis.graph.state import State
from artemis.services.llm import get_llm, invoke_llm_with_timeout_message
from artemis.tools.base import ArtemisTool, ToolCategory, ToolRegistry
from artemis.tools.log_tool import get_analyze_logs_tool
from artemis.tools.scratchpad import get_list_notes_tool, get_read_note_tool
from artemis.tools.tool_wrapper import (
    ToolWrapper,
    get_tool_result_content,
    invoke_tool_with_injection,
)
from artemis.tools.types import CyFunctionDetector
from artemis.tools.video_tool import get_video_analyzer_tool
from artemis.utils.logger import get_logger
from artemis.utils.notes import get_note_file_path, get_notes_dir
from artemis.utils.task_tree import (
    build_plan_and_history,
    get_active_subgoal_hashes,
    get_recent_subgoal_hashes,
)

logger = get_logger(__name__)


class AskCommitteeArgs(BaseModel):
    """Arguments schema for invoking the committee tool."""

    model_config = {"ignored_types": (CyFunctionDetector,)}
    avatar_directive: str = Field(
        ...,
        description="Directive to frame and steer the debate for your avatar.",
    )


ASK_COMMITTEE_DOCSTRING = (
    "[COMMITTEE] Call this tool to summon a committee of sub-agents to debate"
    " and resolve complex failures.\n\n"
    "Your avatar will host the meeting and drive it based on your directive.\n"
    "The result will be a synthesized recommendation."
)


class AskCommitteeTool(ArtemisTool):
    """Universal tool for summoning a committee of sub-agents to debate and resolve failures."""

    def __init__(self, category: ToolCategory = "custom"):
        super().__init__(
            name="ask_committee",
            description=ASK_COMMITTEE_DOCSTRING,
            args_schema=AskCommitteeArgs,
            category=category,
        )

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    async def execute(
        self,
        driver: BaseDeviceDriver | None = None,  # pylint: disable=unused-argument
        ctx: ArtemisContext | None = None,
        avatar_directive: str = "",
        tool_call_id: str | None = None,  # pylint: disable=unused-argument
        state: State | None = None,
        **kwargs: Any,
    ) -> str:
        directive = avatar_directive or kwargs.get("AvatarDirective") or ""
        return await _run_committee_logic(
            ctx=ctx,
            state=state,
            avatar_directive=directive,
        )


# Universal tool instance & aliases
ask_committee = AskCommitteeTool()
AskCommittee = AskCommitteeTool
AskCommitteeToolAlias = AskCommitteeTool
ToolRegistry.register(ask_committee)


def get_ask_committee_tool(ctx: ArtemisContext) -> BaseTool:
    """Exports ask_committee as a LangChain BaseTool."""
    return trace_langchain_tool(ask_committee.to_langchain_tool(ctx), ctx)


# pylint: disable=too-many-branches,too-many-locals,too-many-statements
async def _execute_committee(
    ctx: ArtemisContext,
    state: State | None,
    avatar_directive: str,
) -> str:
    """Executes committee debate loop and returns the final conclusion."""
    with TraceSpan(name="committee", trace_type="agent", ctx=ctx):
        logger.info(f"Committee invoked with directive: {avatar_directive}")

        # 1. Access or create blackboard file
        if ctx.data_engine:
            notes_dir = get_notes_dir(ctx.data_engine.base_dir)
        else:
            notes_dir = Path(".") / "notes"
        notes_dir.mkdir(parents=True, exist_ok=True)

        # Load latest screenshot as base64 to provide multimodal vision to the committee
        screenshot_b64 = None
        screenshot_path = getattr(state, "latest_screenshot", None) if state is not None else None
        if screenshot_path and Path(screenshot_path).exists():
            try:
                with open(screenshot_path, "rb") as f:
                    screenshot_b64 = base64.b64encode(f.read()).decode("utf-8")
                logger.info(f"Committee successfully loaded screenshot from: {screenshot_path}")
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.error(f"Failed to read screenshot in committee: {e}")

        trace_id = CURRENT_TRACE_ID.get() or str(uuid4())
        blackboard_path = notes_dir / f"blackboard_{trace_id}.md"

        # Initialize blackboard with goal and directive
        initial_goal = (
            getattr(state, "initial_goal", "Not specified.")
            if state is not None
            else "Not specified."
        )
        blackboard_content = (
            "# Committee Discussion Blackboard\n\n"
            f"## User's Initial Goal\n{initial_goal}\n\n"
            f"## Master's Directive\n{avatar_directive}\n\n"
        )
        blackboard_path.write_text(blackboard_content, encoding="utf-8")

        # 2. Extract history
        history_summary = "## Previous Context\n"
        if ctx.data_engine:
            steps = ctx.data_engine.get_agent_friendly_steps()
            if steps:
                active_subgoal_hash = "default"
                if ctx.data_engine:
                    task_plan_path = get_note_file_path(ctx.data_engine.base_dir, "task_plan")
                else:
                    task_plan_path = notes_dir / "task_plan.md"
                task_plan = "No task plan yet."
                keep_hashes = None
                if task_plan_path.exists():
                    try:
                        task_plan = task_plan_path.read_text(encoding="utf-8")

                        active_subgoal_hash, _ = get_active_subgoal_hashes(task_plan)
                        keep_hashes = get_recent_subgoal_hashes(
                            steps,
                            active_subgoal_hash,
                            ctx.data_engine.base_dir,
                        )
                    except Exception as e:  # pylint: disable=broad-exception-caught
                        logger.error(f"Failed to parse active subgoal in committee: {e}")

                try:
                    plan_and_history = build_plan_and_history(
                        task_plan,
                        steps,
                        active_subgoal_hash,
                        keep_subgoal_hashes=keep_hashes,
                        last_n_detailed=3,
                    )
                    history_summary += plan_and_history
                except Exception as e:  # pylint: disable=broad-exception-caught
                    logger.error(f"Failed to build plan and history in committee: {e}")
                    history_summary += f"Error building plan and history: {e}"

                # Append immediate failure reason if present in state
                replan_reason = (
                    getattr(state, "operator_replan_reason", None) if state is not None else None
                )
                if replan_reason:
                    history_summary += f"\n\n### Current Failure Reason\n{replan_reason}\n"

                # Read failed plans history to ensure the committee avoids
                # repeating failed strategies
                failed_plans_history = ""
                failed_history_path = notes_dir / "failed_plans_history.md"
                if failed_history_path.exists():
                    try:
                        failed_plans_history = failed_history_path.read_text(encoding="utf-8")
                    except Exception as e:  # pylint: disable=broad-exception-caught
                        logger.error(f"Failed to read failed plans history in committee: {e}")
                if failed_plans_history:
                    history_summary += (
                        f"\n\n### Previous Failed Plan Attempts\n{failed_plans_history}\n"
                    )

        with open(blackboard_path, "a", encoding="utf-8") as f:
            f.write(history_summary + "\n\n")

        # 3. Load prompts from JSON
        prompts = {}
        prompts_path = Path(__file__).parent / "committee_prompts.json"
        if prompts_path.exists():
            try:
                prompts = json.loads(prompts_path.read_text(encoding="utf-8"))
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.error(f"Failed to load committee prompts: {e}")

        planner_prompt = prompts.get("planner_avatar", "You are the Planner Avatar.")
        history_analyzer_prompt = prompts.get(
            "history_analyzer_expert", "You are the History Analyzer."
        )
        diagnoser_prompt = prompts.get("diagnoser_expert", "You are the Diagnoser Expert.")

        # 4. Prepare Specialized Tools for Members

        # History Analyzer Tools
        analyzer_inst = HistoryAnalyzer(ctx)
        history_steps = ctx.data_engine.get_agent_friendly_steps() if ctx.data_engine else []
        # pylint: disable=protected-access
        details_tool = analyzer_inst._get_step_details_tool(history_steps)
        read_note_tool_hist = get_read_note_tool(ctx)
        list_notes_tool_hist = get_list_notes_tool(ctx)

        history_analyzer_tools = [
            trace_langchain_tool(details_tool, ctx),
            trace_langchain_tool(read_note_tool_hist, ctx),
            trace_langchain_tool(list_notes_tool_hist, ctx),
        ]

        # Diagnoser Tools
        video_tool = get_video_analyzer_tool(ctx, role="diagnoser")
        log_tool = get_analyze_logs_tool(ctx)
        read_note_tool = get_read_note_tool(ctx)
        list_notes_tool = get_list_notes_tool(ctx)

        diagnoser_tools = [
            trace_langchain_tool(video_tool, ctx),
            trace_langchain_tool(log_tool, ctx),
            trace_langchain_tool(read_note_tool, ctx),
            trace_langchain_tool(list_notes_tool, ctx),
        ]

        # 5. Instantiate LLMs
        llm_pl = get_llm(ctx=ctx, name="planner_avatar")

        llm_hist = get_llm(ctx=ctx, name="history_analyzer_expert")
        llm_hist = llm_hist.bind_tools(tools=history_analyzer_tools)

        llm_diag = get_llm(ctx=ctx, name="diagnoser_expert")
        llm_diag = llm_diag.bind_tools(tools=diagnoser_tools)

        # 6. Debate Loop (configured rounds)
        exec_setup = getattr(ctx, "execution_setup", None)
        rounds = getattr(exec_setup, "committee_debate_rounds", 2) if exec_setup else 2

        async def run_agent_turn(llm, system_prompt, tools, agent_name):
            current_blackboard = blackboard_path.read_text(encoding="utf-8")

            human_content = [
                {
                    "type": "text",
                    "text": f"Current Blackboard:\n{current_blackboard}",
                }
            ]
            if screenshot_b64:
                human_content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}"},
                    }
                )

            messages = [
                SystemMessage(content=system_prompt),
                HumanMessage(content=human_content),
            ]

            max_iterations = 3
            for _ in range(max_iterations):
                response = await invoke_llm_with_timeout_message(llm.ainvoke(messages))

                if not response.tool_calls:
                    return response.content

                messages.append(response)

                async def run_tool(tc):
                    tool_name = tc["name"]
                    logger.info(f"{agent_name} requested tool: {tool_name}")
                    tool_to_run = next((t for t in tools if t.name == tool_name), None)
                    if tool_to_run:
                        try:
                            args = dict(tc["args"])
                            result = await invoke_tool_with_injection(
                                tool=tool_to_run,
                                args=args,
                                tool_call_id=tc["id"],
                                state=state,
                            )
                            return get_tool_result_content(result)
                        except Exception as e:  # pylint: disable=broad-exception-caught
                            return f"Error: {e}"
                    else:
                        return f"Error: Tool {tool_name} not found"

                active_tool_calls = [
                    tc for tc in response.tool_calls if tc["name"] != "google_search"
                ]
                tool_outputs = await asyncio.gather(*(run_tool(tc) for tc in active_tool_calls))

                for tc, result in zip(active_tool_calls, tool_outputs):
                    messages.append(
                        ToolMessage(
                            tool_call_id=tc["id"],
                            content=result,
                            status="success" if not result.startswith("Error") else "error",
                        )
                    )
            return "Error: Reached max iterations in turn."

        for r in range(1, rounds + 1):
            logger.info(f"Committee Round {r}")

            # 1. Planner Avatar speaks first (Host)
            pl_response = await run_agent_turn(llm_pl, planner_prompt, [], "Planner Avatar")
            with open(blackboard_path, "a", encoding="utf-8") as f:
                f.write(f"### Planner Avatar (Round {r})\n{pl_response}\n\n")

            # 2. Diagnoser speaks (Can use tools)
            diag_response = await run_agent_turn(
                llm_diag, diagnoser_prompt, diagnoser_tools, "Diagnoser"
            )
            with open(blackboard_path, "a", encoding="utf-8") as f:
                f.write(f"### Diagnoser (Round {r})\n{diag_response}\n\n")

            # 3. History Analyzer speaks (Can use tools)
            hist_response = await run_agent_turn(
                llm_hist,
                history_analyzer_prompt,
                history_analyzer_tools,
                "History Analyzer",
            )
            with open(blackboard_path, "a", encoding="utf-8") as f:
                f.write(f"### History Analyzer (Round {r})\n{hist_response}\n\n")

        # Final Synthesis by Planner Avatar
        current_blackboard = blackboard_path.read_text(encoding="utf-8")

        final_human_content = [
            {
                "type": "text",
                "text": f"Current Blackboard:\n{current_blackboard}",
            }
        ]
        if screenshot_b64:
            final_human_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}"},
                }
            )

        msg_final = [
            SystemMessage(
                content=(
                    "You are the Planner Avatar. Summarize the discussion"
                    " and output the final recommended plan revisions and"
                    " action plan based on your master's directive."
                )
            ),
            HumanMessage(content=final_human_content),
        ]
        resp_final = await invoke_llm_with_timeout_message(llm_pl.ainvoke(msg_final))

        with open(blackboard_path, "a", encoding="utf-8") as f:
            f.write(f"## Final Conclusion\n{resp_final.content}\n")

        return resp_final.content


async def _run_committee_logic(
    ctx: ArtemisContext | None,
    state: State | None,
    avatar_directive: str,
) -> str:
    """Executes multi-agent committee debate logic."""
    if ctx is None:
        return "Error: ArtemisContext is required for ask_committee."

    try:
        return await asyncio.wait_for(_execute_committee(ctx, state, avatar_directive), timeout=300)
    except TimeoutError:
        logger.error("Committee timed out after 300 seconds.")
        return "Error: Committee timed out after 300 seconds."


ask_committee_wrapper = ToolWrapper(
    tool_fn_getter=get_ask_committee_tool,
    on_success_fn=lambda output: f"Committee concluded:\n{output}",
    on_failure_fn=lambda error: f"Committee failed: {error}",
)
