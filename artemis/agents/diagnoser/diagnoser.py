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

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from artemis.context import ArtemisContext
from artemis.data_engine.trace import (
    CURRENT_TRACE_ID,
    TraceSpan,
    trace,
    trace_langchain_tool,
)
from artemis.graph.state import State
from artemis.services.llm import get_llm, invoke_llm_with_timeout_message
from artemis.tools.command_tool import get_run_short_adb_command_tool
from artemis.tools.diagnoser_submit_answer_tool import get_submit_answer_tool
from artemis.tools.index import get_tool_by_name
from artemis.tools.log_tool import get_analyze_logs_tool
from artemis.tools.mobile.read_hierarchy import get_ui_hierarchy_tool
from artemis.tools.scratchpad import get_list_notes_tool, get_read_note_tool
from artemis.tools.tool_wrapper import (
    get_tool_result_content,
    invoke_tool_with_injection,
)
from artemis.tools.video_tool import get_video_analyzer_tool
from artemis.tools.wait_tool import get_wait_tool
from artemis.utils.logger import get_logger
from artemis.utils.task_tree import (
    build_plan_and_history,
    get_active_subgoal_hashes,
    get_recent_subgoal_hashes,
)

logger = get_logger(__name__)


class Diagnoser:
    def __init__(self, ctx: ArtemisContext):
        self.ctx = ctx
        self.is_device_online = self._is_device_online()

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

    @trace(type="agent", name="diagnoser")
    async def run(self, prompt: str, state: State) -> str:
        """Runs the Diagnoser and returns its summary."""

        # 1. Load prompt
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

        # 2. Prepare LLM
        llm = get_llm(ctx=self.ctx, name="diagnoser")

        log_tool = get_analyze_logs_tool(self.ctx)

        read_note_tool = get_read_note_tool(self.ctx)
        list_notes_tool = get_list_notes_tool(self.ctx)
        adb_command_tool = get_run_short_adb_command_tool(self.ctx)
        wait_tool = get_wait_tool(self.ctx)
        ui_hierarchy_tool = get_ui_hierarchy_tool(self.ctx)
        submit_answer_tool = get_submit_answer_tool(self.ctx)

        all_tools = [
            log_tool,
            read_note_tool,
            list_notes_tool,
            adb_command_tool,
            wait_tool,
            ui_hierarchy_tool,
            submit_answer_tool,
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
            video_tool = get_video_analyzer_tool(self.ctx, role="diagnoser")
            all_tools.append(video_tool)

        traced_tools = [trace_langchain_tool(t, self.ctx) for t in all_tools]

        # Bind custom tools
        base_llm = llm

        # 4. Get context from DataEngine and State
        initial_goal = state.initial_goal

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

        # Build task tree
        plan_and_history = "No plan or history available."
        if self.ctx.data_engine and task_plan != "No task plan yet.":
            try:
                keep_hashes = get_recent_subgoal_hashes(
                    steps,
                    subgoal_hash,
                    window_steps=self.ctx.agent_config.history_window_steps,
                )

                plan_and_history = build_plan_and_history(
                    task_plan,
                    steps,
                    subgoal_hash,
                    keep_subgoal_hashes=keep_hashes,
                )
            except Exception as e:
                logger.error(f"Failed to build plan and history in Diagnoser: {e}")
                plan_and_history = f"Error building plan and history: {e}"

        # Get Checker Feedback
        checker_feedback = ""
        if self.ctx.data_engine:
            notes_dir = Path(self.ctx.data_engine.base_dir) / "notes"
            verification_chat_path = notes_dir / f"verification_chat_{subgoal_hash}.json"
            if verification_chat_path.exists():
                try:
                    turns = json.loads(verification_chat_path.read_text(encoding="utf-8"))
                    dialogue_lines = []
                    for t in turns:
                        role = "Operator" if t["role"] == "operator" else "Checker"
                        dialogue_lines.append(f"**{role} (Round {t['round']})**:\n{t['content']}")
                    checker_feedback = "\n\n".join(dialogue_lines)
                except Exception as e:
                    logger.error(f"Error reading verification chat in Diagnoser: {e}")

        # Get Current Screenshot and UI Hierarchy
        latest_screenshot_b64 = None
        minimal_list = ""

        if state.latest_screenshot and os.path.exists(state.latest_screenshot):
            try:
                with open(state.latest_screenshot, "rb") as f:
                    latest_screenshot_b64 = base64.b64encode(f.read()).decode("utf-8")
            except Exception as e:
                logger.error(f"Failed to read latest screenshot in Diagnoser: {e}")

        if state.latest_ui_hierarchy:
            minimal_list = self._format_minimal_list(state.latest_ui_hierarchy)

        # Construct rich human message
        content = []
        content.append({"type": "text", "text": f"Initial Goal: {initial_goal}\n"})
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

        if latest_screenshot_b64:
            content.append({"type": "text", "text": "--- Current Screenshot ---"})
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{latest_screenshot_b64}"},
                }
            )

        content.append({"type": "text", "text": f"--- Diagnostic Query ---\n{prompt}"})

        current_messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=content),
        ]

        self.ctx.background_jobs = {}

        max_iterations = 30
        outcome = "No result"

        try:
            for i in range(max_iterations):
                # 5. Agent Loop Injection & Final Turn Guard
                if i == max_iterations - 1:
                    # Identify all jobs in the registry with status "running"
                    running_jobs = [
                        job
                        for job in self.ctx.background_jobs.values()
                        if job.get("status") == "running"
                    ]
                    if running_jobs:
                        # Await all associated task objects
                        await asyncio.gather(
                            *(job["task"] for job in running_jobs if job.get("task") is not None),
                            return_exceptions=True,
                        )

                # Iterate through the background_jobs registry for all jobs where status is "completed" or "failed" and consumed is false
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

                if new_tool_messages:
                    current_messages.extend(new_tool_messages)

                if i == max_iterations - 1:
                    current_messages.append(
                        HumanMessage(
                            content=(
                                "[WARNING] This is your final iteration. Call"
                                " 'submit_answer' to provide your final"
                                " diagnosis."
                            )
                        )
                    )
                    submit_tool = next(
                        (t for t in traced_tools if t.name == "submit_answer"),
                        None,
                    )
                    llm = base_llm.bind_tools(tools=[submit_tool] if submit_tool else [])
                else:
                    if i > 0:
                        current_messages.append(
                            HumanMessage(
                                content=(
                                    "You have not completed the diagnosis yet"
                                    f" (iteration {i + 1} of {max_iterations})."
                                )
                            )
                        )
                    llm = base_llm.bind_tools(tools=traced_tools)

                async def run_stream():
                    full_response = None
                    trace_id = CURRENT_TRACE_ID.get()
                    async for chunk in llm.astream(current_messages):
                        if full_response is None:
                            full_response = chunk
                        else:
                            full_response += chunk

                        pass
                    return full_response

                with TraceSpan(name="gemini_diagnoser_call", ctx=self.ctx) as span:
                    span.payload = {
                        "context_components": {
                            "system_prompt": system_prompt,
                            "plan_and_history": plan_and_history,
                            "checker_feedback": checker_feedback,
                            "visible_ui_elements": minimal_list,
                            "diagnostic_query": prompt,
                        }
                    }
                    response = await invoke_llm_with_timeout_message(run_stream())
                    span.result = (
                        response.content if hasattr(response, "content") else str(response)
                    )

                    if hasattr(response, "usage_metadata") and response.usage_metadata is not None:
                        usage = response.usage_metadata
                        if isinstance(usage, dict):
                            prompt_token_count = usage.get("prompt_token_count")
                            candidates_token_count = usage.get("candidates_token_count")
                            cached_content_token_count = usage.get("cached_content_token_count")
                        else:
                            prompt_token_count = getattr(usage, "prompt_token_count", None)
                            candidates_token_count = getattr(usage, "candidates_token_count", None)
                            cached_content_token_count = getattr(
                                usage, "cached_content_token_count", None
                            )

                        if span.payload is None:
                            span.payload = {}

                        span.payload["token_usage"] = {
                            "prompt_token_count": prompt_token_count,
                            "candidates_token_count": candidates_token_count,
                            "cached_content_token_count": (cached_content_token_count),
                        }

                if response.content:
                    outcome = response.content

                if not response.tool_calls:
                    break

                submit_call = next(
                    (
                        tc
                        for tc in response.tool_calls
                        if tc["name"] == "submit_answer" or tc["name"].endswith(":submit_answer")
                    ),
                    None,
                )
                if submit_call:
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

                    outcome = f"Analysis: {analysis} | Actionalble Steps: "
                    for step in steps:
                        outcome += f"{step} "
                    break

                if i == max_iterations - 1:
                    tool_names = ", ".join(tc["name"] for tc in response.tool_calls)
                    warning = (
                        "\n\n[Warning: Diagnoser reached the maximum iteration"
                        f" limit of {max_iterations} and could not execute"
                        f" tool(s): {tool_names}]"
                    )
                    if outcome == "No result":
                        outcome = (
                            "Diagnoser reached the maximum iteration limit of"
                            f" {max_iterations} while attempting to call tools:"
                            f" {tool_names}"
                        )
                    else:
                        outcome += warning
                    break

                current_messages.append(response)

                async def run_tool(tc):
                    tool_name = tc["name"].split(":")[-1] if ":" in tc["name"] else tc["name"]
                    logger.info(f"Diagnoser requested tool: {tool_name}")

                    result = ""
                    status = "success"
                    try:
                        if tool_name == "video_analyzer":
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

                            async def run_bg_job():
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
                                        self.ctx.background_jobs[job_id]["result"] = (
                                            "Error: video_analyzer tool not found"
                                        )
                                        self.ctx.background_jobs[job_id]["status"] = "failed"
                                        if self.ctx.data_engine:
                                            self.ctx.data_engine.unregister_background_task(
                                                task_id=job_id, status="failed"
                                            )
                                except asyncio.CancelledError:
                                    logger.info(f"Background job {job_id} cancelled.")
                                    raise
                                except Exception as e:
                                    logger.error(f"Background job {job_id} failed: {e}")
                                    self.ctx.background_jobs[job_id]["result"] = str(e)
                                    self.ctx.background_jobs[job_id]["status"] = "failed"
                                    if self.ctx.data_engine:
                                        self.ctx.data_engine.unregister_background_task(
                                            task_id=job_id, status="failed"
                                        )

                            task = asyncio.create_task(run_bg_job())
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
                                    if isinstance(result, list):
                                        result_str = "\n".join(map(str, result))
                                    elif not isinstance(result, str):
                                        result_str = str(result)
                                    else:
                                        result_str = result
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

                    return ToolMessage(
                        tool_call_id=tc["id"],
                        content=result,
                        status=status,
                    )

                # Execute all requested tools in parallel
                active_tool_calls = [
                    tc
                    for tc in response.tool_calls
                    if (tc["name"].split(":")[-1] if ":" in tc["name"] else tc["name"])
                    != "google_search"
                ]
                tool_outputs = await asyncio.gather(*(run_tool(tc) for tc in active_tool_calls))

                for tm in tool_outputs:
                    current_messages.append(tm)
        finally:
            # 6. Cleanup on Exit
            for job in self.ctx.background_jobs.values():
                if job.get("status") == "running" and job.get("task") is not None:
                    job["task"].cancel()

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
