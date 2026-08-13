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

"""Asynchronous Visual Step Summarizer for Artemis Flash profile.

Processes Before/After screenshot pairs and action metadata in the background
to generate high-density, strictly objective visual state transition summaries.
Ensures zero blocking latency for the main runner and includes out-of-order self-healing retries.
"""

import asyncio
import base64
from pathlib import Path
from typing import Any

from jinja2 import Template
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from artemis.context import ArtemisContext
from artemis.services.llm import get_google_llm, get_llm
from artemis.utils.logger import get_logger
from artemis.utils.visualization import draw_action_overlay_on_image

logger = get_logger(__name__)


class VisualStepSummarizer:
    """Non-blocking background worker generating objective visual step summaries.

    Key design properties:
    1. Zero-blocking dispatch: The main Flash runner dispatches and proceeds immediately.
    2. Graceful fallback: If a step summary is not ready, it cleanly falls back to the original action text.
    3. Self-healing re-trigger: If a previous step's summary is stalled or lost while subsequent steps
       complete, it automatically re-dispatches the stalled step.
    """

    def __init__(self, ctx: ArtemisContext, model_name: str | None = None):
        self.ctx = ctx
        self._summaries: dict[int, str] = {}
        self._pending_tasks: dict[int, asyncio.Task] = {}
        self._step_inputs: dict[int, dict[str, Any]] = {}
        self._retry_counts: dict[int, int] = {}
        self._max_retries = 2

        # Initialize lightweight VLM: prioritize explicit model_name
        target_model = model_name or "gemini-2.5-flash-lite"
        try:
            if model_name:
                self._llm = get_google_llm(model_name=target_model, temperature=0.0)
            else:
                self._llm = get_llm(ctx, name="summarizer", is_utils=True)
        except Exception:
            self._llm = get_google_llm(model_name=target_model, temperature=0.0)

        # Load system prompt template
        prompt_path = Path(__file__).parent / "flash_summarizer.md"
        if prompt_path.exists():
            self._prompt_template = prompt_path.read_text(encoding="utf-8")
        else:
            self._prompt_template = (
                "You are the Step Summarizer for an Android UI automation agent.\n"
                "Synthesize the physical action and visual delta between BEFORE and AFTER screens in exactly ONE "
                "continuous first-person paragraph using 'I' (e.g., 'In Step {{ step_number }}, I tapped... and observed...').\n"
                "Strictly avoid subjective validation words: successfully, completed, failed, achieved, navigated to."
            )

    def dispatch(
        self,
        step_number: int,
        action_name: str,
        action_args: dict[str, Any],
        pre_img_bytes: bytes | None,
        post_img_bytes: bytes | None,
        exec_outcome: str,
    ) -> None:
        """Dispatches an asynchronous summarization task without blocking the caller."""
        self._step_inputs[step_number] = {
            "action_name": action_name,
            "action_args": action_args,
            "pre_img_bytes": pre_img_bytes,
            "post_img_bytes": post_img_bytes,
            "exec_outcome": exec_outcome,
        }

        task = asyncio.create_task(self._run_summary(step_number))
        self._pending_tasks[step_number] = task

        # Check for and re-trigger any previous stalled steps
        self._check_and_retrigger_stalled_steps(current_step=step_number)

    def _check_and_retrigger_stalled_steps(self, current_step: int) -> None:
        """Re-dispatches any previous steps that remain unsummarized while later steps arrive."""
        for past_step in range(1, current_step):
            if past_step not in self._summaries and past_step in self._step_inputs:
                task = self._pending_tasks.get(past_step)
                retries = self._retry_counts.get(past_step, 0)
                if (task is None or task.done()) and retries < self._max_retries:
                    self._retry_counts[past_step] = retries + 1
                    logger.info(
                        f"VisualStepSummarizer: Retriggering stalled summary for Step {past_step} "
                        f"(attempt {self._retry_counts[past_step] + 1})"
                    )
                    self._pending_tasks[past_step] = asyncio.create_task(
                        self._run_summary(past_step)
                    )

    async def _run_summary(self, step_number: int) -> None:
        """Executes the lightweight VLM call to summarize the step transition."""
        input_data = self._step_inputs.get(step_number)
        if not input_data:
            return

        action_name = input_data["action_name"]
        action_args = input_data["action_args"]
        pre_bytes = input_data["pre_img_bytes"]
        post_bytes = input_data["post_img_bytes"]
        exec_outcome = input_data["exec_outcome"]

        try:
            rendered_prompt = Template(self._prompt_template).render(step_number=step_number)
            content_blocks: list[dict[str, Any]] = [
                {
                    "type": "text",
                    "text": (
                        f"Step {step_number} Physical Action: {action_name}({action_args})\n"
                        f"Controller Outcome: {exec_outcome}"
                    ),
                }
            ]

            if pre_bytes:
                # 🎨 Visually mark the exact action (tap ripple, sequence numbers, swipe arrow) on the BEFORE screenshot
                annotated_pre_bytes = draw_action_overlay_on_image(
                    image_bytes=pre_bytes,
                    action_name=action_name,
                    action_args=action_args,
                )
                b64_pre = base64.b64encode(annotated_pre_bytes).decode("utf-8")
                content_blocks.append(
                    {
                        "type": "text",
                        "text": "--- [1] BEFORE ACTION SCREEN (Action Marked Visually in Red) ---",
                    }
                )
                content_blocks.append(
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_pre}"}}
                )

            if post_bytes:
                b64_post = base64.b64encode(post_bytes).decode("utf-8")
                content_blocks.append({"type": "text", "text": "--- [2] AFTER ACTION SCREEN ---"})
                content_blocks.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64_post}"},
                    }
                )

            messages: list[BaseMessage] = [
                SystemMessage(content=rendered_prompt),
                HumanMessage(content=content_blocks),
            ]

            response = await asyncio.wait_for(self._llm.ainvoke(messages), timeout=25.0)
            summary_raw = response.content if isinstance(response.content, str) else ""
            if isinstance(response.content, list):
                summary_raw = "".join(
                    b.get("text", "")
                    for b in response.content
                    if isinstance(b, dict) and "text" in b
                )

            summary_text = summary_raw.strip()
            if summary_text:
                self._summaries[step_number] = summary_text
                logger.info(
                    f"VisualStepSummarizer: Generated summary for Step {step_number}: {summary_text[:80]}..."
                )

                # Free binary image buffers from memory once summary is secured
                if step_number in self._step_inputs:
                    self._step_inputs[step_number]["pre_img_bytes"] = None
                    self._step_inputs[step_number]["post_img_bytes"] = None

                # Update DataEngine telemetry if active
                if self.ctx.data_engine:
                    try:
                        self.ctx.data_engine.update_step_summary(step_number, summary_text)
                    except Exception as de_err:
                        logger.debug(f"DataEngine step summary update skipped: {de_err}")

        except Exception as e:
            logger.warning(
                f"VisualStepSummarizer: Error generating summary for step {step_number}: {e}"
            )
            if self._retry_counts.get(step_number, 0) >= self._max_retries:
                # Max retries reached, free memory buffer
                if step_number in self._step_inputs:
                    self._step_inputs[step_number]["pre_img_bytes"] = None
                    self._step_inputs[step_number]["post_img_bytes"] = None

    def get_summary(self, step_number: int, fallback_text: str | None = None) -> str | None:
        """Retrieves summarized state for step_number, or fallback_text if not ready yet."""
        return self._summaries.get(step_number, fallback_text)

    def has_summary(self, step_number: int) -> bool:
        """Checks if a step summary has completed."""
        return step_number in self._summaries

    async def flush(self) -> None:
        """Flushes and awaits all pending background summary tasks upon completion."""
        tasks = [t for t in self._pending_tasks.values() if not t.done()]
        if tasks:
            logger.info(f"VisualStepSummarizer: Flushing {len(tasks)} pending summary tasks...")
            await asyncio.gather(*tasks, return_exceptions=True)
