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
import base64
import json
import os
from pathlib import Path
from typing import Any
import uuid

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, ToolMessage

from artemis.context import ArtemisContext
from artemis.data_engine.trace import (
    CURRENT_TRACE_ID,
    TraceSpan,
    trace,
    trace_langchain_tool,
)
from artemis.graph.state import State
from artemis.services.llm import acomplete, get_llm, invoke_llm_with_timeout_message
from artemis.tools.command_tool import get_run_short_adb_command_tool
from artemis.tools.diagnoser_submit_answer_tool import get_submit_answer_tool
from artemis.tools.history import get_history_tools
from artemis.tools.index import get_tool_by_name
from artemis.tools.log_tool import get_analyze_logs_tool
from artemis.tools.mobile.read_hierarchy import get_ui_hierarchy_tool
from artemis.tools.scratchpad import get_list_notes_tool, get_read_note_tool
from artemis.tools.tool_wrapper import (
    get_tool_result_content,
    invoke_tool_with_injection,
    split_multimodal_result,
    tool_result_messages,
)
from artemis.tools.video_tool import get_video_analyzer_tool
from artemis.tools.wait_tool import get_wait_tool
from artemis.utils.logger import get_logger
from artemis.memory.context_policy import build_history_for
from artemis.utils.task_tree import (
    get_active_subgoal_hashes,
    get_recent_subgoal_hashes,
)

logger = get_logger(__name__)

_MAX_ITERATIONS = 30

_FINAL_TURN_WARNING = (
    "[WARNING] This is your final iteration. Call 'submit_answer' to provide your final diagnosis."
)


class Diagnoser:
    def __init__(self, ctx: ArtemisContext):
        self.ctx = ctx
        self.is_device_online = self._is_device_online()
        #: The model of the current run; decides how screenshot results travel.
        self._llm = None

    def _is_device_online(self) -> bool:
        try:
            if self.ctx.adb_client is None:
                return False
            if getattr(self.ctx.adb_client, "is_online", True) is False:
                return False
            self.ctx.get_adb_client()
            return True
        except Exception:
            return False

    def _load_prompt_config(self) -> tuple[str, str]:
        """Loads the system prompt and checker-feedback template from disk."""
        prompt_path = Path(__file__).parent.joinpath("diagnoser.json")
        if not prompt_path.exists():
            raise FileNotFoundError(f"Prompt file not found at {prompt_path}")

        with open(prompt_path, encoding="utf-8") as f:
            prompt_data = json.load(f)

        system_prompt = prompt_data.get("system_prompt", "")
        if isinstance(system_prompt, list):
            system_prompt = "\n".join(system_prompt)
        checker_feedback_template = prompt_data.get("checker_feedback_template", "")
        if isinstance(checker_feedback_template, list):
            checker_feedback_template = "\n".join(checker_feedback_template)
        return system_prompt, checker_feedback_template

    def _build_traced_tools(self) -> list:
        """Assembles the tool set (honoring the offline/video gates) and wraps it in tracing."""
        all_tools = [
            get_analyze_logs_tool(self.ctx),
            get_read_note_tool(self.ctx),
            get_list_notes_tool(self.ctx),
            get_run_short_adb_command_tool(self.ctx),
            get_wait_tool(self.ctx),
            get_ui_hierarchy_tool(self.ctx),
            *get_history_tools(self.ctx),
            get_submit_answer_tool(self.ctx),
        ]
        if not self.is_device_online:
            logger.info(
                "Diagnoser running in OFFLINE environment: stripping adb short"
                " command and wait tools."
            )
            all_tools = [
                t
                for t in all_tools
                if t.name not in ("run_adb_command", "wait")
                and not t.name.endswith(":run_adb_command")
                and not t.name.endswith(":wait")
            ]

        if self.ctx.execution_setup and self.ctx.execution_setup.video_recording_tools_enabled:
            all_tools.append(get_video_analyzer_tool(self.ctx, role="diagnoser"))

        return [trace_langchain_tool(t, self.ctx) for t in all_tools]

    def _build_plan_and_history(self) -> str:
        """Renders the task plan and recent step history from the DataEngine."""
        task_plan = "No task plan yet."
        steps = []
        subgoal_hash = "default"

        if self.ctx.data_engine:
            notes_dir = Path(self.ctx.data_engine.base_dir) / "notes"
            task_plan_path = notes_dir / "task_plan.md"

            if task_plan_path.exists():
                try:
                    task_plan = task_plan_path.read_text(encoding="utf-8")

                    subgoal_hash, _ = get_active_subgoal_hashes(task_plan)
                except Exception as e:
                    logger.error(f"Failed to parse active subgoal in Diagnoser: {e}")
                    task_plan = f"Error reading task plan: {e}"

            steps = self.ctx.data_engine.get_agent_friendly_steps()

        plan_and_history = "No plan or history available."
        if self.ctx.data_engine and task_plan != "No task plan yet.":
            try:
                keep_hashes = get_recent_subgoal_hashes(
                    steps,
                    subgoal_hash,
                    self.ctx.data_engine.base_dir,
                )

                plan_and_history = build_history_for(
                    "diagnoser",
                    task_plan,
                    steps,
                    subgoal_hash,
                    keep_subgoal_hashes=keep_hashes,
                    engine=self.ctx.data_engine,
                )
            except Exception as e:
                logger.error(f"Failed to build plan and history in Diagnoser: {e}")
                plan_and_history = f"Error building plan and history: {e}"
        return plan_and_history

    def _collect_checker_feedback(self, state: State) -> str:
        """Renders verification findings injected by the checkpoint harvest, if any."""
        findings = getattr(state, "operator_feedback", None)
        if findings:
            return "\n".join(f"- {f}" for f in findings)
        return ""

    def _read_screenshot_b64(self, state: State) -> str | None:
        """Reads the latest screenshot as base64, or ``None`` when unavailable."""
        if state.latest_screenshot and os.path.exists(state.latest_screenshot):
            try:
                with open(state.latest_screenshot, "rb") as f:
                    return base64.b64encode(f.read()).decode("utf-8")
            except Exception as e:
                logger.error(f"Failed to read latest screenshot in Diagnoser: {e}")
        return None

    def _build_initial_messages(
        self,
        prompt: str,
        state: State,
        system_prompt: str,
        checker_feedback_template: str,
        plan_and_history: str,
        checker_feedback: str,
        minimal_list: str,
    ) -> list[BaseMessage]:
        """Build the initial prompt with history, device state and screenshot."""
        content = []
        content.append({"type": "text", "text": f"Initial Goal: {state.initial_goal}\n"})
        content.append(
            {
                "type": "text",
                "text": f"--- Plan & History ---\n{plan_and_history}\n",
            }
        )

        if checker_feedback and checker_feedback_template:
            formatted_feedback = checker_feedback_template.format(checker_feedback=checker_feedback)
            content.append({"type": "text", "text": formatted_feedback})

        if minimal_list:
            content.append(
                {
                    "type": "text",
                    "text": f"--- Visible UI Elements ---\n{minimal_list}\n",
                }
            )

        latest_screenshot_b64 = self._read_screenshot_b64(state)
        if latest_screenshot_b64:
            content.append({"type": "text", "text": "--- Current Screenshot ---"})
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{latest_screenshot_b64}"},
                }
            )

        content.append({"type": "text", "text": f"--- Diagnostic Query ---\n{prompt}"})

        return [
            SystemMessage(content=system_prompt),
            HumanMessage(content=content),
        ]

    async def _await_running_jobs(self) -> None:
        """Blocks until every still-running background job has settled."""
        running_jobs = [
            job for job in self.ctx.background_jobs.values() if job.get("status") == "running"
        ]
        if running_jobs:
            await asyncio.gather(
                *(job["task"] for job in running_jobs if job.get("task") is not None),
                return_exceptions=True,
            )

    def _drain_finished_jobs(self) -> list[ToolMessage]:
        """Converts finished, unconsumed background jobs into tool messages."""
        new_tool_messages = []
        for job in self.ctx.background_jobs.values():
            if job.get("status") in (
                "completed",
                "failed",
            ) and not job.get("consumed", False):
                tm_status = "success" if job["status"] == "completed" else "error"
                tm = ToolMessage(
                    tool_call_id=job["tool_call_id"],
                    content=job["result"] or "",
                    status=tm_status,
                )
                new_tool_messages.append(tm)
                job["consumed"] = True
        return new_tool_messages

    def _cancel_running_jobs(self) -> None:
        """Cancels any background job still running when the loop exits."""
        for job in self.ctx.background_jobs.values():
            if job.get("status") == "running" and job.get("task") is not None:
                job["task"].cancel()

    def _prepare_iteration_llm(
        self,
        base_llm,
        traced_tools: list,
        iteration: int,
        current_messages: list[BaseMessage],
    ):
        """Appends the iteration notice and binds the tools available this turn.

        The final iteration is restricted to ``submit_answer`` so the model
        must conclude; earlier iterations get a progress nudge and the full
        tool set.
        """
        if iteration == _MAX_ITERATIONS - 1:
            current_messages.append(HumanMessage(content=_FINAL_TURN_WARNING))
            submit_tool = next(
                (t for t in traced_tools if t.name == "submit_answer"),
                None,
            )
            return base_llm.bind_tools(tools=[submit_tool] if submit_tool else [])

        if iteration > 0:
            current_messages.append(
                HumanMessage(
                    content=(
                        "You have not completed the diagnosis yet"
                        f" (iteration {iteration + 1} of {_MAX_ITERATIONS})."
                    )
                )
            )
        return base_llm.bind_tools(tools=traced_tools)

    def _token_usage_payload(self, response) -> dict | None:
        """Extracts token-usage counters from the response metadata, if present."""
        if not hasattr(response, "usage_metadata") or response.usage_metadata is None:
            return None
        usage = response.usage_metadata
        if isinstance(usage, dict):
            prompt_token_count = usage.get("prompt_token_count")
            candidates_token_count = usage.get("candidates_token_count")
            cached_content_token_count = usage.get("cached_content_token_count")
        else:
            prompt_token_count = getattr(usage, "prompt_token_count", None)
            candidates_token_count = getattr(usage, "candidates_token_count", None)
            cached_content_token_count = getattr(usage, "cached_content_token_count", None)

        return {
            "prompt_token_count": prompt_token_count,
            "candidates_token_count": candidates_token_count,
            "cached_content_token_count": (cached_content_token_count),
        }

    async def _invoke_model(
        self,
        llm,
        current_messages: list[BaseMessage],
        context_components: dict,
    ):
        """Invokes the model inside a trace span carrying the context payload."""
        with TraceSpan(name="gemini_diagnoser_call", ctx=self.ctx) as span:
            span.payload = {"context_components": context_components}
            response = await invoke_llm_with_timeout_message(acomplete(llm, current_messages))
            span.result = response.content if hasattr(response, "content") else str(response)

            token_usage = self._token_usage_payload(response)
            if token_usage is not None:
                if span.payload is None:
                    span.payload = {}
                span.payload["token_usage"] = token_usage
        return response

    def _find_submit_call(self, tool_calls: list) -> dict | None:
        """Finds a ``submit_answer`` call among the response's tool calls."""
        return next(
            (
                tc
                for tc in tool_calls
                if tc["name"] == "submit_answer" or tc["name"].endswith(":submit_answer")
            ),
            None,
        )

    def _handle_submit_call(self, submit_call: dict, traced_tools: list) -> str:
        """Records the submitted answer and renders it as the run's outcome."""
        args = submit_call["args"]
        analysis = args.get("analysis", "No analysis provided.")
        steps = args.get("actionable_steps", [])

        submit_tool = next(
            (
                t
                for t in traced_tools
                if t.name == "submit_answer" or t.name.endswith(":submit_answer")
            ),
            None,
        )
        if submit_tool:
            submit_tool.invoke(args)

        outcome = f"Analysis: {analysis} | Actionable Steps: "
        for step in steps:
            outcome += f"{step} "
        return outcome

    def _max_iteration_outcome(self, response, outcome: str) -> str:
        """Renders the outcome when the loop cap is hit with tool calls pending."""
        tool_names = ", ".join(tc["name"] for tc in response.tool_calls)
        warning = (
            "\n\n[Warning: Diagnoser reached the maximum iteration"
            f" limit of {_MAX_ITERATIONS} and could not execute"
            f" tool(s): {tool_names}]"
        )
        if outcome == "No result":
            return (
                "Diagnoser reached the maximum iteration limit of"
                f" {_MAX_ITERATIONS} while attempting to call tools:"
                f" {tool_names}"
            )
        return outcome + warning

    async def _run_video_job(self, job_id: str, tc: dict, traced_tools: list, state: State) -> None:
        """Runs a ``video_analyzer`` call in the background and records its result."""
        try:
            tool_to_run = next(
                (t for t in traced_tools if t.name == "video_analyzer"),
                None,
            )
            if tool_to_run:
                args = dict(tc["args"])
                result_obj = await invoke_tool_with_injection(
                    tool=tool_to_run,
                    args=args,
                    tool_call_id=tc["id"],
                    state=state,
                )
                result_content = get_tool_result_content(result_obj)
                self.ctx.background_jobs[job_id]["result"] = result_content
                self.ctx.background_jobs[job_id]["status"] = "completed"
                if self.ctx.data_engine:
                    self.ctx.data_engine.unregister_background_task(
                        task_id=job_id,
                        status="completed",
                    )
            else:
                self.ctx.background_jobs[job_id]["result"] = "Error: video_analyzer tool not found"
                self.ctx.background_jobs[job_id]["status"] = "failed"
                if self.ctx.data_engine:
                    self.ctx.data_engine.unregister_background_task(task_id=job_id, status="failed")
        except asyncio.CancelledError:
            logger.info(f"Background job {job_id} cancelled.")
            raise
        except Exception as e:
            logger.error(f"Background job {job_id} failed: {e}")
            self.ctx.background_jobs[job_id]["result"] = str(e)
            self.ctx.background_jobs[job_id]["status"] = "failed"
            if self.ctx.data_engine:
                self.ctx.data_engine.unregister_background_task(task_id=job_id, status="failed")

    def _start_video_job(self, tc: dict, traced_tools: list, state: State) -> ToolMessage:
        """Registers a background job for ``video_analyzer`` and acknowledges it."""
        job_id = f"job_{uuid.uuid4().hex}"
        self.ctx.background_jobs[job_id] = {
            "tool_call_id": tc["id"],
            "status": "running",
            "result": None,
            "consumed": False,
            "task": None,
        }
        if self.ctx.data_engine:
            self.ctx.data_engine.register_background_task(
                task_id=job_id,
                summary=(f"Diagnoser Video Analyzer Tool Call: {tc['id']}"),
            )

        task = asyncio.create_task(self._run_video_job(job_id, tc, traced_tools, state))
        self.ctx.background_jobs[job_id]["task"] = task

        return ToolMessage(
            tool_call_id=tc["id"],
            content=(
                f"Video analyzer started. Job ID: {job_id}."
                " [Warning] No background logs, status"
                " updates, or errors will be logged."
            ),
            status="success",
        )

    async def _run_tool(self, tc: dict, traced_tools: list, state: State) -> list[BaseMessage]:
        """Executes one requested tool call and wraps its result as messages.

        Text results are one tool message; a step screenshot travels in the
        carrier the model's provider accepts (``tool_result_messages``), so
        base64 never lands in a text field.
        """
        tool_name = tc["name"].split(":")[-1] if ":" in tc["name"] else tc["name"]
        logger.info(f"Diagnoser requested tool: {tool_name}")

        result = ""
        status = "success"
        try:
            if tool_name == "video_analyzer":
                return [self._start_video_job(tc, traced_tools, state)]

            if ":" in tool_name:
                tool_to_run = get_tool_by_name(tool_name, traced_tools)
            else:
                tool_to_run = next(
                    (t for t in traced_tools if t.name == tool_name),
                    None,
                )
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
                        result_str, _ = split_multimodal_result(result)
                        span.result = result
                        if result_str.startswith("Error"):
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

        return tool_result_messages(tc["id"], result, name=tool_name, status=status, llm=self._llm)

    async def _execute_tool_calls(
        self, tool_calls: list, traced_tools: list, state: State
    ) -> list[BaseMessage]:
        """Executes all requested tools in parallel, skipping ``google_search``."""
        active_tool_calls = [
            tc
            for tc in tool_calls
            if (tc["name"].split(":")[-1] if ":" in tc["name"] else tc["name"]) != "google_search"
        ]
        batches = await asyncio.gather(
            *(self._run_tool(tc, traced_tools, state) for tc in active_tool_calls)
        )
        return [message for batch in batches for message in batch]

    @trace(type="agent", name="diagnoser")
    async def run(self, prompt: str, state: State) -> str:
        """Runs the Diagnoser and returns its summary."""

        system_prompt, checker_feedback_template = self._load_prompt_config()

        base_llm = get_llm(ctx=self.ctx, name="diagnoser")
        self._llm = base_llm
        traced_tools = self._build_traced_tools()

        plan_and_history = self._build_plan_and_history()
        checker_feedback = self._collect_checker_feedback(state)
        minimal_list = (
            self._format_minimal_list(state.latest_ui_hierarchy)
            if state.latest_ui_hierarchy
            else ""
        )

        current_messages = self._build_initial_messages(
            prompt,
            state,
            system_prompt,
            checker_feedback_template,
            plan_and_history,
            checker_feedback,
            minimal_list,
        )
        context_components = {
            "system_prompt": system_prompt,
            "plan_and_history": plan_and_history,
            "checker_feedback": checker_feedback,
            "visible_ui_elements": minimal_list,
            "diagnostic_query": prompt,
        }

        self.ctx.background_jobs = {}
        outcome = "No result"

        try:
            for i in range(_MAX_ITERATIONS):
                # Include background results before asking for the final answer.
                if i == _MAX_ITERATIONS - 1:
                    await self._await_running_jobs()

                new_tool_messages = self._drain_finished_jobs()
                if new_tool_messages:
                    current_messages.extend(new_tool_messages)

                llm = self._prepare_iteration_llm(base_llm, traced_tools, i, current_messages)

                response = await self._invoke_model(llm, current_messages, context_components)

                if response.content:
                    outcome = response.content

                if not response.tool_calls:
                    break

                submit_call = self._find_submit_call(response.tool_calls)
                if submit_call:
                    outcome = self._handle_submit_call(submit_call, traced_tools)
                    break

                if i == _MAX_ITERATIONS - 1:
                    outcome = self._max_iteration_outcome(response, outcome)
                    break

                current_messages.append(response)
                current_messages.extend(
                    await self._execute_tool_calls(response.tool_calls, traced_tools, state)
                )
        finally:
            self._cancel_running_jobs()

        return outcome

    def _format_minimal_list(self, fused_xml: list[dict[str, Any]]) -> str:
        """Formats fused XML into a minimal structured list."""
        lines = []
        for i, node in enumerate(fused_xml):
            text = node.get("text") or node.get("content-desc") or ""
            bounds = node.get("bounds")

            ocr_elements = node.get("ocr_elements")
            if ocr_elements:
                for ocr in ocr_elements:
                    lines.append(f"[{i}] OCR Text: '{ocr['text']}' | Bounds: {ocr['bounds']}")
            elif text.strip():
                lines.append(f"[{i}] Text: '{text.strip()}' | Bounds: {bounds}")

        return "\n".join(lines)
