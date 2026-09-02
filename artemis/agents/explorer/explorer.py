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

"""Explorer agent facade.

This module is the stable entry point (and ``mock.patch`` target namespace)
for the Explorer agent.  The implementation is split across sibling modules:

- ``tool_declarations``: static tool schemas (universal + native).
- ``perception_tools``: the ``exec_*`` perception/vision tool method group.
- ``universal_runner``: the LangChain-based ``_run_universal`` loop.
- ``run_setup``: flash mode and the pre-loop setup phases of ``run``.
- ``native_runner``: the native Gemini reasoning loop of ``run``.

All historical module-level names (``settings``, ``StorageManager``,
``genai``, ``search_ui_func``, ``search_by_coordinates_func``,
``_run_object_detection``, ``draw_dots``, ``perform_ocr``,
``is_ocr_configured``, ``get_llm``, ``UNIVERSAL_EXPLORER_TOOLS``, ...) remain
importable and patchable here; split-out code resolves them through this
module at call time (see ``artemis.agents.explorer._facade``).
"""

import asyncio
import base64
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Literal

import cv2
from google import genai
from google.genai import types
import httpx
from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from artemis.agents.image_processor.image_processor import ImageProcessor
from artemis.agents.object_detector.object_detector import _run_object_detection
from artemis.config import settings
from artemis.constants import SAFETY_SETTINGS_BLOCK_NONE
from artemis.context import ArtemisContext
from artemis.data_engine.storage import StorageManager
from artemis.data_engine.trace import TraceSpan, trace
from artemis.graph.state import State
from artemis.llm.reliability import (
    LLMExhaustedError,
    LLMPermanentError,
    classify_failure,
    retry_policy_for,
)
from artemis.services.llm import _record_llm_event, _record_llm_retry, get_llm

# Import diagnostic functions directly
from artemis.agents.explorer.constants import EXPLORE_DESCRIPTIONS
from artemis.agents.explorer.native_runner import NativeRunnerMixin
from artemis.agents.explorer.perception_tools import PerceptionToolsMixin
from artemis.agents.explorer.run_setup import RunSetupMixin
from artemis.agents.explorer.tool_declarations import (
    NATIVE_EXPLORER_TOOL_DECLARATIONS,
    UNIVERSAL_EXPLORER_TOOLS,
)
from artemis.agents.explorer.universal_runner import UniversalRunnerMixin
from artemis.mcp.xml_search_server import (
    search_by_coordinates as search_by_coordinates_func,
    search_ui as search_ui_func,
)
from artemis.utils.logger import get_logger
from artemis.utils.ocr_api import is_ocr_configured, perform_ocr
from artemis.utils.ocr_xml_fusion import fuse_ocr_with_xml
from artemis.utils.visualization import draw_dots, format_minimal_list_with_points

logger = get_logger(__name__)


async def _generate_content_with_reliability(operation, *, label: str = "Explorer model call"):
    """Run one native google-genai model call under the shared reliability layer.

    Retry decisions are owned by ``classify_failure``/``retry_policy_for``
    (artemis.llm.reliability) instead of a blanket exponential-backoff loop:
    non-retryable categories (auth, bad request) are raised immediately as
    ``LLMPermanentError`` and exhausted retryable categories surface as
    ``LLMExhaustedError``.  Only the retry/error-classification policy is
    centralized here; the google-genai transport itself stays untouched.
    ``operation`` must be a zero-argument callable returning a fresh awaitable
    per attempt.
    """
    attempt = 0
    while True:
        try:
            return await operation()
        except Exception as call_err:
            attempt += 1
            failure = classify_failure(call_err)
            if not failure.retryable:
                logger.error(
                    f"{label} permanently failed [{failure.category.value}]: {call_err}"
                )
                _record_llm_event(
                    "llm_gave_up",
                    {
                        "source": "explorer",
                        "error": str(call_err)[:1000],
                        "category": failure.category.value,
                        "retryable": False,
                    },
                    status="failed",
                )
                raise LLMPermanentError(
                    f"{label} failed [{failure.category.value}]: {call_err}",
                    failure=failure,
                    cause=call_err,
                ) from call_err
            policy = retry_policy_for(failure.category)
            if attempt >= policy.max_attempts:
                logger.error(
                    f"{label} exhausted {attempt} attempt(s)"
                    f" [{failure.category.value}]: {call_err}"
                )
                _record_llm_event(
                    "llm_gave_up",
                    {
                        "source": "explorer",
                        "error": str(call_err)[:1000],
                        "category": failure.category.value,
                        "retryable": True,
                        "attempts": attempt,
                    },
                    status="failed",
                )
                raise LLMExhaustedError(
                    f"{label} exhausted {attempt} attempt(s)"
                    f" [{failure.category.value}]: {call_err}",
                    failure=failure,
                    cause=call_err,
                ) from call_err
            delay = policy.delay_for(attempt)
            logger.warning(
                f"{label} failed [{failure.category.value}] on attempt"
                f" {attempt}/{policy.max_attempts}: {call_err}."
                f" Retrying in {delay:.2f}s..."
            )
            _record_llm_retry(
                str(call_err),
                delay,
                attempt=attempt,
                max_retries=policy.max_attempts,
                source="explorer",
            )
            await asyncio.sleep(delay)


class Explorer(PerceptionToolsMixin, UniversalRunnerMixin, RunSetupMixin, NativeRunnerMixin):
    DENYLIST_TEMPLATE = (
        "\n# TOOL DENYLIST\n- The following tools are denylisted and cannot be used: {tools}\n"
    )
    TOOLS = NATIVE_EXPLORER_TOOL_DECLARATIONS

    def __init__(self, ctx: ArtemisContext):
        self.ctx = ctx
        self.global_label_idx = 1
        self.width = 1080
        self.height = 2400
        self.image_name = None
        self.screenshot_path = None
        self.image_pool = {}
        self.next_img_id = 1
        try:
            agent_cfg = getattr(self.ctx, "agent_config", None)
            denylisted_config = (
                getattr(agent_cfg, "denylisted_tools", {}).get("explorer", []) if agent_cfg else []
            )
            self.denylisted_tools = set(denylisted_config)
        except (TypeError, AttributeError):
            self.denylisted_tools = set()
        self.http_client = None
        self.turn_latencies = []
        self.turn_cached_tokens = []
        self.trace_history = []
        self._init_engine()

    def _init_engine(self) -> None:
        """Initializes model engine and decides whether to use native Gemini or Universal path."""
        ctx = self.ctx
        llm_config = getattr(ctx, "llm_config", None)
        llm_cfg = getattr(llm_config, "explorer", None) if llm_config else None
        model_str = (
            llm_cfg.model if (llm_cfg and hasattr(llm_cfg, "model")) else "gemini-3.7-flash"
        ).lower()
        self.model_name = (
            llm_cfg.model if (llm_cfg and hasattr(llm_cfg, "model")) else "gemini-3.7-flash"
        )
        if "/" in self.model_name:
            self.model_name = self.model_name.split("/")[-1]

        has_google_key = bool(
            settings.GOOGLE_API_KEY and settings.GOOGLE_API_KEY.get_secret_value()
        )
        is_gemini_model = "gemini" in model_str

        self.client = getattr(ctx, "_genai_client", None)
        if self.client is not None:
            self.use_native_gemini = True
        elif has_google_key and is_gemini_model:
            try:
                self.client = genai.Client(api_key=settings.GOOGLE_API_KEY.get_secret_value())
                ctx._genai_client = self.client
                self.use_native_gemini = True
            except Exception as e:
                logger.warning(
                    f"Failed to initialize native Gemini client for Explorer: {e}."
                    " Using universal engine."
                )
                self.use_native_gemini = False
        else:
            self.use_native_gemini = False

    def _prune_historical_images(self, contents, keep_last=1):
        image_parts = []
        for content in contents:
            for part in content.parts:
                if getattr(part, "inline_data", None) or getattr(part, "file_data", None):
                    image_parts.append(part)
        if len(image_parts) <= keep_last:
            return
        to_keep_ids = {id(p) for p in image_parts[-keep_last:]}
        for content in contents:
            content.parts = [
                types.Part.from_text(
                    text=("[Image pruned to maintain visual focus on latest state]")
                )
                if (getattr(p, "inline_data", None) or getattr(p, "file_data", None))
                and id(p) not in to_keep_ids
                else p
                for p in content.parts
            ]

    def get_exposed_tools(self, only_submit: bool = False) -> list[types.FunctionDeclaration]:
        """Returns the list of tools, filtering out denylisted and unconfigured ones."""
        denylisted = set(self.denylisted_tools)
        if not is_ocr_configured():
            denylisted.add("get_ocr_list")
        tools = [tool for tool in self.TOOLS if tool.name not in denylisted]
        if only_submit:
            tools = [tool for tool in tools if tool.name == "submit_answer"]
        return tools

    @trace(type="agent", name="explorer")
    async def run(
        self,
        query: str,
        context_feedback: str,
        screenshot_path: str,
        state: State,
        minimal_list: str = "",
        enable_caching: bool = False,
        version: Literal["flash", "pro", "ultra"] = "pro",
    ) -> str:
        if version == "flash":
            return await self._run_flash(query, screenshot_path)

        self.http_client = httpx.AsyncClient()

        # 1. Compute current image hash and verify with Data Engine
        image_name, record = self._resolve_image_record(screenshot_path)
        self.image_name = image_name
        self.screenshot_path = screenshot_path

        # Resolve parameters
        mode, max_iterations = self._resolve_version_limits(version)

        # 2. Prepare Native Tools declarations
        self._resolve_screen_dimensions(state)

        # Initialize Image Pool
        self._init_image_pool(screenshot_path)

        # 4. Initialize or reuse dynamic context-level GenAI client for connection pooling
        client = self._ensure_genai_client()

        model_name, temperature, thinking_level, fallback_model = self._resolve_model_params()

        # 5. Construct Prompt & Initial Message List
        prompt_template, prompt_error = self._build_prompt_template(version, mode, max_iterations)
        if prompt_error is not None:
            return prompt_error

        # Generate initial visual annotations if minimal_list is empty
        minimal_list, img_to_read = await self._prepare_initial_annotation(
            record, state, screenshot_path, minimal_list, image_name
        )

        if not self.use_native_gemini:
            return await self._run_universal(
                query=query,
                context_feedback=context_feedback,
                screenshot_path=img_to_read,
                state=state,
                minimal_list=minimal_list,
                version=version,
                prompt_template=prompt_template,
                max_iterations=max_iterations,
            )

        return await self._run_native(
            client=client,
            query=query,
            context_feedback=context_feedback,
            minimal_list=minimal_list,
            img_to_read=img_to_read,
            enable_caching=enable_caching,
            prompt_template=prompt_template,
            model_name=model_name,
            temperature=temperature,
            thinking_level=thinking_level,
            max_iterations=max_iterations,
        )
