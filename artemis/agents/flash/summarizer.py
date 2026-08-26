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
Ensures zero blocking latency for the main runner and independently retries failed jobs.
"""

import asyncio
import base64
from pathlib import Path
from typing import Any
from uuid import UUID

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
    2. Lossless pending state: The compressor retains the source image until its summary is ready.
    3. Independent retry: Every action owns a retry loop and does not depend on later actions arriving.
    """

    def __init__(self, ctx: ArtemisContext, model_name: str | None = None):
        self.ctx = ctx
        self._summaries: dict[int | str, str] = {}
        self._pending_tasks: dict[int | str, asyncio.Task] = {}
        self._step_inputs: dict[int | str, dict[str, Any]] = {}
        self._retry_counts: dict[int | str, int] = {}
        self._retry_delays = (0.0, 0.5, 1.0, 2.0, 3.0)

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
        *,
        action_key: str | None = None,
        data_engine_step_id: UUID | str | None = None,
    ) -> None:
        """Dispatches an asynchronous summarization task without blocking the caller."""
        key: int | str = action_key or step_number
        self._step_inputs[key] = {
            "step_number": step_number,
            "action_name": action_name,
            "action_args": action_args,
            "pre_img_bytes": pre_img_bytes,
            "post_img_bytes": post_img_bytes,
            "exec_outcome": exec_outcome,
            "data_engine_step_id": data_engine_step_id,
        }

        task = asyncio.create_task(self._run_summary_until_ready(key))
        self._pending_tasks[key] = task

    async def _run_summary_until_ready(self, action_key: int | str) -> None:
        """Retry one action independently until a non-empty summary is committed."""
        while action_key not in self._summaries:
            if await self._run_summary_once(action_key):
                return

            retry_count = self._retry_counts.get(action_key, 0) + 1
            self._retry_counts[action_key] = retry_count
            delay = self._retry_delays[min(retry_count - 1, len(self._retry_delays) - 1)]
            input_data = self._step_inputs.get(action_key)
            display_step = input_data.get("step_number") if input_data else action_key
            logger.info(
                f"VisualStepSummarizer: Retrying Step {display_step} "
                f"(attempt {retry_count + 1}) after {delay:.1f}s"
            )
            if delay:
                await asyncio.sleep(delay)

    async def _run_summary_once(self, action_key: int | str) -> bool:
        """Execute one lightweight VLM attempt for a step transition."""
        input_data = self._step_inputs.get(action_key)
        if not input_data:
            return False

        step_number = input_data["step_number"]
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
                self._summaries[action_key] = summary_text
                logger.info(
                    f"VisualStepSummarizer: Generated summary for Step {step_number}: {summary_text[:80]}..."
                )

                # Free binary image buffers from memory once summary is secured
                if action_key in self._step_inputs:
                    self._step_inputs[action_key]["pre_img_bytes"] = None
                    self._step_inputs[action_key]["post_img_bytes"] = None

                # Update DataEngine telemetry if active
                if self.ctx.data_engine:
                    try:
                        target_step = input_data.get("data_engine_step_id") or step_number
                        self.ctx.data_engine.update_step_summary(target_step, summary_text)
                    except Exception as de_err:
                        logger.debug(f"DataEngine step summary update skipped: {de_err}")
                return True

        except Exception as e:
            logger.warning(
                f"VisualStepSummarizer: Error generating summary for step {step_number}: {e}"
            )
        return False

    def get_summary(
        self, action_key: int | str, fallback_text: str | None = None
    ) -> str | None:
        """Retrieve the summary for a stable action key, or fallback text."""
        return self._summaries.get(action_key, fallback_text)

    def has_summary(self, action_key: int | str) -> bool:
        """Check whether an action summary has completed."""
        return action_key in self._summaries

    def has_job(self, action_key: int | str) -> bool:
        """Check whether an action has been submitted for visual summarization."""
        return action_key in self._step_inputs

    def is_pending(self, action_key: int | str) -> bool:
        """Check whether an action still needs a summary."""
        return self.has_job(action_key) and not self.has_summary(action_key)

    async def flush(self, timeout_seconds: float = 30.0) -> None:
        """Wait for active jobs up to a bound, then cancel remaining retry loops."""
        tasks = [t for t in self._pending_tasks.values() if not t.done()]
        if tasks:
            logger.info(f"VisualStepSummarizer: Flushing {len(tasks)} pending summary tasks...")
            _, pending = await asyncio.wait(tasks, timeout=timeout_seconds)
            if pending:
                logger.warning(
                    f"VisualStepSummarizer: Cancelling {len(pending)} unfinished summary tasks "
                    f"after {timeout_seconds:.1f}s flush timeout."
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
