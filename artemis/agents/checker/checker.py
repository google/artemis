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
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from jinja2 import Template
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from pydantic import BaseModel, Field

from artemis.constants import CHECKER_MAX_ITERATIONS
from artemis.context import ArtemisContext
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.data_engine.trace import (
    TraceSpan,
    trace,
    trace_langchain_tool,
)
from artemis.services.llm import get_llm, invoke_llm_with_timeout_message
from artemis.tools.index import get_tool_by_name
from artemis.tools.scratchpad import get_read_note_tool_pure
from artemis.tools.tool_wrapper import (
    get_tool_result_content,
    invoke_tool_with_injection,
)
from artemis.utils.logger import get_logger
from artemis.utils.notes import get_note_file_path, get_notes_dir
from artemis.utils.ocr_api import is_ocr_configured, perform_ocr
from artemis.utils.ocr_xml_fusion import (
    _crop_image_remove_status_bar,
    _detect_status_bar_height,
    _map_coordinates_back,
    fuse_ocr_with_xml,
)
from artemis.utils.task_tree import (
    build_plan_and_history,
    get_recent_subgoal_hashes,
)
from artemis.utils.verification import (
    append_verification_chat,
    get_verification_chat_path,
    get_verification_chat_rounds,
    read_verification_chat,
)
from artemis.utils.text import safe_extract_text
from artemis.utils.visualization import format_minimal_list_with_elements

logger = get_logger(__name__)


class CheckerResult(BaseModel):
    """Structured output contract for Checker verification."""

    success: bool = Field(
        description="Whether the active subgoal was successfully completed based on visual and note evidence."
    )
    reason: str = Field(
        description="Detailed explanation of the result. If failed, specify what is missing or wrong and give actionable hints for the Operator."
    )


@trace(type="agent", name="checker")
async def run_async_check(
    ctx: ArtemisContext,
    subgoal_text: str,
    subgoal_hash: str | None = None,
    raw_perception_data: dict[str, Any] | None = None,
    latest_ui_hierarchy: list[Any] | None = None,
):
    """Runs the checker verification as a background task."""
    logger.info(f"Starting async Checker for: {subgoal_text}")

    if not ctx.data_engine:
        logger.warning("DataEngine not available, skipping checker.")
        return {"status": "success", "reason": "DataEngine unavailable."}

    notes_dir = get_notes_dir(ctx.data_engine.base_dir)
    status_path = notes_dir / "checker_status.json"

    if ctx.execution_setup and ctx.execution_setup.disable_checker:
        logger.info("Checker is disabled via flag. Skipping.")
        try:
            status_path.write_text(
                json.dumps({"status": "success", "reason": "Checker disabled."}),
                encoding="utf-8",
            )
        except Exception:
            pass
        return {"status": "success", "reason": "Checker disabled."}

    # 1. Set status to running
    try:
        status_path.write_text(
            json.dumps({"status": "running", "subgoal": subgoal_text}),
            encoding="utf-8",
        )
        logger.info(f"Wrote checker status 'running' to {status_path}")
    except Exception as e:
        logger.error(f"Failed to write checker status: {e}")
        return {"status": "success", "reason": f"Status write error: {e}"}

    # 2. Run Checker logic
    try:
        # Retrieve perception data: directly reuse upstream perception if available
        if isinstance(raw_perception_data, dict) and raw_perception_data.get("screenshot_b64"):
            screenshot_b64 = raw_perception_data["screenshot_b64"]
            width = raw_perception_data.get("width", 1080)
            height = raw_perception_data.get("height", 2400)
            fused_xml = latest_ui_hierarchy or raw_perception_data.get("xml_hierarchy") or []
        elif (
            hasattr(ctx, "latest_perception_data")
            and isinstance(ctx.latest_perception_data, dict)
            and ctx.latest_perception_data
        ):
            screenshot_b64 = ctx.latest_perception_data.get("screenshot_b64")
            width = ctx.latest_perception_data.get("width", 1080)
            height = ctx.latest_perception_data.get("height", 2400)
            fused_xml = ctx.latest_perception_data.get("latest_ui_hierarchy") or []
        else:
            # Fallback to direct controller query
            controller = UnifiedMobileController(ctx)
            device_data = await controller.get_screen_data()
            screenshot_b64 = device_data.base64
            xml_hierarchy = device_data.elements
            width = device_data.width
            height = device_data.height

            ocr_results = []
            if is_ocr_configured():
                try:
                    status_bar_height = _detect_status_bar_height(xml_hierarchy, height)
                    if status_bar_height > 0:
                        cropped_b64, _, _ = _crop_image_remove_status_bar(
                            screenshot_b64, status_bar_height
                        )
                        raw_ocr_results = await perform_ocr(cropped_b64)
                        ocr_results = _map_coordinates_back(raw_ocr_results, status_bar_height)
                    else:
                        ocr_results = await perform_ocr(screenshot_b64)
                except Exception as e:
                    logger.warning(f"Checker OCR failed, proceeding without OCR: {e}")

            fused_xml = fuse_ocr_with_xml(xml_hierarchy, ocr_results)

        minimal_list, elements, labels = format_minimal_list_with_elements(fused_xml, width, height)

        # Build task tree
        task_plan = "No task plan yet."
        task_plan_path = get_note_file_path(ctx.data_engine.base_dir, "task_plan")
        if task_plan_path.exists():
            try:
                task_plan = task_plan_path.read_text(encoding="utf-8")
            except Exception as e:
                logger.error(f"Failed to read task plan for Checker: {e}")

        steps = ctx.data_engine.get_agent_friendly_steps() if ctx.data_engine else []

        # Calculate exact subgoal hash from subgoal_text directly to avoid 'default' placeholder
        if not subgoal_hash or subgoal_hash == "default":
            clean_subgoal = safe_extract_text(subgoal_text).strip()
            subgoal_hash = hashlib.md5(clean_subgoal.encode("utf-8")).hexdigest()

        keep_hashes = get_recent_subgoal_hashes(steps, subgoal_hash, ctx.data_engine.base_dir)

        plan_and_history = build_plan_and_history(
            task_plan, steps, subgoal_hash, keep_subgoal_hashes=keep_hashes
        )

        chat_path = get_verification_chat_path(ctx.data_engine.base_dir, subgoal_hash)
        turns = read_verification_chat(chat_path)

        # Calculate round number
        max_op, max_chk = get_verification_chat_rounds(turns)
        round_num = max(max_op, max_chk + 1)
        if round_num == 0:
            round_num = 1

        # Render dialogue history for prompt
        dialogue_lines = []
        for t in turns:
            role = "Operator" if t["role"] == "operator" else "Checker"
            dialogue_lines.append(f"**{role} (Round {t['round']})**:\n{t['content']}")
        dialogue_history = (
            "\n\n".join(dialogue_lines) if dialogue_lines else "No previous dialogue."
        )

        prompts = {}
        prompts_path = Path(__file__).parent / "checker.json"
        if prompts_path.exists():
            try:
                prompts = json.loads(prompts_path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.error(f"Failed to load checker prompts: {e}")

        prompt_template = prompts.get(
            "checker_prompt",
            "You are the Checker. Your job is to verify if the current subtask"
            " was successfully completed.",
        )

        full_prompt = Template(prompt_template).render(
            subgoal=subgoal_text,
            plan_and_history=plan_and_history,
            dialogue_history=dialogue_history if dialogue_history else "No previous dialogue.",
            minimal_list=minimal_list,
        )

        llm = get_llm(ctx=ctx, name="checker")
        read_note = get_read_note_tool_pure(ctx)

        @tool
        def submit_verification(
            success: bool,
            reason: str,
        ) -> str:
            """[TERMINAL] Submit the final verification outcome (success or failure with reason). This completes verification."""
            return f"Verification decision: success={success}, reason={reason}"

        tools_to_bind = [
            trace_langchain_tool(read_note, ctx),
            submit_verification,
        ]

        content = [
            {"type": "text", "text": full_prompt},
            {"type": "text", "text": "--- Current Screenshot ---"},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}"},
            },
        ]

        messages = [
            SystemMessage(
                content=prompts.get(
                    "checker_system",
                    "You are the Checker agent. Your objective is to accurately"
                    " assess whether the active subgoal was successfully"
                    " completed.",
                )
            ),
            HumanMessage(content=content),
        ]

        max_iterations = (
            getattr(
                ctx.execution_setup,
                "checker_max_iterations",
                CHECKER_MAX_ITERATIONS,
            )
            if ctx.execution_setup
            else CHECKER_MAX_ITERATIONS
        )

        success = True
        reason = "Checker completed."

        for i in range(max_iterations):
            is_final_round = i == max_iterations - 1

            if is_final_round:
                messages.append(
                    HumanMessage(
                        content=(
                            "This is your final iteration; all tools are"
                            " stripped, and you must provide your final structured verification result."
                        )
                    )
                )
                structured_llm = llm.with_structured_output(CheckerResult)
                structured_res: CheckerResult = await invoke_llm_with_timeout_message(
                    structured_llm.ainvoke(messages)
                )
                if isinstance(structured_res, CheckerResult):
                    success = structured_res.success
                    reason = structured_res.reason
                elif isinstance(structured_res, dict):
                    success = structured_res.get("success", True)
                    reason = structured_res.get("reason", "Verification completed.")
                break
            else:
                if i > 0:
                    messages.append(
                        HumanMessage(
                            content=(
                                "You have not completed verification yet"
                                f" (iteration {i + 1} of {max_iterations})."
                            )
                        )
                    )
                active_llm = llm.bind_tools(tools=tools_to_bind)

                async def run_stream():
                    full_response = None
                    async for chunk in active_llm.astream(messages):
                        if full_response is None:
                            full_response = chunk
                        else:
                            full_response += chunk
                    return full_response

                response = await invoke_llm_with_timeout_message(run_stream())

                # Check if submit_verification was invoked
                submit_tc = next(
                    (
                        tc
                        for tc in response.tool_calls
                        if tc["name"].endswith("submit_verification")
                    ),
                    None,
                )
                if submit_tc:
                    tc_args = dict(submit_tc["args"])
                    success = bool(tc_args.get("success", True))
                    reason = str(tc_args.get("reason", "Verification submitted."))
                    break

                if not response.tool_calls:
                    # Model answered in plain text; use structured_llm directly
                    structured_llm = llm.with_structured_output(CheckerResult)
                    structured_res: CheckerResult = await invoke_llm_with_timeout_message(
                        structured_llm.ainvoke(messages + [response])
                    )
                    if isinstance(structured_res, CheckerResult):
                        success = structured_res.success
                        reason = structured_res.reason
                    elif isinstance(structured_res, dict):
                        success = structured_res.get("success", True)
                        reason = structured_res.get("reason", "Verification completed.")
                    break

                messages.append(response)

                async def run_tool(tc):
                    tool_name = tc["name"].split(":")[-1] if ":" in tc["name"] else tc["name"]
                    logger.info(f"Checker requested tool: {tool_name}")

                    result_str = ""
                    tool_status = "success"
                    try:
                        if ":" in tool_name:
                            tool_to_run = get_tool_by_name(tool_name, tools_to_bind)
                        else:
                            tool_to_run = next(
                                (t for t in tools_to_bind if t.name == tool_name),
                                None,
                            )
                        if tool_to_run:
                            args = dict(tc["args"])
                            with TraceSpan(name=tool_name, ctx=ctx) as span:
                                span.payload = {"args": args}
                                try:
                                    result_obj = await invoke_tool_with_injection(
                                        tool=tool_to_run,
                                        args=args,
                                        tool_call_id=tc["id"],
                                    )
                                    result_str = get_tool_result_content(result_obj)
                                    span.result = result_str
                                    if result_str.startswith("Error"):
                                        tool_status = "error"
                                        span.status = "failed"
                                        span.error = result_str
                                except Exception as err:
                                    tool_status = "error"
                                    span.status = "failed"
                                    span.error = str(err)
                                    raise err
                        else:
                            result_str = f"Error: Tool {tool_name} not supported"
                            tool_status = "error"
                    except Exception as err:
                        logger.error(f"Error running tool {tool_name}: {err}")
                        result_str = f"Error running tool {tool_name}: {err}"
                        tool_status = "error"

                    return ToolMessage(
                        tool_call_id=tc["id"],
                        content=result_str,
                        status=tool_status,
                    )

                tool_outputs = await asyncio.gather(*(run_tool(tc) for tc in response.tool_calls))
                for tm in tool_outputs:
                    messages.append(tm)

        logger.info(
            f"Async Checker result for '{subgoal_text}': Success={success}, Reason={reason}"
        )

        # Update status file
        status_path.write_text(
            json.dumps({"status": "success" if success else "failed", "reason": reason}),
            encoding="utf-8",
        )

        # Clean up dialogue file if successful
        if success:
            if chat_path.exists():
                logger.info(f"Checker succeeded. Cleaning up dialogue file: {chat_path}")
                chat_path.unlink()

        # If failed, revert status in task_plan.md and write feedback
        if not success:
            task_plan_path = get_note_file_path(ctx.data_engine.base_dir, "task_plan")
            if task_plan_path.exists():
                task_plan = task_plan_path.read_text(encoding="utf-8")
                lines = task_plan.split("\n")

                for i in range(len(lines) - 1, -1, -1):
                    if lines[i].startswith("- ["):
                        task_content = safe_extract_text(lines[i][5:]).strip()
                        safe_subgoal = safe_extract_text(subgoal_text).strip()
                        if task_content == safe_subgoal:
                            lines[i] = re.sub(r"^(- \[)[x/ ](\])", r"\g<1>/\g<2>", lines[i])
                            logger.info(
                                "Matched top-level subgoal by content."
                                f" Reverted status to [/]: {subgoal_text}"
                            )
                            break
                task_plan_path.write_text("\n".join(lines), encoding="utf-8")

            append_verification_chat(
                chat_path,
                "checker",
                f"Status: Failed.\nReason: {reason}",
                round_num,
            )

        return {"status": "success" if success else "failed", "reason": reason}

    except Exception as e:
        logger.error(f"Async Checker encountered exception (fail-open): {e}")
        status_path.write_text(
            json.dumps(
                {
                    "status": "success",
                    "reason": f"Checker Fail-Open: {e}",
                }
            ),
            encoding="utf-8",
        )
        return {"status": "success", "reason": f"Checker Fail-Open: {e}"}
