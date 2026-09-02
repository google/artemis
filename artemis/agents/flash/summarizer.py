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

Scheduling (zero-blocking dispatch, bounded retry, bounded flush, step_id
keying with tool_call_id aliases) lives in the shared
:class:`artemis.memory.step_memory.StepMemoryService`; this class contributes
only the visual-transition lens: red action-marker overlay on the BEFORE
frame, the ``flash_summarizer.md`` neutral-wording contract, and versioned
``summary_status`` writes to the DataEngine.
"""

import asyncio
import base64
from pathlib import Path
from typing import Any
from uuid import UUID

from jinja2 import Template
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from artemis.context import ArtemisContext
from artemis.memory.step_memory import JobKey, StepMemoryService
from artemis.services.llm import RobustChatModelWrapper, get_google_llm, get_llm
from artemis.services.token_meter import record_llm_usage
from artemis.utils.logger import get_logger
from artemis.utils.visualization import draw_action_overlay_on_image

logger = get_logger(__name__)

# Degenerate-output guard (§5 echo validation): a summary that echoes the
# input's section markers, or falls outside sane length bounds, fails the
# attempt so the bounded service retry regenerates it.
SUMMARY_ECHO_MARKER = "--- ["
SUMMARY_MIN_CHARS = 15
SUMMARY_MAX_CHARS = 1500


def degenerate_summary_reason(text: str) -> str | None:
    """Why a lens output is unusable (echo/length), or None when it is fine."""
    if SUMMARY_ECHO_MARKER in text or text.startswith("---"):
        return "echoes an input section marker"
    if len(text) < SUMMARY_MIN_CHARS:
        return f"too short ({len(text)} chars < {SUMMARY_MIN_CHARS})"
    if len(text) > SUMMARY_MAX_CHARS:
        return f"too long ({len(text)} chars > {SUMMARY_MAX_CHARS})"
    return None


class VisualStepSummarizer(StepMemoryService):
    """Visual-transition lens on top of the shared step-memory runtime.

    Key design properties (inherited from StepMemoryService):
    1. Zero-blocking dispatch: The main Flash runner dispatches and proceeds immediately.
    2. Lossless pending state: The compressor retains the source image until its summary is ready.
    3. Independent bounded retry: Every action owns a retry loop capped at 1 + retry_limit attempts.
    """

    def __init__(
        self,
        ctx: ArtemisContext,
        model_name: str | None = None,
        retry_limit: int = 3,
        *,
        max_concurrency: int = 1,
        flush_timeout_s: float = 30.0,
    ):
        super().__init__(
            ctx,
            max_concurrency=max_concurrency,
            retry_limit=retry_limit,
            flush_timeout_s=flush_timeout_s,
        )

        # Initialize lightweight VLM: prioritize explicit model_name
        target_model = model_name or "gemini-2.5-flash-lite"
        self._model_name = target_model
        try:
            if model_name:
                self._llm = get_google_llm(model_name=target_model, temperature=0.0)
            else:
                self._llm = get_llm(ctx, name="summarizer", is_utils=True)
        except Exception:
            self._llm = get_google_llm(model_name=target_model, temperature=0.0)
        try:
            configured = getattr(self._llm, "model", None) or getattr(
                self._llm, "model_name", None
            )
            if isinstance(configured, str) and configured:
                self._model_name = configured
        except Exception:
            pass

        # Load system prompt templates: the dual-frame transition prompt and
        # the single-frame variant (§5 revision: describe whichever frames
        # exist — a Pro step record never carries an independent after-frame,
        # by design, and must not be prompted as if one were expected).
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
        single_path = Path(__file__).parent / "flash_summarizer_single.md"
        if single_path.exists():
            self._single_prompt_template = single_path.read_text(encoding="utf-8")
        else:
            self._single_prompt_template = (
                "You are the Step Summarizer for an Android UI automation agent.\n"
                "Exactly ONE screenshot (the decision frame) is available; there is NO after-action screenshot —"
                " that only means no independent post-action evidence exists.\n"
                "Describe strictly what THIS screen shows and where the action landed (red marker), in exactly ONE"
                " continuous first-person paragraph using 'I' for Step {{ step_number }}. Never describe or guess"
                " the post-action state.\n"
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
        """Dispatches an asynchronous summarization task without blocking the caller.

        Jobs are keyed by the DataEngine step id when one is available; the
        tool_call_id (``action_key``) is retained as an alias so the message
        compressor can keep querying by it. Callers with neither provide the
        step ordinal, matching the legacy keying.
        """
        key: JobKey
        aliases: tuple[JobKey, ...] = ()
        if data_engine_step_id is not None:
            key = str(data_engine_step_id)
            if action_key is not None:
                aliases = (action_key,)
        else:
            key = action_key if action_key is not None else step_number

        payload = {
            "step_number": step_number,
            "action_name": action_name,
            "action_args": action_args,
            "pre_img_bytes": pre_img_bytes,
            "post_img_bytes": post_img_bytes,
            "exec_outcome": exec_outcome,
            "data_engine_step_id": data_engine_step_id,
        }
        self.submit(key, payload, aliases=aliases)

    def _meter_lens_call(self, response: Any) -> None:
        """Meter one raw-model lens call as an ``llm_usage`` trace, best-effort.

        Gateway-wrapped models already meter at the wrapper exit; only the raw
        ``get_google_llm`` bypass needs explicit metering here. Lens prompts
        are tiny and must not overwrite the session's ``last_prompt_tokens``
        (the compaction thresholds' live context base), hence
        ``update_last_prompt=False``.
        """
        if isinstance(self._llm, RobustChatModelWrapper):
            return
        engine = getattr(self.ctx, "data_engine", None) if self.ctx else None
        record_llm_usage(
            engine,
            response,
            source=f"lens:visual_transition:{self._model_name}",
            update_last_prompt=False,
        )

    def _on_status(self, key: JobKey, status: str) -> None:
        """Best-effort DataEngine summary-status write (pending/failed)."""
        if not self.ctx.data_engine:
            return
        payload = self._step_inputs.get(key) or {}
        target_step = payload.get("data_engine_step_id")
        if not target_step:
            return
        try:
            self.ctx.data_engine.update_step_summary(
                target_step,
                None,
                status=status,
                source="visual_transition",
                model=self._model_name,
            )
        except Exception as de_err:
            logger.debug(f"DataEngine summary status update skipped: {de_err}")

    async def _attempt(self, key: JobKey) -> bool:
        """Execute one lightweight VLM attempt for a step transition."""
        input_data = self._step_inputs.get(key)
        if not input_data:
            return False

        step_number = input_data["step_number"]
        action_name = input_data["action_name"]
        action_args = input_data["action_args"]
        pre_bytes = input_data["pre_img_bytes"]
        post_bytes = input_data["post_img_bytes"]
        exec_outcome = input_data["exec_outcome"]

        try:
            # §5 revision: the lens input adapts to whichever frames exist.
            # Both frames -> the classic BEFORE/AFTER transition prompt; a
            # single frame (Pro steps never carry an independent after-frame,
            # by design) -> the single-frame variant, which has no AFTER
            # ACTION section and forbids synthesizing a transition.
            dual = bool(pre_bytes) and bool(post_bytes)
            template = self._prompt_template if dual else self._single_prompt_template
            rendered_prompt = Template(template).render(step_number=step_number)
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
                        "text": (
                            "--- [1] BEFORE ACTION SCREEN (Action Marked Visually in Red) ---"
                            if dual
                            else "--- [1] DECISION FRAME: SCREEN AT ACTION TIME"
                            " (Action Marked Visually in Red) ---"
                        ),
                    }
                )
                content_blocks.append(
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_pre}"}}
                )

            if post_bytes:
                b64_post = base64.b64encode(post_bytes).decode("utf-8")
                content_blocks.append(
                    {
                        "type": "text",
                        "text": (
                            "--- [2] AFTER ACTION SCREEN ---"
                            if dual
                            else "--- [1] SCREEN OBSERVED AFTER THE ACTION"
                            " (no decision frame available) ---"
                        ),
                    }
                )
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
            self._meter_lens_call(response)
            summary_raw = response.content if isinstance(response.content, str) else ""
            if isinstance(response.content, list):
                summary_raw = "".join(
                    b.get("text", "")
                    for b in response.content
                    if isinstance(b, dict) and "text" in b
                )

            summary_text = summary_raw.strip()
            degenerate = degenerate_summary_reason(summary_text) if summary_text else None
            if degenerate:
                logger.warning(
                    f"VisualStepSummarizer: Discarding degenerate summary for"
                    f" Step {step_number} ({degenerate}): {summary_text[:80]!r}"
                )
                return False
            if summary_text:
                self._summaries[key] = summary_text
                logger.info(
                    f"VisualStepSummarizer: Generated summary for Step {step_number}: {summary_text[:80]}..."
                )

                # Free binary image buffers from memory once summary is secured
                if key in self._step_inputs:
                    self._step_inputs[key]["pre_img_bytes"] = None
                    self._step_inputs[key]["post_img_bytes"] = None

                # Update DataEngine telemetry if active
                if self.ctx.data_engine:
                    try:
                        target_step = input_data.get("data_engine_step_id") or step_number
                        self.ctx.data_engine.update_step_summary(
                            target_step,
                            summary_text,
                            status="ready",
                            source="visual_transition",
                            model=self._model_name,
                        )
                    except Exception as de_err:
                        logger.debug(f"DataEngine step summary update skipped: {de_err}")
                return True

        except Exception as e:
            logger.warning(
                f"VisualStepSummarizer: Error generating summary for step {step_number}: {e}"
            )
        return False
