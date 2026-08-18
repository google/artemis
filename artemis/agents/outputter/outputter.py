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

from jinja2 import Template
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, ToolMessage
from artemis.agents.outputter.tools import (
    get_search_history_tool,
    get_step_details_tool,
    get_step_screenshot_tool,
)
from artemis.config import OutputConfig
from artemis.context import ArtemisContext
from artemis.data_engine.trace import trace
from artemis.graph.state import State
from artemis.services.llm import get_llm, invoke_llm_with_timeout_message, with_fallback
from artemis.tools.scratchpad import (
    get_append_note_tool_pure,
    get_list_notes_tool_pure,
    get_read_note_tool_pure,
    get_save_note_tool_pure,
    get_update_note_tool_pure,
)
from artemis.tools.video_tool import get_video_analyzer_tool_pure
from artemis.tools.tool_wrapper import invoke_tool_with_injection
from artemis.utils.logger import get_logger
from artemis.utils.task_tree import build_plan_and_history, get_active_subgoal_hashes
from pydantic import BaseModel

logger = get_logger(__name__)


@trace(type="agent", name="outputter")
async def outputter(
    ctx: ArtemisContext,
    output_config: OutputConfig,
    graph_output: State,
    plan_and_history: str | None = None,
) -> dict:
    logger.info("Starting Outputter Agent")

    # Fetch plan and history if not provided
    if plan_and_history is None and ctx.data_engine:
        try:
            history = ctx.data_engine.get_agent_friendly_steps()

            # Read current plan if it exists and base_dir is valid
            current_plan = ""
            base_dir = getattr(ctx.data_engine, "base_dir", None)
            if base_dir and isinstance(base_dir, (str, Path)):
                notes_dir = Path(base_dir) / "notes"
                current_path = notes_dir / "task_plan.md"
                if current_path.exists():
                    try:
                        current_plan = current_path.read_text(encoding="utf-8")
                    except Exception as e:
                        logger.error(f"Failed to read current plan for outputter: {e}")

            if history:
                active_subgoal_hash = "default"
                if current_plan:
                    try:
                        active_subgoal_hash, _ = get_active_subgoal_hashes(current_plan)
                    except Exception as e:
                        logger.error(f"Failed to parse active subgoal in outputter: {e}")

                plan_and_history = build_plan_and_history(
                    current_plan,
                    history,
                    active_subgoal_hash,
                    last_n_detailed=0,
                    min_summaries=len(history),
                    strict_milestone_pruning=False,
                )
        except Exception as e:
            logger.error(f"Failed to resolve plan and history in outputter: {e}")

    system_message = (
        "You are the Output Synthesis Agent for an Android UI automation"
        " system. Your sole objective is to verify whether the user's initial"
        " goal was achieved and synthesize the final report.\n\n## Core"
        " Principles\n1. STRICTLY EVIDENCE-BASED & NO HALLUCINATION: Rely ONLY"
        " on the actual observed execution history and successful tool outputs."
        " Do not assume, reconstruct, or hallucinate any actions, elements, or"
        " steps that did not actually occur during this specific run. If a step"
        " was skipped or not observed (e.g., because the device was already in"
        " the target state), you must explicitly state that it was 'Not"
        " Observed / Already in State' rather than fabricating the interaction."
        " If a tool call (like `video_analyzer`) fails or returns an error, you"
        " must report the failure and the resulting lack of evidence, rather"
        " than assuming success or guessing the details. If the user requests a"
        " step-by-step guide, you must only include steps that were actually"
        " executed and observed; do not invent 'standard' steps to fill in"
        " gaps.\n2. NO JARGON IN FINAL ANSWER: The 'Final Answer' must be"
        " written in clear, user-friendly language, completely free of"
        " agent-specific jargon (e.g., do not mention 'nodes', 'XPath',"
        " 'selectors', 'ReAct', or 'tools').\n\n## Tool Usage Guidelines\n-"
        " LAZY VERIFICATION: If the execution history and the final screenshot"
        " already in your context provide undeniable proof of the outcome, do"
        " not call tools. Directly output your conclusion.\n- ACTIVE RETRIEVAL:"
        " You should only call tools (e.g., `get_step_details`,"
        " `get_step_screenshot`, `search_history_for_text`) if you need to"
        " extract specific hidden data (like verification codes, tracking"
        " numbers) or if the final state is ambiguous.\n- VIDEO ANALYSIS COST:"
        " The `video_analyzer` tool is expensive. Use it only when necessary"
        " (e.g., to verify video playback or motion).\n- PERSISTENT WRITE: If"
        " you extract important information requested by the user (such as a"
        " verification code, tracking number, or a text summary), you should"
        " use `save_note` or `update_note` to write it directly into persistent"
        " notes so it can be retrieved by other agents or the system."
    )

    render_kwargs = {
        "initial_goal": graph_output.initial_goal,
        "structured_output": output_config.structured_output,
        "output_description": output_config.output_description,
        "plan_and_history": plan_and_history,
    }

    human_message = Template(
        Path(__file__).parent.joinpath("outputter.md").read_text(encoding="utf-8")
    ).render(**render_kwargs)

    screenshot_b64 = (
        graph_output.operator_raw_data.get("screenshot_b64")
        if graph_output.operator_raw_data
        else None
    )

    human_message_content = [{"type": "text", "text": human_message}]
    if screenshot_b64:
        human_message_content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}"},
            }
        )

    messages: list[BaseMessage] = [
        SystemMessage(content=system_message),
        HumanMessage(content=human_message_content),
    ]

    # Setup tools
    history_steps = []
    if ctx.data_engine:
        history_steps = ctx.data_engine.get_agent_friendly_steps()

    details_tool = get_step_details_tool(history_steps)
    screenshot_tool = get_step_screenshot_tool(ctx, history_steps)
    search_tool = get_search_history_tool(ctx)
    list_notes_tool = get_list_notes_tool_pure(ctx)
    read_note_tool = get_read_note_tool_pure(ctx)
    save_note_tool = get_save_note_tool_pure(ctx)
    update_note_tool = get_update_note_tool_pure(ctx)
    append_note_tool = get_append_note_tool_pure(ctx)
    video_analyzer_tool = get_video_analyzer_tool_pure(ctx)

    tools = [
        details_tool,
        screenshot_tool,
        search_tool,
        list_notes_tool,
        read_note_tool,
        save_note_tool,
        update_note_tool,
        append_note_tool,
        video_analyzer_tool,
    ]

    llm = get_llm(ctx=ctx, name="outputter", is_utils=True)
    llm_fallback = get_llm(ctx=ctx, name="outputter", is_utils=True, use_fallback=True)

    llm_with_tools = llm.bind_tools(tools=tools)
    llm_fallback_with_tools = llm_fallback.bind_tools(tools=tools)

    # ReAct Loop
    max_turns = 20
    raw_answer = None
    for turn in range(max_turns):
        logger.info(f"Outputter ReAct turn {turn + 1}")

        response = await with_fallback(
            main_call=lambda: invoke_llm_with_timeout_message(llm_with_tools.ainvoke(messages)),
            fallback_call=lambda: invoke_llm_with_timeout_message(
                llm_fallback_with_tools.ainvoke(messages)
            ),
        )

        messages.append(response)

        if not response.tool_calls:
            raw_answer = response.content
            break

        for tc in response.tool_calls:
            tool_name = tc["name"].split(":")[-1] if ":" in tc["name"] else tc["name"]
            args = tc["args"]

            logger.info(f"Outputter executing tool {tool_name} with args: {args}")
            tool_map = {
                "get_step_details": details_tool,
                "get_step_screenshot": screenshot_tool,
                "search_history_for_text": search_tool,
                "list_notes": list_notes_tool,
                "read_note": read_note_tool,
                "save_note": save_note_tool,
                "update_note": update_note_tool,
                "append_note": append_note_tool,
                "video_analyzer": video_analyzer_tool,
            }

            try:
                selected_tool = tool_map.get(tool_name)
                if selected_tool:
                    result = await invoke_tool_with_injection(
                        tool=selected_tool,
                        args=args,
                        tool_call_id=tc["id"],
                        state=graph_output,
                        record_trace=True,
                    )
                else:
                    result = f"Error: Tool {tool_name} is not supported."
                status = (
                    "success"
                    if not (isinstance(result, str) and result.startswith("Error"))
                    else "error"
                )
            except Exception as e:
                logger.error(f"Error running tool {tool_name}: {e}")
                result = f"Error running tool {tool_name}: {e}"
                status = "error"

            messages.append(
                ToolMessage(
                    tool_call_id=tc["id"],
                    content=result if isinstance(result, (str, list)) else str(result),
                    status=status,
                )
            )

    if raw_answer is None:
        raw_answer = "Error: Outputter failed to resolve the query within maximum turns."
        logger.error(raw_answer)

    # Final Formatting if structured output is requested
    if output_config.structured_output:
        logger.info("Formatting raw output to structured format...")
        schema = None
        so = output_config.structured_output
        if isinstance(so, dict):
            schema = so
        elif isinstance(so, BaseModel):
            schema = type(so)
        elif isinstance(so, type) and issubclass(so, BaseModel):
            schema = so

        if schema is not None:
            structured_llm = llm.with_structured_output(schema)
            structured_llm_fallback = llm_fallback.with_structured_output(schema)

            format_messages = [
                SystemMessage(
                    content=(
                        "You are a helpful assistant. Your task is to take the"
                        " raw execution summary and format it into the"
                        " requested structured output schema. Do not invent any"
                        " information. If the raw summary does not contain the"
                        " required info, leave those fields empty or null."
                    )
                ),
                HumanMessage(
                    content=(
                        f"Initial Goal: {graph_output.initial_goal}\nRaw Summary:\n{raw_answer}"
                    )
                ),
            ]

            response = await with_fallback(
                main_call=lambda: invoke_llm_with_timeout_message(
                    structured_llm.ainvoke(format_messages)
                ),
                fallback_call=lambda: invoke_llm_with_timeout_message(
                    structured_llm_fallback.ainvoke(format_messages)
                ),
            )

            if isinstance(response, BaseModel):
                return response.model_dump()
            return response

    # Fallback to old behavior if it didn't match the template
    if isinstance(raw_answer, str):
        raw_answer_stripped = raw_answer.strip()
        if raw_answer_stripped.startswith("{") and raw_answer_stripped.endswith("}"):
            try:
                return json.loads(raw_answer_stripped)
            except Exception:
                pass
        elif raw_answer_stripped.startswith("```json") and raw_answer_stripped.endswith("```"):
            try:
                content = raw_answer_stripped[7:-3].strip()
                return json.loads(content)
            except Exception:
                pass

    return raw_answer
