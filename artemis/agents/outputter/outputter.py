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
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Template
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from artemis.core.tool_failure import ToolFailure, is_tool_failure
from artemis.config import OutputConfig
from artemis.context import ArtemisContext
from artemis.data_engine.trace import trace
from artemis.graph.state import State
from artemis.llm.structured import ParseFailure, content_to_text, parse_structured
from artemis.services.llm import acomplete, get_llm, invoke_llm_with_timeout_message, with_fallback
from artemis.tools.scratchpad import (
    get_append_note_tool_pure,
    get_list_notes_tool_pure,
    get_read_note_tool_pure,
    get_save_note_tool_pure,
    get_update_note_tool_pure,
)
from artemis.tools.video_tool import get_video_analyzer_tool_pure
from artemis.tools.history import get_history_tools
from artemis.tools.tool_wrapper import (
    invoke_tool_with_injection,
    tool_result_messages,
)
from artemis.memory.context_policy import build_history_for
from artemis.utils.notes import read_note_content
from artemis.utils.task_tree import get_active_subgoal_hashes
from third_party.mobile_use.utils.logger import get_logger
from pydantic import BaseModel
from artemis.utils.image_codec import image_data_uri

logger = get_logger(__name__)

MAX_TURNS = 20

# Note key of the report the prompt asks for. The UI Task Report card and the
# MCP docs read it from notes/output.md.
OUTPUT_NOTE_KEY = "output"

# Note writes run one by one in the order issued; all other tools only read
# and can run in parallel.
_WRITE_TOOLS = frozenset({"save_note", "update_note", "append_note"})

_MAX_TURNS_ERROR = "Error: Outputter failed to resolve the query within maximum turns."

_SYSTEM_MESSAGE = (
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
    " You should only call tools (e.g., `search_history` to locate the"
    " step, `replay_steps` for its full record, `get_step_screenshot` for"
    " its image) if you need to extract specific hidden data (like"
    " verification codes, tracking numbers) or if the final state is"
    " ambiguous.\n- BATCH INDEPENDENT LOOKUPS: When you need several"
    " lookups that do not depend on each other, request them together in"
    " the same turn instead of one per turn.\n- VIDEO ANALYSIS COST:"
    " The `video_analyzer` tool is expensive. Use it only when necessary"
    " (e.g., to verify video playback or motion).\n- PERSISTENT WRITE: If"
    " you extract important information requested by the user (such as a"
    " verification code, tracking number, or a text summary), you should"
    " use `save_note` or `update_note` to write it directly into persistent"
    " notes so it can be retrieved by other agents or the system."
)

_FINAL_TURN_NOTICE = (
    "This is your final turn and tools are no longer available. Write your"
    " final answer to the user now, using only the evidence gathered so far."
    " State plainly anything you could not verify."
)

_EMPTY_REPLY_NUDGE = (
    "Your previous reply was empty. If you still need evidence, call the"
    " relevant tools; otherwise write your final answer to the user now."
)

_FORMAT_SYSTEM_MESSAGE = (
    "You are a helpful assistant. Your task is to take the"
    " raw execution summary and format it into the"
    " requested structured output schema. Do not invent any"
    " information. If the raw summary does not contain the"
    " required info, leave those fields empty or null."
)


@lru_cache(maxsize=1)
def _prompt_template() -> Template:
    return Template(Path(__file__).with_name("outputter.md").read_text(encoding="utf-8"))


def _notes_base_dir(ctx: ArtemisContext) -> str | Path | None:
    base_dir = getattr(ctx.data_engine, "base_dir", None) if ctx.data_engine else None
    return base_dir if isinstance(base_dir, (str, Path)) else None


def _resolve_plan_and_history(ctx: ArtemisContext) -> str | None:
    """Build the plan and history text passed to the outputter prompt."""
    try:
        history = ctx.data_engine.get_agent_friendly_steps()

        # Read current plan if it exists and base_dir is valid
        current_plan = ""
        base_dir = _notes_base_dir(ctx)
        if base_dir:
            current_path = Path(base_dir) / "notes" / "task_plan.md"
            if current_path.exists():
                try:
                    current_plan = current_path.read_text(encoding="utf-8")
                except Exception as e:
                    logger.error(f"Failed to read current plan for outputter: {e}")

        if not history:
            return None

        active_subgoal_hash = "default"
        if current_plan:
            try:
                active_subgoal_hash, _ = get_active_subgoal_hashes(current_plan)
            except Exception as e:
                logger.error(f"Failed to parse active subgoal in outputter: {e}")

        return build_history_for(
            "outputter",
            current_plan,
            history,
            active_subgoal_hash,
            engine=ctx.data_engine,
        )
    except Exception as e:
        logger.error(f"Failed to resolve plan and history in outputter: {e}")
        return None


def _verification_block(ctx: ArtemisContext, graph_output: State) -> str | None:
    """Format the verdict ledger and test summary, or None if the run had no check items."""
    try:
        from artemis.graph.checkpoints import read_ledger, read_run_outcome

        ledger_records = read_ledger(ctx.data_engine.base_dir)
        run_outcome = read_run_outcome(ctx.data_engine.base_dir) or getattr(
            graph_output, "run_outcome", None
        )
        if not (ledger_records or run_outcome):
            return None
        lines = ["--- Verification Verdict Ledger (append-only) ---"]
        for r in ledger_records:
            lines.append(
                f"- attempt {r.get('attempt_id')} [{r.get('kind')}]"
                f" '{r.get('item_text')}' -> {r.get('status')}:"
                f" {r.get('evidence', '')}"
            )
        if run_outcome:
            tests = run_outcome.get("tests") or {}
            lines.append(
                "--- Test Summary ---\n"
                f"task_status={run_outcome.get('task_status')},"
                f" passed={tests.get('passed', 0)},"
                f" failed={tests.get('failed', 0)},"
                f" inconclusive={tests.get('inconclusive', 0)},"
                f" unchecked={tests.get('unchecked', 0)}"
            )
        lines.append(
            "Report every declared check item with its final state"
            " (passed / failed / inconclusive / unchecked) and include"
            " the complete verdict sequence above. A failed assertion"
            " is a test result to report verbatim, not something to"
            " explain away. Items reported unchecked were never"
            " judged — never present them as passed."
        )
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Failed to attach verdict ledger to outputter: {e}")
        return None


def _environment_block(ctx: ArtemisContext) -> str | None:
    """Describe which UI hierarchy backend served the run.

    Mentions a mid-run switch too: falling back to UIAutomator2 explains slower
    steps and conflicts with other automation tools.
    """
    from artemis.clients.screen_client_factory import hierarchy_backend_sentence

    relative = ctx.data_engine.get_relative_time if ctx.data_engine else None
    environment_line = hierarchy_backend_sentence(
        getattr(ctx, "ui_adb_client", None), relative_time=relative
    )
    if not environment_line:
        return None
    return (
        "## Run Environment (facts, include verbatim under a short "
        "'Environment' note only if the source changed mid-run or the "
        "user asked about it)" + chr(10) + environment_line
    )


def _target_schema_json(output_config: OutputConfig) -> str | None:
    """Return the structured output schema as JSON text for the prompt."""
    schema = output_config.structured_output_schema()
    if schema is None:
        return None
    try:
        if isinstance(schema, type) and issubclass(schema, BaseModel):
            schema = schema.model_json_schema()
        return json.dumps(schema, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"Could not render the structured output schema for outputter: {e}")
        return None


def _initial_messages(
    ctx: ArtemisContext,
    output_config: OutputConfig,
    graph_output: State,
    plan_and_history: str | None,
) -> list[BaseMessage]:
    human_message = _prompt_template().render(
        initial_goal=graph_output.initial_goal,
        structured_output=output_config.structured_output,
        output_description=output_config.output_description,
        target_schema=_target_schema_json(output_config),
        plan_and_history=plan_and_history,
    )
    content: list[dict[str, Any]] = []

    raw_data = graph_output.operator_raw_data
    screenshot_b64 = raw_data.get("screenshot_b64") if raw_data else None
    if screenshot_b64:
        # Image-first: vision models ground better when the screenshot
        # precedes the instruction/context text.
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": image_data_uri(screenshot_b64)},
            }
        )

    content.append({"type": "text", "text": human_message})

    if ctx.data_engine:
        verification = _verification_block(ctx, graph_output)
        if verification:
            content.append({"type": "text", "text": verification})

    environment = _environment_block(ctx)
    if environment:
        content.append({"type": "text", "text": environment})

    return [SystemMessage(content=_SYSTEM_MESSAGE), HumanMessage(content=content)]


def _outputter_tools(ctx: ArtemisContext) -> list[BaseTool]:
    """Return the history, notes and video tools available to the outputter."""
    search_tool, replay_tool, screenshot_tool = get_history_tools(ctx)
    return [
        search_tool,
        replay_tool,
        screenshot_tool,
        get_list_notes_tool_pure(ctx),
        get_read_note_tool_pure(ctx),
        get_save_note_tool_pure(ctx),
        get_update_note_tool_pure(ctx),
        get_append_note_tool_pure(ctx),
        get_video_analyzer_tool_pure(ctx),
    ]


def _tool_name(tool_call: dict) -> str:
    name = tool_call["name"]
    return name.split(":")[-1] if ":" in name else name


class _Investigation:
    """Runs the outputter's tool calls and tracks whether the report was saved."""

    def __init__(self, tool_map: dict[str, BaseTool], graph_output: State, llm: Any):
        self.tool_map = tool_map
        self.graph_output = graph_output
        self.llm = llm
        self.report_written = False

    async def _run_tool(self, tc: dict) -> list[BaseMessage]:
        tool_name = _tool_name(tc)
        args = tc["args"]
        logger.info(f"Outputter executing tool {tool_name} with args: {args}")
        try:
            selected_tool = self.tool_map.get(tool_name)
            if selected_tool:
                result = await invoke_tool_with_injection(
                    tool=selected_tool,
                    args=args,
                    tool_call_id=tc["id"],
                    state=self.graph_output,
                    record_trace=True,
                )
            else:
                result = ToolFailure(f"Error: Tool {tool_name} is not supported.")
            status = "error" if is_tool_failure(result) else "success"
        except Exception as e:
            logger.error(f"Error running tool {tool_name}: {e}")
            result = f"Error running tool {tool_name}: {e}"
            status = "error"

        if (
            tool_name in _WRITE_TOOLS
            and status == "success"
            and isinstance(args, dict)
            and args.get("key") == OUTPUT_NOTE_KEY
        ):
            self.report_written = True

        # Screenshots are wrapped in whatever message type the provider accepts
        # for images; text results stay a plain ToolMessage.
        return tool_result_messages(tc["id"], result, name=tool_name, status=status, llm=self.llm)

    async def execute_tool_calls(self, tool_calls: list[dict]) -> list[BaseMessage]:
        """Run the tool calls of one turn and return the reply messages.

        Consecutive reads run in parallel; each note write runs on its own, in
        order. All ToolMessages come first, in call order, followed by any
        HumanMessages carrying images: some providers reject a tool call whose
        results are not directly after it.
        """
        per_call: list[list[BaseMessage]] = []
        batch: list[dict] = []

        async def flush() -> None:
            if batch:
                per_call.extend(await asyncio.gather(*(self._run_tool(tc) for tc in batch)))
                batch.clear()

        for tc in tool_calls:
            if _tool_name(tc) in _WRITE_TOOLS:
                await flush()
                per_call.append(await self._run_tool(tc))
            else:
                batch.append(tc)
        await flush()

        produced = [m for msgs in per_call for m in msgs]
        tool_messages = [m for m in produced if isinstance(m, ToolMessage)]
        carriers = [m for m in produced if not isinstance(m, ToolMessage)]
        return tool_messages + carriers


def _read_saved_report(ctx: ArtemisContext) -> str | None:
    base_dir = _notes_base_dir(ctx)
    if not base_dir:
        return None
    try:
        report = read_note_content(base_dir, OUTPUT_NOTE_KEY).strip()
    except Exception as e:
        logger.warning(f"Could not read the saved outputter report: {e}")
        return None
    return report or None


async def _run_react_loop(
    investigation: _Investigation,
    messages: list[BaseMessage],
    llm_with_tools: Any,
    llm_fallback_with_tools: Any,
) -> str | None:
    """Call the model and its tools until it answers; return the answer or None."""
    nudged = False
    for turn in range(MAX_TURNS):
        logger.info(f"Outputter ReAct turn {turn + 1}")
        final_turn = turn == MAX_TURNS - 1
        if final_turn:
            messages.append(HumanMessage(content=_FINAL_TURN_NOTICE))

        response = await with_fallback(
            main_call=lambda: invoke_llm_with_timeout_message(acomplete(llm_with_tools, messages)),
            fallback_call=lambda: invoke_llm_with_timeout_message(
                acomplete(llm_fallback_with_tools, messages)
            ),
        )
        text = content_to_text(response.content).strip()

        if response.tool_calls and not final_turn:
            messages.append(response)
            messages.extend(await investigation.execute_tool_calls(response.tool_calls))
            continue

        if text:
            messages.append(response)
            return text

        if final_turn:
            logger.warning("Outputter final turn produced no answer text.")
            return None

        if nudged:
            logger.warning("Outputter reply was empty again after a nudge.")
            return None

        # An empty model turn is not kept: Gemini rejects messages without parts.
        logger.warning("Outputter reply was empty; asking once more for an answer.")
        messages.append(HumanMessage(content=_EMPTY_REPLY_NUDGE))
        nudged = True
    return None


async def _format_structured(
    output_config: OutputConfig,
    graph_output: State,
    raw_answer: str,
    saved_report: str | None,
    llm: Any,
    llm_fallback: Any,
) -> Any:
    """Convert the answer to the configured schema with a second model call.

    Returns None if no schema is configured. If the call fails, falls back to
    parsing the answer locally, and then to the answer text. Never raises.
    """
    bound = output_config.bind_structured_output(llm, llm_fallback)
    if bound is None:
        return None
    structured_llm, structured_llm_fallback = bound
    logger.info("Formatting raw output to structured format...")

    format_messages: list[BaseMessage] = [SystemMessage(content=_FORMAT_SYSTEM_MESSAGE)]
    if saved_report:
        format_messages.append(
            HumanMessage(content=f"Supporting Evidence (saved report):\n{saved_report}")
        )
    format_messages.append(
        HumanMessage(
            content=(f"Initial Goal: {graph_output.initial_goal}\nRaw Summary:\n{raw_answer}")
        )
    )

    try:
        response = await with_fallback(
            main_call=lambda: invoke_llm_with_timeout_message(
                structured_llm.ainvoke(format_messages)
            ),
            fallback_call=lambda: invoke_llm_with_timeout_message(
                structured_llm_fallback.ainvoke(format_messages)
            ),
        )
        if response is None:
            raise ValueError("the model returned no structured output")
    except Exception as e:
        logger.warning(f"Structured formatting failed ({e}); parsing the answer locally.")
        schema = output_config.structured_output_schema()
        parsed = parse_structured(
            raw_answer,
            schema=schema if isinstance(schema, type) and issubclass(schema, BaseModel) else None,
        )
        if isinstance(parsed, ParseFailure):
            logger.warning("Local parse failed as well; returning the answer as text.")
            return raw_answer
        response = parsed

    if isinstance(response, BaseModel):
        return response.model_dump()
    return response


def _coerce_text_answer(raw_answer: str) -> Any:
    """Parse the answer if it looks like JSON; otherwise return it unchanged."""
    stripped = raw_answer.strip()
    if stripped.startswith(("{", "[", "```")):
        parsed = parse_structured(stripped)
        if not isinstance(parsed, ParseFailure):
            return parsed
        # The raw text is a legitimate final answer; the miss is only
        # noteworthy because the answer *looked* like JSON.
        logger.warning(
            "Outputter answer looked like JSON but could not be parsed"
            f" ({parsed.error}); returning it as plain text."
        )
    return raw_answer


@trace(type="agent", name="outputter")
async def outputter(
    ctx: ArtemisContext,
    output_config: OutputConfig,
    graph_output: State,
    plan_and_history: str | None = None,
) -> dict:
    logger.info("Starting Outputter Agent")

    if plan_and_history is None and ctx.data_engine:
        plan_and_history = _resolve_plan_and_history(ctx)

    messages = _initial_messages(ctx, output_config, graph_output, plan_and_history)

    tools = _outputter_tools(ctx)
    llm = get_llm(ctx=ctx, name="outputter", is_utils=True)
    llm_fallback = get_llm(ctx=ctx, name="outputter", is_utils=True, use_fallback=True)
    investigation = _Investigation({t.name: t for t in tools}, graph_output, llm)

    raw_answer = await _run_react_loop(
        investigation,
        messages,
        llm.bind_tools(tools=tools),
        llm_fallback.bind_tools(tools=tools),
    )

    saved_report = _read_saved_report(ctx) if investigation.report_written else None
    if raw_answer is None:
        if saved_report:
            logger.warning("Outputter produced no answer; using its saved report instead.")
            raw_answer = saved_report
        else:
            raw_answer = _MAX_TURNS_ERROR
            logger.error(raw_answer)

    if output_config.structured_output:
        formatted = await _format_structured(
            output_config, graph_output, raw_answer, saved_report, llm, llm_fallback
        )
        if formatted is not None:
            return formatted

    return _coerce_text_answer(raw_answer)
