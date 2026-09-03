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

- ``tiers``: the flash / pro / ultra tier table (single source of truth).
- ``tool_declarations``: static tool schemas (universal + native).
- ``perception_tools``: the ``exec_*`` perception/vision tool method group.
- ``universal_runner``: the LangChain-based ``_run_universal`` loop.
- ``run_setup``: flash mode and the pre-loop setup phases of ``run``.
- ``native_runner``: the native Gemini reasoning loop of ``run``.

All historical module-level names (``settings``, ``StorageManager``,
``genai``, ``_run_object_detection``, ``draw_dots``, ``perform_ocr``,
``is_ocr_configured``, ``get_llm``, ``UNIVERSAL_EXPLORER_TOOLS``, ...) remain
importable and patchable here; split-out code resolves them through this
module at call time (see ``artemis.agents.explorer._facade``).
"""

import asyncio
import json
import os
from typing import Any, Literal

from google import genai
from google.genai import types

from artemis.agents.explorer.geometry import (
    FALLBACK_SCREEN_SIZE,
    is_valid_norm_point,
    norm_to_pixel,
    pixel_to_norm,
)
from artemis.agents.explorer.native_runner import NativeRunnerMixin
from artemis.agents.explorer.perception_tools import PerceptionToolsMixin
from artemis.agents.explorer.run_setup import RunSetupMixin
from artemis.agents.explorer.screen_index import normalize_text, ScreenElement, ScreenIndex
from artemis.agents.explorer.tiers import ExplorerTier, get_tier
from artemis.agents.explorer.tool_declarations import (
    NATIVE_EXPLORER_TOOL_DECLARATIONS,
    UNIVERSAL_EXPLORER_TOOLS,
)
from artemis.agents.explorer.universal_runner import UniversalRunnerMixin
from artemis.agents.object_detector.object_detector import _run_object_detection
from artemis.config import settings
from artemis.context import ArtemisContext
from artemis.data_engine.storage import StorageManager
from artemis.data_engine.trace import trace
from artemis.graph.state import State
from artemis.llm.reliability import (
    LLMExhaustedError,
    LLMPermanentError,
    classify_failure,
    retry_policy_for,
)
from artemis.services.llm import _record_llm_event, _record_llm_retry, get_llm
from artemis.utils.logger import get_logger
from artemis.utils.ocr_api import is_ocr_configured, perform_ocr
from artemis.utils.visualization import draw_dots

logger = get_logger(__name__)

#: Public surface plus the late-bound collaborators the split-out modules
#: resolve through this facade (``mock.patch`` targets for the test suite).
__all__ = [
    "DEFAULT_EXPLORER_MODEL",
    "Explorer",
    "NATIVE_EXPLORER_TOOL_DECLARATIONS",
    "StorageManager",
    "UNIVERSAL_EXPLORER_TOOLS",
    "_generate_content_with_reliability",
    "_run_object_detection",
    "draw_dots",
    "genai",
    "get_llm",
    "is_ocr_configured",
    "logger",
    "perform_ocr",
    "settings",
]

#: Model used when the LLM configuration carries no Explorer entry.
DEFAULT_EXPLORER_MODEL = "gemini-3.8-flash"

#: Environment override for Gemini explicit context caching ("true" / "false").
CACHING_ENV_VAR = "ARTEMIS_EXPLORER_CACHING"

#: How far outside its labeled element (as a fraction of the screen, per side)
#: a candidate's point may fall and still inherit that element's bounds.
LABEL_TOLERANCE_RATIO = 0.02


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
                logger.error(f"{label} permanently failed [{failure.category.value}]: {call_err}")
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
                    f"{label} exhausted {attempt} attempt(s) [{failure.category.value}]: {call_err}"
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
    """Locates UI elements on a screenshot for the ``ask_explorer`` tool.

    The tier (flash / pro / ultra) is applied per ``run`` call from
    :data:`artemis.agents.explorer.tiers.EXPLORER_TIERS`; it decides the
    engine, the turn budget, the exposed perception tools and the caching
    default.  Construction is side-effect free: no network client is created
    until the native Gemini branch actually needs one.
    """

    DENYLIST_TEMPLATE = (
        "\n# TOOL DENYLIST\n- The following tools are denylisted and cannot be used: {tools}\n"
    )
    TOOLS = NATIVE_EXPLORER_TOOL_DECLARATIONS

    def __init__(self, ctx: ArtemisContext):
        self.ctx = ctx
        self.global_label_idx = 1
        self.width, self.height = FALLBACK_SCREEN_SIZE
        self.image_name: str | None = None
        self.screenshot_path: str | None = None
        self.image_pool: dict[str, dict[str, Any]] = {}
        self.next_img_id = 1
        self.http_client = None
        self.client = None
        self.turn_latencies: list[float] = []
        self.turn_cached_tokens: list[int] = []
        self.trace_history: list[dict[str, Any]] = []

        # Rebuilt per ``run``: the queryable snapshot of the screen, and the
        # element behind every label the model has been shown so far.
        self.screen_index: ScreenIndex = ScreenIndex.empty(self.width, self.height)
        self.label_registry: dict[str, ScreenElement] = {}

        # No tier is applied until ``run``; until then only the user's own
        # denylist (plus unconfigured OCR) restricts the tool set.
        self.tier: ExplorerTier | None = None
        self.max_turns = 0
        self._user_denylist = self._load_user_denylist()
        self.denylisted_tools = self._compute_denylist(None)

        self.model_name = self._resolve_model_name()
        self.use_native_gemini = self._detect_native_engine()

    # ------------------------------------------------------------------ #
    # Engine selection (no side effects)
    # ------------------------------------------------------------------ #

    def _resolve_model_name(self) -> str:
        """Returns the configured Explorer model name without a provider prefix."""
        llm_config = getattr(self.ctx, "llm_config", None)
        llm_cfg = getattr(llm_config, "explorer", None) if llm_config else None
        model = getattr(llm_cfg, "model", None) if llm_cfg else None
        model_name = model if isinstance(model, str) and model else DEFAULT_EXPLORER_MODEL
        if "/" in model_name:
            model_name = model_name.split("/")[-1]
        return model_name

    def _detect_native_engine(self) -> bool:
        """True when the native google-genai SDK should drive the reasoning loop.

        A Gemini model alone is not enough: without a Google API key (or a
        client already shared on the context) the SDK cannot be constructed,
        so such configurations run through the universal LangChain engine.
        """
        if "gemini" not in self.model_name.lower():
            return False
        if getattr(self.ctx, "_genai_client", None) is not None:
            return True
        api_key = getattr(settings, "GOOGLE_API_KEY", None)
        return bool(api_key and api_key.get_secret_value())

    def _ensure_genai_client(self):
        """Returns the context-level GenAI client, creating it on first use.

        Only the native branch calls this, so universal configurations never
        touch ``genai`` (and never fail on a missing Google key).  The client
        is stored on the context for connection pooling across agents.
        """
        client = getattr(self.ctx, "_genai_client", None)
        if client is None:
            logger.info("Initializing GenAI client on context for connection pooling (Explorer)")
            api_key = getattr(settings, "GOOGLE_API_KEY", None)
            client = genai.Client(api_key=api_key.get_secret_value() if api_key else None)
            self.ctx._genai_client = client
        self.client = client
        return client

    # ------------------------------------------------------------------ #
    # Tier application
    # ------------------------------------------------------------------ #

    def _load_user_denylist(self) -> frozenset[str]:
        """Reads the user-configured Explorer denylist from the agent config."""
        try:
            agent_cfg = getattr(self.ctx, "agent_config", None)
            denylists = getattr(agent_cfg, "denylisted_tools", None) if agent_cfg else None
            configured = denylists.get("explorer", []) if isinstance(denylists, dict) else []
            return frozenset(str(name) for name in configured)
        except (TypeError, AttributeError):
            return frozenset()

    def _compute_denylist(self, tier: ExplorerTier | None) -> set[str]:
        """Tools the model must not see: tier-hidden, user-denylisted, unconfigured OCR."""
        denylist = set(self._user_denylist)
        if tier is not None:
            denylist |= tier.hidden_tools
        if not is_ocr_configured():
            denylist.add("get_ocr_list")
        return denylist

    def _apply_tier(self, tier: ExplorerTier) -> None:
        """Adopts the tier's turn budget and tool exposure for this run."""
        self.tier = tier
        self.max_turns = tier.max_turns
        self.denylisted_tools = self._compute_denylist(tier)

    def _resolve_caching(self, tier: ExplorerTier, enable_caching: bool | None) -> bool:
        """Resolves Gemini explicit context caching for this run.

        Precedence: explicit argument, ``ARTEMIS_EXPLORER_CACHING`` env
        override, the agent configuration, the execution setup, the global
        settings, then the tier default.  Configuration values are only
        trusted when they are real booleans so a mocked or malformed config
        (``None``, sentinel objects) falls through instead of forcing a value.
        """
        if isinstance(enable_caching, bool):
            return enable_caching
        env_value = os.getenv(CACHING_ENV_VAR, "").strip().lower()
        if env_value in ("true", "false"):
            return env_value == "true"
        for source in (
            getattr(self.ctx, "agent_config", None),
            getattr(self.ctx, "execution_setup", None),
        ):
            value = getattr(getattr(source, "explorer", None), "caching", None)
            if isinstance(value, bool):
                return value
        value = getattr(settings, "EXPLORER_CACHING", None)
        if isinstance(value, bool):
            return value
        return tier.caching

    def _hidden_tool_names(self) -> set[str]:
        """Denylisted tools plus OCR when it is unconfigured, evaluated at call time.

        OCR availability is re-checked here (not only in ``_apply_tier``) so
        callers that reset ``denylisted_tools`` still never see ``get_ocr_list``
        without a configured OCR backend.
        """
        hidden = set(self.denylisted_tools)
        if not is_ocr_configured():
            hidden.add("get_ocr_list")
        return hidden

    def get_exposed_tools(self, only_submit: bool = False) -> list[types.FunctionDeclaration]:
        """Native tool declarations the model may see for the current tier."""
        hidden = self._hidden_tool_names()
        tools = [tool for tool in self.TOOLS if tool.name not in hidden]
        if only_submit:
            tools = [tool for tool in tools if tool.name == "submit_answer"]
        return tools

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

    # ------------------------------------------------------------------ #
    # Label bounds and the structural pre-pass
    # ------------------------------------------------------------------ #

    def _norm_bounds(self, element: ScreenElement) -> list[int] | None:
        """The element's bounds on the 0-1000 grid, or None when they collapse.

        A box that rounds to zero width or height is useless to the caller's
        hit test, so it is withheld rather than shipped as a degenerate rect.
        """
        left, top, right, bottom = element.bounds
        n_left, n_top = pixel_to_norm(left, top, self.width, self.height)
        n_right, n_bottom = pixel_to_norm(right, bottom, self.width, self.height)
        if n_right <= n_left or n_bottom <= n_top:
            return None
        return [n_left, n_top, n_right, n_bottom]

    def _enrich_candidates(self, candidates: list[Any]) -> list[Any]:
        """Attaches the registered element's bounds to candidates that point at it.

        The label alone is not trusted: models occasionally reuse a label for
        a different spot, so the coordinates must also fall inside the
        element (padded by :data:`LABEL_TOLERANCE_RATIO` of the screen to
        absorb rounding).  Unknown labels, detection-only labels and points
        outside the element pass through untouched.
        """
        enriched: list[Any] = []
        for cand in candidates:
            element = (
                self.label_registry.get(str(cand.get("label"))) if isinstance(cand, dict) else None
            )
            coords = cand.get("coords") if element is not None else None
            if element is None or not is_valid_norm_point(coords):
                enriched.append(cand)
                continue
            px, py = norm_to_pixel(int(coords[0]), int(coords[1]), self.width, self.height)
            left, top, right, bottom = element.bounds
            pad_x = self.width * LABEL_TOLERANCE_RATIO
            pad_y = self.height * LABEL_TOLERANCE_RATIO
            inside = left - pad_x <= px <= right + pad_x and top - pad_y <= py <= bottom + pad_y
            norm_bounds = self._norm_bounds(element) if inside else None
            if norm_bounds is None:
                enriched.append(cand)
                continue
            enriched.append({**cand, "bounds": norm_bounds, "source": element.source})
        return enriched

    def _structural_prepass(self, query: str) -> tuple[list[dict[str, Any]], list[str]]:
        """Resolves the ``|``-separated parts of ``query`` that exactly name one element.

        Returns ``(candidates, unresolved_parts)``.  Only a *unique* exact
        label match is taken: several matches mean the caller's wording does
        not disambiguate, which is exactly what the model is for.
        """
        parts = [part.strip() for part in query.split("|") if part.strip()]
        candidates: list[dict[str, Any]] = []
        unresolved: list[str] = []
        for part in parts:
            hits = self.screen_index.exact_matches(part)
            if len(hits) != 1:
                unresolved.append(part)
                continue
            element = hits[0]
            label = f"T{len(candidates) + 1}"
            self._register_label(label, element)
            cx, cy = element.center
            candidate: dict[str, Any] = {
                "label": label,
                "coords": list(pixel_to_norm(cx, cy, self.width, self.height)),
            }
            norm_bounds = self._norm_bounds(element)
            if norm_bounds is not None:
                candidate["bounds"] = norm_bounds
            candidate["source"] = element.source
            candidate["description"] = (
                f"{element.text} (exact text match)"
                if normalize_text(part) == normalize_text(element.text)
                else f"{part} (exact text match: '{element.text}')"
            )
            candidates.append(candidate)
        return candidates, unresolved

    @staticmethod
    def _merge_prepass(prepass: list[dict[str, Any]], raw: str) -> str:
        """Prepends pre-pass candidates to the engine's outcome JSON.

        An engine answer that is not outcome-shaped (a bare error string)
        becomes the ``fallback_message`` so the structural hits survive it.
        """
        if not prepass:
            return raw
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            data = None
        if not isinstance(data, dict):
            return json.dumps(
                {"candidates": prepass, "fallback_message": str(raw)}, ensure_ascii=False
            )
        engine_candidates = data.get("candidates") or []
        return json.dumps(
            {
                "candidates": [*prepass, *engine_candidates],
                "fallback_message": str(data.get("fallback_message") or ""),
            },
            ensure_ascii=False,
        )

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #

    @trace(type="agent", name="explorer")
    async def run(
        self,
        query: str,
        context_feedback: str,
        screenshot_path: str,
        state: State,
        minimal_list: str = "",
        enable_caching: bool | None = None,
        version: Literal["flash", "pro", "ultra"] = "pro",
    ) -> str:
        """Locates ``query`` on ``screenshot_path`` under the ``version`` tier.

        Every tier first indexes the screen in memory and tries the
        structural pre-pass; only the parts of the query it cannot settle
        reach the tier's engine.  Returns the JSON string
        ``{"candidates": [...], "fallback_message": "..."}`` consumed by
        ``artemis.tools.explorer_tool``.
        """
        tier = get_tier(version)
        self._apply_tier(tier)
        self.screenshot_path = screenshot_path
        self.global_label_idx = 1
        self.label_registry = {}

        try:
            fused_xml = await self._load_screen(screenshot_path, state, tier)
            prepass, unresolved = self._structural_prepass(query)
            if prepass and not unresolved:
                logger.info(
                    f"Explorer pre-pass answered {query!r} from the UI tree without a model call."
                )
                return json.dumps(
                    {"candidates": prepass, "fallback_message": ""}, ensure_ascii=False
                )
            engine_query = " | ".join(unresolved) if prepass else query

            if tier.is_oneshot:
                raw = await self._run_flash(engine_query, screenshot_path)
            else:
                raw = await self._run_loop(
                    tier,
                    engine_query,
                    context_feedback,
                    screenshot_path,
                    state,
                    minimal_list,
                    enable_caching,
                    fused_xml,
                )
            return self._merge_prepass(prepass, raw)
        finally:
            await self._close_http_client()

    async def _run_loop(
        self,
        tier: ExplorerTier,
        query: str,
        context_feedback: str,
        screenshot_path: str,
        state: State,
        minimal_list: str,
        enable_caching: bool | None,
        fused_xml: list[dict[str, Any]],
    ) -> str:
        """Shared setup for the loop engines, then the engine-specific loop."""
        self._init_image_pool(screenshot_path)

        prompt_template, prompt_error = self._build_prompt_template(tier)
        if prompt_error is not None:
            return self._failure_outcome(prompt_error)

        minimal_list, img_to_read = self._prepare_initial_annotation(
            fused_xml, screenshot_path, minimal_list, self.image_name
        )

        if not self.use_native_gemini:
            return await self._run_universal(
                query=query,
                context_feedback=context_feedback,
                screenshot_path=img_to_read,
                state=state,
                minimal_list=minimal_list,
                prompt_template=prompt_template,
                max_turns=self.max_turns,
            )

        client = self._ensure_genai_client()
        model_name, temperature, thinking_level, _fallback_model = self._resolve_model_params()
        return await self._run_native(
            client=client,
            query=query,
            context_feedback=context_feedback,
            minimal_list=minimal_list,
            img_to_read=img_to_read,
            enable_caching=self._resolve_caching(tier, enable_caching),
            prompt_template=prompt_template,
            model_name=model_name,
            temperature=temperature,
            thinking_level=thinking_level,
            max_turns=self.max_turns,
        )

    async def _close_http_client(self) -> None:
        """Releases the on-demand OCR HTTP client; both engines share this path."""
        client, self.http_client = self.http_client, None
        if client is None:
            return
        try:
            await client.aclose()
            logger.info("Closed Explorer HTTP client.")
        except Exception as close_err:
            logger.warning(f"Failed to close Explorer HTTP client: {close_err}")
