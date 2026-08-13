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
from uuid import UUID

from langchain_core.messages import (
    HumanMessage,
    SystemMessage,
)

from artemis.context import ArtemisContext
from artemis.data_engine.trace import CURRENT_TRACE_ID, trace
from artemis.graph.state import State
from artemis.services.llm import get_llm
from artemis.utils.coordinates import normalize_any_structure
from artemis.utils.logger import get_logger
from artemis.utils.task_tree import (
    build_plan_and_history,
    get_active_subgoal_hashes,
)

logger = get_logger(__name__)


class SummarizerNode:
    def __init__(self, ctx: ArtemisContext):
        self.ctx = ctx

    @trace(type="agent", name="summarizer")
    async def __call__(self, state: State):
        # 1. Trigger background task for summary and data engine update
        if self.ctx.data_engine and state.current_step_id:
            step_id = UUID(state.current_step_id)
            decisions = state.structured_decisions
            operator_raw_thinking = getattr(state, "operator_raw_thinking", None)
            operator_native_thinking = getattr(state, "operator_native_thinking", None)
            last_execution_result = getattr(state, "last_execution_result", None)

            # Spawn background task
            trace_id = CURRENT_TRACE_ID.get()
            task = asyncio.create_task(
                self._generate_summary_and_update_engine(
                    step_id,
                    decisions,
                    operator_raw_thinking,
                    operator_native_thinking,
                    last_execution_result,
                    trace_id,
                ),
                name=f"summarizer_step_{step_id}",
            )
            if hasattr(self.ctx, "background_tasks") and isinstance(
                self.ctx.background_tasks, list
            ):
                self.ctx.background_tasks.append(task)

        return {}

    async def _generate_summary_and_update_engine(
        self,
        step_id,
        decisions,
        operator_raw_thinking,
        operator_native_thinking,
        last_execution_result,
        trace_id,
    ):
        """Background task to generate summary and update Data Engine with retries."""

        max_retries = 3
        retry_delay = 2

        for attempt in range(max_retries):
            try:
                logger.info(f"Generating summary for step {step_id} (attempt {attempt + 1})")

                # Prepare prompt for summary
                llm = get_llm(ctx=self.ctx, name="summarizer")

                prompt_path = Path(__file__).parent.joinpath("summarizer.md")
                if not prompt_path.exists():
                    prompt = (
                        "You are a summarizer. Summarize the latest action and"
                        " its result in 1-2 concise sentences. Focus on: Action"
                        " taken -> Result -> Observation."
                    )
                else:
                    prompt = prompt_path.read_text(encoding="utf-8")

                # Normalize coordinates before presenting to the summarizer LLM
                width = (
                    getattr(self.ctx.device, "device_width", 1080)
                    if self.ctx and self.ctx.device
                    else 1080
                )
                height = (
                    getattr(self.ctx.device, "device_height", 2400)
                    if self.ctx and self.ctx.device
                    else 2400
                )

                content = []

                # Consistently use optimal build_plan_and_history to construct accurate context
                plan_and_history_str = ""
                if self.ctx.data_engine:
                    try:
                        friendly_steps = self.ctx.data_engine.get_agent_friendly_steps()
                        if isinstance(friendly_steps, list):
                            # 1. Trim history to only include steps up to the current step being summarized (inclusive)
                            current_steps = []
                            for s in friendly_steps:
                                current_steps.append(s)
                                if str(s.get("step_id")) == str(step_id):
                                    break

                            # 2. Read task plan to provide goal orientation to the summarizer
                            task_plan = ""
                            base_dir = getattr(self.ctx.data_engine, "base_dir", None)
                            if base_dir and isinstance(base_dir, (str, Path)):
                                notes_dir = Path(base_dir) / "notes"
                                task_plan_path = notes_dir / "task_plan.md"
                                if task_plan_path.exists():
                                    task_plan = task_plan_path.read_text(encoding="utf-8")

                            # 3. Unified rendering: concise for first N steps (with self-healing facts), detailed for current step (without redundant summary header)

                            subgoal_hash = "default"
                            if task_plan:
                                subgoal_hash, _ = get_active_subgoal_hashes(task_plan)

                            plan_and_history_str = build_plan_and_history(
                                task_plan=task_plan,
                                steps=current_steps,
                                current_subgoal_hash=subgoal_hash,
                                last_n_detailed=1,
                                min_summaries=10,
                                strict_milestone_pruning=False,
                            )
                    except Exception as e:
                        logger.error(f"Failed to build plan and history for Summarizer: {e}")

                if plan_and_history_str:
                    content.append(plan_and_history_str)
                else:
                    # Fallback to standard raw fields if chronological trace couldn't be generated
                    if decisions:
                        try:
                            dec_obj = json.loads(decisions)
                            dec_obj = normalize_any_structure(dec_obj, width, height)
                            content.append(
                                f"Decisions made: {json.dumps(dec_obj, ensure_ascii=False)}"
                            )
                        except Exception:
                            content.append(f"Decisions made: {decisions}")

                    if operator_raw_thinking:
                        content.append(f"Operator explicit thoughts:\n{operator_raw_thinking}")

                    if operator_native_thinking:
                        content.append(f"Operator native thoughts:\n{operator_native_thinking}")

                messages = [
                    SystemMessage(content=prompt),
                    HumanMessage(content="\n\n".join(content)),
                ]

                generated_text = ""
                aggregated_chunk = None
                async for chunk in llm.astream(messages):
                    if aggregated_chunk is None:
                        aggregated_chunk = chunk
                    else:
                        aggregated_chunk += chunk

                    if chunk.content:
                        text_to_stream = ""
                        if isinstance(chunk.content, str):
                            text_to_stream = chunk.content
                        elif isinstance(chunk.content, list):
                            for item in chunk.content:
                                if isinstance(item, dict) and item.get("type") == "text":
                                    text_to_stream += item.get("text", "")

                        if text_to_stream:
                            generated_text += text_to_stream
                    pass

                summary = generated_text.strip()
                logger.info(f"Summary generated: {summary}")

                if aggregated_chunk and getattr(aggregated_chunk, "usage_metadata", None):
                    usage = aggregated_chunk.usage_metadata
                    cached_tokens = usage.get("input_token_details", {}).get("cache_read", 0)
                    logger.info(
                        "Summarizer LLM usage:"
                        f" prompt_tokens={usage.get('input_tokens')}"
                        f" (cached={cached_tokens}),"
                        f" completion_tokens={usage.get('output_tokens')}"
                    )

                # Update Data Engine
                self.ctx.data_engine.update_step_summary(step_id, summary)
                logger.info(f"Data Engine updated with summary for step {step_id}")
                return  # Success

            except Exception as e:
                logger.error(
                    f"Failed to generate summary or update engine (attempt {attempt + 1}): {e}"
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                else:
                    logger.error(
                        f"Max retries reached for step {step_id}. Summary generation failed."
                    )
