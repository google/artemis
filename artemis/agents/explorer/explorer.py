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

"""Explorer agent entry point.

The :class:`Explorer` class combines the following modules:

- ``tiers``: engine, tools and budgets for flash / pro / ultra.
- ``tool_declarations``: static tool schemas (universal).
- ``perception_tools``: the ``exec_*`` perception/vision tool method group.
- ``universal_runner``: the LangChain-based ``_run_universal`` loop.
- ``run_setup``: flash mode and the pre-loop setup phases of ``run``.
"""

import json
from typing import Any, Literal

from artemis.agents.explorer.constants import DEFAULT_EXPLORER_MODEL
from artemis.agents.explorer.geometry import (
    FALLBACK_SCREEN_SIZE,
    is_valid_norm_point,
    norm_to_pixel,
    pixel_to_norm,
)
from artemis.agents.explorer.perception_tools import PerceptionToolsMixin
from artemis.agents.explorer.run_setup import RunSetupMixin
from artemis.agents.explorer.screen_index import normalize_text, ScreenElement, ScreenIndex
from artemis.agents.explorer.tiers import ExplorerTier, get_tier
from artemis.agents.explorer.universal_runner import UniversalRunnerMixin
from artemis.context import ArtemisContext
from artemis.data_engine.trace import trace
from artemis.graph.state import State
from artemis.llm.naming import strip_provider_prefix
from artemis.utils.ocr_api import is_ocr_configured
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

__all__ = ["Explorer"]

#: How far outside its labeled element (as a fraction of the screen, per side)
#: a candidate's point may fall and still inherit that element's bounds.
LABEL_TOLERANCE_RATIO = 0.02


class Explorer(PerceptionToolsMixin, UniversalRunnerMixin, RunSetupMixin):
    """Locates UI elements on a screenshot for the ``ask_explorer`` tool.

    The tier (flash / pro / ultra) is applied per ``run`` call from
    :data:`artemis.agents.explorer.tiers.EXPLORER_TIERS`; it decides the
    turn budget and the exposed perception tools.  Construction is
    side-effect free: no network client is created.
    """

    DENYLIST_TEMPLATE = (
        "\n# TOOL DENYLIST\n- The following tools are denylisted and cannot be used: {tools}\n"
    )

    def __init__(self, ctx: ArtemisContext):
        self.ctx = ctx
        self.global_label_idx = 1
        self.width, self.height = FALLBACK_SCREEN_SIZE
        self.image_name: str | None = None
        self.screenshot_path: str | None = None
        self.image_pool: dict[str, dict[str, Any]] = {}
        self.next_img_id = 1
        self.http_client = None

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

    def _resolve_model_name(self) -> str:
        """Returns the configured Explorer model name without a provider prefix."""
        llm_config = getattr(self.ctx, "llm_config", None)
        llm_cfg = getattr(llm_config, "explorer", None) if llm_config else None
        model = getattr(llm_cfg, "model", None) if llm_cfg else None
        model_name = model if isinstance(model, str) and model else DEFAULT_EXPLORER_MODEL
        return strip_provider_prefix(model_name)

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
        fused_xml: list[dict[str, Any]],
    ) -> str:
        """Prepares the run, then hands the unresolved query to the universal loop."""
        self._init_image_pool(screenshot_path)

        prompt_template, prompt_error = self._build_prompt_template(tier)
        if prompt_error is not None:
            return self._failure_outcome(prompt_error)

        minimal_list, img_to_read = self._prepare_initial_annotation(
            fused_xml, screenshot_path, minimal_list, self.image_name
        )

        return await self._run_universal(
            query=query,
            context_feedback=context_feedback,
            screenshot_path=img_to_read,
            state=state,
            minimal_list=minimal_list,
            prompt_template=prompt_template,
            max_turns=self.max_turns,
        )

    async def _close_http_client(self) -> None:
        """Releases the on-demand OCR HTTP client used by the perception tools."""
        client, self.http_client = self.http_client, None
        if client is None:
            return
        try:
            await client.aclose()
            logger.info("Closed Explorer HTTP client.")
        except Exception as close_err:
            logger.warning(f"Failed to close Explorer HTTP client: {close_err}")
