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

"""Universal Multi-Model FlashRunner for Artemis.

Executes autonomous reactive mobile workflows across Google Gemini,
OpenAI GPT-4o/o3, Anthropic Claude 3.5/3.7, and OpenRouter endpoints.
"""

import asyncio
import base64
import json
from pathlib import Path
import re
import uuid

from jinja2 import Template
from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from artemis.agents.flash.context_compressor import compress_flash_messages
from artemis.agents.flash.summarizer import VisualStepSummarizer
from artemis.agents.validator.tool_declarations import (
    ASK_EXPLORER_TOOL,
    CLICK_SEQUENCE_TOOL,
    REPORT_TASK_STATUS_TOOL,
    VALIDATOR_TOOLS_DECLARATION,
    capture_screenshot_and_parse_ui,
    prune_intermediate_screenshots,
)
from artemis.config import StepSummarizerConfig, load_agent_config
from artemis.context import ArtemisContext
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.data_engine.trace import trace
from artemis.graph.perception import _check_injected_instruction_file
from artemis.graph.state import State
from artemis.mcp.action_executor import McpActionExecutor
from artemis.services.llm import (
    RobustChatModelWrapper,
    get_google_llm,
    get_llm,
    invoke_llm_with_timeout_message,
)
from artemis.utils.coordinates import parse_swipe_parameters
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


class FlashRunner:
    """Reactive, ultra-fast agent execution loop supporting all VLM providers."""

    def __init__(self, ctx: ArtemisContext, goal: str, max_turns: int | None = None):
        self.ctx = ctx
        self.goal = goal
        try:
            cfg = load_agent_config()
            self.max_turns = max_turns if max_turns is not None else cfg.flash.max_turns
            self.step_summarizer_cfg = cfg.flash.step_summarizer
        except Exception:
            self.max_turns = max_turns if max_turns is not None else 30
            self.step_summarizer_cfg = StepSummarizerConfig()

        self.controller = UnifiedMobileController(ctx)
        self.executor = McpActionExecutor(ctx, self.controller)
        self.summarizer = (
            VisualStepSummarizer(ctx, model_name=self.step_summarizer_cfg.model)
            if self.step_summarizer_cfg.enabled
            else None
        )

    def _get_tools(self) -> list:
        tools = [t for t in VALIDATOR_TOOLS_DECLARATION if t.name != "report_failure_analysis"]
        tools.insert(1, CLICK_SEQUENCE_TOOL)
        tools.append(ASK_EXPLORER_TOOL)
        tools.append(REPORT_TASK_STATUS_TOOL)
        # With an actuator installed, drop declarations for device actions the
        # backend does not implement (and append its extension tools); without
        # one, the full historical declaration set is kept.
        actuator = getattr(self.ctx, "actuator", None)
        if actuator is not None:
            from artemis.mcp.action_manifest import filter_declarations

            tools = filter_declarations(tools, actuator, "flash")
        return tools

    def _prune_intermediate_screenshots(self, messages: list[BaseMessage]) -> None:
        """Prunes binary screenshot blocks from intermediate observation messages."""
        prune_intermediate_screenshots(messages)

    @trace(type="agent", name="FlashRunner")
    async def run(self, state: State) -> dict:
        logger.info(f"Starting Artemis Flash reactive loop for goal: {self.goal}")

        # 1. Initialize Universal LLM via Service Layer
        try:
            llm = get_llm(self.ctx, name="operator")
        except Exception as e:
            logger.warning(f"Failed to get operator LLM from config, using default: {e}")

            llm = RobustChatModelWrapper(get_google_llm(model_name="gemini-3.7-flash"), self.ctx)

        tools_declaration = self._get_tools()

        # 2. Capture Initial State (Screenshot + UI Tree)
        shot_path, img_bytes, xml_list = await capture_screenshot_and_parse_ui(
            self.ctx, state, self.controller, skip_settling=False
        )
        state.latest_screenshot = shot_path

        user_content: list[dict] = [
            {"type": "text", "text": f"Your objective is: {self.goal}"},
        ]
        if img_bytes:
            img_b64 = base64.b64encode(img_bytes).decode("utf-8")
            user_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                }
            )
        if xml_list:
            user_content.append(
                {
                    "type": "text",
                    "text": f"--- UI Element List ---\n{xml_list}",
                }
            )
        user_content.append(
            {
                "type": "text",
                "text": (
                    "CRITICAL RULE: In every single turn, you MUST FIRST output a natural"
                    " language reasoning/explanation paragraph BEFORE invoking any tool call."
                ),
            }
        )

        # Render System Prompt. Tool-teaching segments are gated on the available
        # tool set so an absent tool leaves no trace in the prompt; until an actuator
        # is wired in, the full manifest set reproduces the historical prompt.
        prompt_path = Path(__file__).parent / "flash_runner.md"
        prompt_template = prompt_path.read_text(encoding="utf-8")
        available_tools = frozenset(t.name for t in tools_declaration)
        system_prompt = Template(prompt_template).render(
            goal=self.goal, available_tools=available_tools
        )

        messages: list[BaseMessage] = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_content),
        ]

        turns = 0
        action_sequence = 0
        final_report = None
        current_pre_screenshot_bytes = img_bytes
        current_xml_list = xml_list

        while turns < self.max_turns:
            turns += 1
            logger.info(f"--- Artemis Flash Turn {turns}/{self.max_turns} ---")

            if self.ctx.data_engine:
                self.ctx.data_engine.allocate_step_id()

            # Check for real-time injected instructions
            if self.ctx.data_engine and self.ctx.data_engine.base_dir:
                try:
                    injected_instruction = await asyncio.to_thread(
                        _check_injected_instruction_file,
                        str(self.ctx.data_engine.base_dir),
                    )
                    if injected_instruction:
                        messages.append(
                            HumanMessage(
                                content=(
                                    "[REAL-TIME INJECTED INSTRUCTION from user]:"
                                    f" {injected_instruction}\nYou MUST immediately follow"
                                    " this instruction and adjust your plan/actions."
                                )
                            )
                        )
                except Exception as e:
                    logger.warning(f"Failed to check injected instruction in FlashRunner: {e}")

            # Compress history even when visual summarization is disabled, so
            # screenshot and historical XML pruning have identical semantics.
            compress_flash_messages(
                messages,
                summarizer=self.summarizer,
                prune_history_xml=self.step_summarizer_cfg.prune_history_xml,
            )

            # Tool restriction on the final turn
            if turns == self.max_turns:
                messages.append(
                    HumanMessage(
                        content=(
                            "[WARNING] This is your final turn; only"
                            " 'report_task_status' is available."
                        )
                    )
                )
                current_tools = [t for t in tools_declaration if t.name == "report_task_status"]
            else:
                current_tools = tools_declaration

            # Bind active tools
            bound_llm = llm.bind_tools(current_tools)

            # Invoke Model with Streaming & Retry
            response = None
            max_retries = 3

            for attempt in range(max_retries):
                try:
                    stream_exec_id = uuid.uuid4()

                    async def run_stream():
                        full_response = None
                        async for chunk in bound_llm.astream(messages):
                            if full_response is None:
                                full_response = chunk
                            else:
                                full_response += chunk

                            # Real-time token streaming to DataEngine for live UI display
                            if (
                                getattr(self, "ctx", None)
                                and getattr(self.ctx, "data_engine", None)
                                and getattr(chunk, "content", None)
                            ):
                                text_to_stream = ""
                                thinking_to_stream = ""
                                if isinstance(chunk.content, str):
                                    text_to_stream = chunk.content
                                elif isinstance(chunk.content, list):
                                    for item in chunk.content:
                                        if isinstance(item, str):
                                            text_to_stream += item
                                        elif isinstance(item, dict):
                                            if item.get("type") == "text":
                                                text_to_stream += item.get("text", "")
                                            elif item.get("type") == "thinking":
                                                thinking_to_stream += item.get("thinking", "")
                                if text_to_stream:
                                    self.ctx.data_engine.stream_output(
                                        stream_exec_id, text_to_stream, is_thinking=False
                                    )
                                if thinking_to_stream:
                                    self.ctx.data_engine.stream_output(
                                        stream_exec_id, thinking_to_stream, is_thinking=True
                                    )
                        return full_response

                    response = await invoke_llm_with_timeout_message(
                        run_stream(), timeout_seconds=10, hard_timeout=180
                    )
                    if response:
                        break
                except Exception as e:
                    logger.warning(f"FlashRunner LLM turn failed on attempt {attempt + 1}: {e}")
                    if attempt == max_retries - 1:
                        raise e
                    await asyncio.sleep(2.0**attempt)

            if response is None:
                break

            messages.append(response)

            # Extract thought and tool calls
            raw_text = response.content if isinstance(response.content, str) else ""
            if isinstance(response.content, list):
                raw_text = "".join(
                    b.get("text", "")
                    for b in response.content
                    if isinstance(b, dict) and "text" in b
                )

            # Extract token usage metadata from response
            step_token_usage = None
            if hasattr(response, "usage_metadata") and response.usage_metadata:
                u = response.usage_metadata
                pr = u.get("input_tokens") or u.get("prompt_tokens") or 0
                co = u.get("output_tokens") or u.get("completion_tokens") or 0
                to = u.get("total_tokens") or (pr + co)
                if to > 0:
                    step_token_usage = {
                        "prompt_tokens": int(pr),
                        "completion_tokens": int(co),
                        "total_tokens": int(to),
                    }
            elif hasattr(response, "response_metadata") and isinstance(
                response.response_metadata, dict
            ):
                u = response.response_metadata.get(
                    "usage_metadata"
                ) or response.response_metadata.get("token_usage")
                if isinstance(u, dict):
                    pr = (
                        u.get("input_tokens")
                        or u.get("prompt_tokens")
                        or u.get("prompt_token_count")
                        or 0
                    )
                    co = (
                        u.get("output_tokens")
                        or u.get("completion_tokens")
                        or u.get("candidates_token_count")
                        or 0
                    )
                    to = u.get("total_tokens") or u.get("total_token_count") or (pr + co)
                    if to > 0:
                        step_token_usage = {
                            "prompt_tokens": int(pr),
                            "completion_tokens": int(co),
                            "total_tokens": int(to),
                        }

            if not step_token_usage or step_token_usage.get("total_tokens", 0) <= 0:
                calc_prompt_chars = 0
                calc_images = 0
                for msg in messages:
                    c = getattr(msg, "content", "")
                    if isinstance(c, str):
                        calc_prompt_chars += len(c)
                    elif isinstance(c, list):
                        for block in c:
                            if isinstance(block, dict):
                                if block.get("type") == "image_url" or "image_url" in block:
                                    calc_images += 1
                                else:
                                    calc_prompt_chars += len(str(block.get("text", "")))
                            else:
                                calc_prompt_chars += len(str(block))
                prompt_tokens = (calc_prompt_chars // 4) + (calc_images * 258)
                completion_tokens = len(raw_text) // 4
                step_token_usage = {
                    "prompt_tokens": max(1, prompt_tokens),
                    "completion_tokens": max(1, completion_tokens),
                    "total_tokens": max(1, prompt_tokens + completion_tokens),
                }

            if self.ctx.data_engine:
                current_step_id = getattr(self.ctx.data_engine, "current_step_id", None)
                self.ctx.data_engine.record_trace(
                    type="llm_call",
                    name="FlashRunner",
                    payload={"token_usage": step_token_usage, "response": raw_text},
                    step_id=current_step_id,
                    status="success",
                )

            tool_calls = response.tool_calls or []

            # Fallback text parsing if tool_calls not parsed natively
            if not tool_calls and "```json" in raw_text:
                try:
                    snippet = raw_text.split("```json")[1].split("```")[0].strip()
                    parsed = json.loads(snippet)
                    if isinstance(parsed, dict) and "name" in parsed:
                        tool_calls = [
                            {
                                "name": parsed["name"],
                                "args": parsed.get("args", {}),
                                "id": str(uuid.uuid4()),
                            }
                        ]
                except Exception:
                    pass

            if not tool_calls:
                logger.info(
                    f"FlashRunner received response without tool calls at turn {turns}:"
                    f" {raw_text[:100]}..."
                )
                if turns == self.max_turns:
                    final_report = {"status": "failed", "explanation": raw_text}
                    break
                messages.append(
                    HumanMessage(
                        content=(
                            "You did not call any tools. Please make progress"
                            " by calling an action tool or 'report_task_status'."
                        )
                    )
                )
                continue

            # Process tool calls
            for tc in tool_calls:
                name = tc["name"].split(":")[-1] if ":" in tc["name"] else tc["name"]
                args = tc.get("args") or {}
                tc_id = tc.get("id") or str(uuid.uuid4())
                logger.info(f"Executing Flash tool: {name}({args})")

                if name == "report_task_status":
                    final_report = args
                    if self.ctx.data_engine:
                        try:
                            if self.ctx.data_engine.current_step_id is None:
                                self.ctx.data_engine.allocate_step_id()
                            self.ctx.data_engine.record_step(
                                pre_screenshot_bytes=current_pre_screenshot_bytes,
                                ui_tree=current_xml_list,
                                action_taken={"action": "report_task_status", "args": args},
                                operator_raw_thinking=raw_text,
                                last_execution_result={
                                    "result": "Task completed with final report."
                                },
                                extra_metadata={"token_usage": step_token_usage},
                            )
                        except Exception as step_err:
                            logger.warning(f"Error recording final step in FlashRunner: {step_err}")
                    messages.append(
                        ToolMessage(
                            tool_call_id=tc_id,
                            name=name,
                            content=json.dumps({"status": "acknowledged"}),
                            status="success",
                        )
                    )
                    if self.summarizer:
                        await self.summarizer.flush()
                    return final_report

                try:
                    exec_result = await self.executor.execute(name, args, tc_id, state)

                    # Dynamic dispatch set: manifest device actions plus any backend
                    # extension tools, so extension steps are recorded like actions.
                    action_names = self.executor.action_tool_names

                    post_img_bytes = exec_result.screenshot_bytes
                    if not post_img_bytes and name in action_names:
                        try:
                            controller = UnifiedMobileController(self.ctx)
                            screen_data = await controller.get_screen_data()
                            post_img_bytes = base64.b64decode(screen_data.base64)
                            if not exec_result.ui_elements_text:
                                exec_result.ui_elements_text = screen_data.elements
                        except Exception as shot_err:
                            logger.warning(
                                f"Failed to capture fallback screenshot in FlashRunner: {shot_err}"
                            )

                    # Record telemetry / step in DataEngine
                    recorded_step_id = None
                    if self.ctx.data_engine and name in action_names:
                        try:
                            if self.ctx.data_engine.current_step_id is None:
                                self.ctx.data_engine.allocate_step_id()

                            # Extract and enrich coordinate metadata
                            norm_coords = None
                            norm_start = None
                            norm_end = None
                            if name == "swipe":
                                kind, target_val, _ = parse_swipe_parameters(args)
                                if (
                                    kind == "coords"
                                    and isinstance(target_val, list)
                                    and len(target_val) == 4
                                ):
                                    norm_coords = target_val
                                    norm_start = target_val[:2]
                                    norm_end = target_val[2:]
                                elif kind == "direction" and isinstance(target_val, str):
                                    g_lower = target_val.lower()
                                    if "up" in g_lower:
                                        norm_coords = [600, 700, 600, 300]
                                    elif "down" in g_lower:
                                        norm_coords = [600, 300, 600, 700]
                                    elif "left" in g_lower:
                                        norm_coords = [750, 500, 250, 500]
                                    elif "right" in g_lower:
                                        norm_coords = [250, 500, 750, 500]
                                    if norm_coords:
                                        norm_start = norm_coords[:2]
                                        norm_end = norm_coords[2:]
                            elif name in ("click", "tap", "long_press", "input_text"):
                                target = args.get("target") or args.get("coordinates")
                                if isinstance(target, (list, tuple)) and len(target) == 2:
                                    norm_coords = list(target)
                                elif isinstance(target, str):
                                    nums = re.findall(r"-?\d+(?:\.\d+)?", target)
                                    if len(nums) == 2:
                                        norm_coords = [int(float(nums[0])), int(float(nums[1]))]

                            action_dict = {
                                "action": name,
                                "coordinates": (
                                    args.get("target")
                                    or args.get("coordinates")
                                    or args.get("sequence")
                                    or norm_coords
                                ),
                                "args": args,
                            }
                            if norm_coords:
                                action_dict["normalized_coordinates"] = norm_coords
                            if norm_start and norm_end:
                                action_dict["normalized_start_coordinates"] = norm_start
                                action_dict["normalized_end_coordinates"] = norm_end

                            recorded_step_id = self.ctx.data_engine.record_step(
                                pre_screenshot_bytes=current_pre_screenshot_bytes,
                                post_screenshot_bytes=post_img_bytes,
                                ui_tree=(exec_result.ui_elements_text or current_xml_list),
                                action_taken=action_dict,
                                operator_raw_thinking=raw_text,
                                last_execution_result={"result": exec_result.text_summary},
                                extra_metadata={"token_usage": step_token_usage},
                            )
                        except Exception as step_err:
                            logger.warning(f"Error recording step in FlashRunner: {step_err}")

                    # ⚡ Non-blocking dispatch of objective visual transition summarizer
                    if self.summarizer and name in action_names:
                        action_sequence += 1
                        self.summarizer.dispatch(
                            step_number=action_sequence,
                            action_name=name,
                            action_args=args,
                            pre_img_bytes=current_pre_screenshot_bytes,
                            post_img_bytes=post_img_bytes,
                            exec_outcome=exec_result.text_summary,
                            action_key=str(tc_id),
                            data_engine_step_id=recorded_step_id,
                        )

                    if post_img_bytes:
                        current_pre_screenshot_bytes = post_img_bytes
                    if exec_result.ui_elements_text:
                        current_xml_list = exec_result.ui_elements_text

                    messages.append(exec_result.to_langchain_tool_message())

                except Exception as e:
                    logger.error(f"Error executing tool {name}: {e}")
                    messages.append(
                        ToolMessage(
                            tool_call_id=tc_id,
                            name=name,
                            content=f"Error executing tool {name}: {e}",
                            status="error",
                        )
                    )

        if self.summarizer:
            await self.summarizer.flush()

        return final_report or {
            "status": "failed",
            "explanation": "Max turns reached without final status report.",
        }
