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

"""Setup phases of ``Explorer.run``.

Split out of ``artemis.agents.explorer.explorer``: the flash-tier one-shot
detection path, the per-run screen loading shared by every tier (image record
resolution, screen dimensions, fused hierarchy, in-memory screen index) and
the named preparation phases executed before a reasoning loop (image pool,
model parameters, tier-aware prompt construction, initial screenshot
annotation), packaged as a mixin consumed by ``Explorer``.  Patched
collaborators (``settings``, ``StorageManager``, ``is_ocr_configured``,
``perform_ocr``, ``draw_dots``, ``_run_object_detection``, ``logger``) are
resolved through the facade module at call time; see
``artemis.agents.explorer._facade``.
"""

import base64
import glob
import hashlib
import json
import os
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any

import httpx

from artemis.agents.explorer._facade import facade
from artemis.agents.explorer.constants import EXECUTION_CONSTRAINT_TEMPLATE
from artemis.agents.explorer.geometry import resolve_screen_size
from artemis.agents.explorer.perception_tools import load_detector_templates
from artemis.agents.explorer.screen_index import ScreenElement, ScreenIndex
from artemis.agents.explorer.tiers import SUBMIT_TOOL, ExplorerTier
from artemis.graph.state import State
from artemis.utils.ocr_xml_fusion import fuse_ocr_with_xml
from artemis.utils.visualization import format_minimal_list_with_elements

#: Prompt bullets may be plain strings or ``{"text": ..., "requires": [...]}``
#: objects; the latter are emitted only when every listed tool is exposed.
PromptBullet = str | dict[str, Any]


def _bullet_text(bullet: PromptBullet, exposed_tools: set[str]) -> str | None:
    """Returns the bullet text, or None when its tool requirements are unmet."""
    if isinstance(bullet, dict):
        required = bullet.get("requires") or []
        if not all(tool in exposed_tools for tool in required):
            return None
        return str(bullet.get("text", ""))
    return str(bullet)


def load_prompt_sections(prompt_path: Path) -> dict[str, Any]:
    """Loads ``explorer.json`` tolerating ``//`` and ``/* */`` comments and trailing commas."""
    content = prompt_path.read_text(encoding="utf-8")
    content = re.sub(r"(?<!:)\/\/.*", "", content)
    content = re.sub(r"/\*.*?\*/", "", content, flags=re.DOTALL)
    content = re.sub(r",\s*([\]}])", r"\1", content)
    return json.loads(content)


class RunSetupMixin:
    """Flash mode and pre-loop setup phases of :class:`Explorer`."""

    if TYPE_CHECKING:
        from artemis.context import ArtemisContext

        DENYLIST_TEMPLATE: str
        ctx: ArtemisContext
        width: int
        height: int
        image_name: str | None
        screenshot_path: str | None
        image_pool: dict[str, dict[str, Any]]
        next_img_id: int
        global_label_idx: int
        http_client: httpx.AsyncClient | None
        denylisted_tools: set[str]
        _user_denylist: frozenset[str]
        screen_index: ScreenIndex
        label_registry: dict[str, ScreenElement]

        def _hidden_tool_names(self) -> set[str]: ...

        def _register_label(self, label: str, element: ScreenElement) -> None: ...

        def _registry_element(self, entry: dict[str, Any]) -> ScreenElement: ...

    @staticmethod
    def _failure_outcome(message: str) -> str:
        """The contract-shaped answer for a run that produced no candidates."""
        return json.dumps({"candidates": [], "fallback_message": message}, ensure_ascii=False)

    async def _run_flash(self, query: str, screenshot_path: str) -> str:
        """Flash tier: one-shot object detection without a reasoning loop.

        Several targets may be listed in ``query`` separated by ``|``; each
        detection becomes a ``D<n>`` candidate described by its detected label.
        """
        _ex = facade()
        templates, global_timeout = load_detector_templates()

        queries = [q.strip() for q in query.split("|") if q.strip()]

        try:
            result = await _ex._run_object_detection(
                self.ctx,
                screenshot_path,
                queries,
                templates,
                global_timeout=global_timeout,
            )
            detected_items = result.get("detected", [])
            candidates = []
            for idx, item in enumerate(detected_items):
                pos = item.get("point")
                if pos and isinstance(pos, list) and len(pos) == 2:
                    candidates.append(
                        {
                            "label": f"D{idx + 1}",
                            "coords": pos,
                            "description": item.get("label", query),
                        }
                    )
            fallback_message = "" if candidates else f"Failed to detect: {query}"
            return json.dumps(
                {"candidates": candidates, "fallback_message": fallback_message},
                ensure_ascii=False,
            )
        except Exception as e:
            _ex.logger.error(f"Flash mode object detection failed: {e}")
            return self._failure_outcome(f"Flash mode detection error: {e}")

    def _resolve_image_record(self, screenshot_path: str) -> tuple:
        """Computes the screenshot hash and looks it up in the Data Engine DB."""
        _ex = facade()
        image_name = None
        record = None
        try:
            sha256_hash = hashlib.sha256()
            with open(screenshot_path, "rb") as f:
                for byte_block in iter(lambda: f.read(4096), b""):
                    sha256_hash.update(byte_block)
            computed_hash = sha256_hash.hexdigest()
            _ex.logger.info(f"Computed screenshot hash: {computed_hash}")

            # Check if it exists in DB
            db_path = _ex.settings.DATA_ENGINE_DB_PATH
            base_dir = _ex.settings.TRACES_PATH
            storage = _ex.StorageManager(db_path, base_dir)
            record = storage.get_image(computed_hash)

            if record:
                image_name = computed_hash
            else:
                _ex.logger.warning(
                    f"Image hash {computed_hash} not found in Data Engine DB."
                    " Data Engine might not be synced yet."
                )

        except Exception as e:
            _ex.logger.warning(f"Failed to compute hash or check DB: {e}")

        return image_name, record

    def _resolve_screen_dimensions(self, state: State) -> None:
        """Adopts the screenshot's pixel size (operator observation, device, fallback)."""
        self.width, self.height = resolve_screen_size(self.ctx, state)

    async def _load_screen(
        self, screenshot_path: str, state: State, tier: ExplorerTier
    ) -> list[dict[str, Any]]:
        """Loads the screen once per run: record, screen size, fused hierarchy, index.

        Every tier goes through here so the structural pre-pass and the
        perception tools share one in-memory snapshot instead of asking the
        Data Engine per call.  On-the-fly OCR is reserved for loop tiers:
        flash never exposes text tools and must stay a single round trip.
        """
        image_name, record = self._resolve_image_record(screenshot_path)
        self.image_name = image_name
        self._resolve_screen_dimensions(state)
        fused_xml = await self._build_fused_hierarchy(
            record, state, screenshot_path, allow_ocr=not tier.is_oneshot
        )
        self.screen_index = ScreenIndex.from_hierarchy(fused_xml, self.width, self.height)
        return fused_xml

    def _init_image_pool(self, screenshot_path: str) -> None:
        """Seeds the Image Pool with the original screenshot as img_0."""
        self.image_pool = {
            "img_0": {
                "path": screenshot_path,
                "transform": {
                    "offset_x": 0.0,
                    "offset_y": 0.0,
                    "scale_x": 1.0,
                    "scale_y": 1.0,
                },
                "description": "Original complete screenshot",
            }
        }
        self.next_img_id = 1

    def _resolve_model_params(self) -> tuple:
        """Resolves model name, temperature, thinking level, and fallback model."""
        _ex = facade()
        llm_config = getattr(self.ctx, "llm_config", None)
        llm_cfg = getattr(llm_config, "explorer", None) if llm_config else None
        temperature = 0.1
        fallback_model = None
        thinking_level = None
        if llm_cfg:
            model_name = llm_cfg.model
            if "/" in model_name:
                model_name = model_name.split("/")[-1]
            if getattr(llm_cfg, "temperature", None) is not None:
                temperature = llm_cfg.temperature
            if getattr(llm_cfg, "thinking_level", None) is not None:
                thinking_level = llm_cfg.thinking_level
            if getattr(llm_cfg, "fallback", None):
                fallback_model = llm_cfg.fallback.model
                if "/" in fallback_model:
                    fallback_model = fallback_model.split("/")[-1]
        else:
            model_name = _ex.DEFAULT_EXPLORER_MODEL

        return model_name, temperature, thinking_level, fallback_model

    def _exposed_tool_names(self, tier: ExplorerTier) -> set[str]:
        """Tools the model can actually call in this run (tier minus hidden)."""
        return (set(tier.tools) | {SUBMIT_TOOL}) - self._hidden_tool_names()

    def _build_prompt_template(self, tier: ExplorerTier) -> tuple[str | None, str | None]:
        """Builds the tier-aware system prompt; returns ``(prompt, error_message)``.

        Bullets whose ``requires`` list names a tool the model cannot call in
        this tier are dropped, so the prompt never describes capabilities
        that are absent from the tool declarations.  Tier-hidden tools are
        simply never mentioned; only the user's own denylist (restricted to
        tools the tier would otherwise expose) is spelled out.
        """
        prompt_path = Path(__file__).parent / "explorer.json"
        if not prompt_path.exists():
            return None, "Error: Explorer prompt template not found."

        try:
            data = load_prompt_sections(prompt_path)
        except Exception as e:
            return None, f"Error loading or parsing explorer.json: {e}"

        exposed = self._exposed_tool_names(tier)
        prompt_parts: list[str] = []
        for section, content_val in data.items():
            if isinstance(content_val, list):
                bullets = [
                    text
                    for text in (_bullet_text(b, exposed) for b in content_val)
                    if text is not None
                ]
                if not bullets:
                    continue
                prompt_parts.append(f"# {section}")
                prompt_parts.extend(f"- {bullet}" for bullet in bullets)
            else:
                text = _bullet_text(content_val, exposed)
                if text is None:
                    continue
                prompt_parts.append(f"# {section}")
                prompt_parts.append(text)
            prompt_parts.append("")

        prompt_template = "\n".join(prompt_parts)

        user_denied = sorted(set(self._user_denylist) & set(tier.tools))
        if user_denied:
            prompt_template += self.DENYLIST_TEMPLATE.format(tools=", ".join(user_denied))

        constraint = EXECUTION_CONSTRAINT_TEMPLATE.format(max_turns=tier.max_turns)
        prompt_template += "\n# EXECUTION CONSTRAINT\n- " + constraint + "\n"

        return prompt_template, None

    async def _build_fused_hierarchy(
        self, record, state: State, screenshot_path: str, allow_ocr: bool = True
    ) -> list[dict[str, Any]]:
        """Loads (and if needed OCR-fuses) the UI hierarchy for the screenshot.

        Precedence: the Data Engine record for the screenshot hash, else the
        state's latest hierarchy when the screenshot is the latest one.  The
        HTTP client backing on-the-fly OCR is created here on first need and
        released by ``Explorer.run`` regardless of the engine used.
        """
        _ex = facade()
        fused_xml: Any = []
        if record and getattr(record, "ui_tree", None):
            ui_tree = record.ui_tree
            ocr_results = getattr(record, "ocr_result", None)
            if ocr_results is None:
                if allow_ocr and _ex.is_ocr_configured():
                    try:
                        _ex.logger.info(
                            "Previous screenshot OCR is missing. Running OCR on-the-fly..."
                        )
                        with open(screenshot_path, "rb") as img_file:
                            img_b64 = base64.b64encode(img_file.read()).decode("utf-8")
                        if self.http_client is None:
                            self.http_client = httpx.AsyncClient()
                        ocr_results = await _ex.perform_ocr(img_b64, client=self.http_client)
                    except Exception as ocr_err:
                        _ex.logger.error(
                            f"On-the-fly OCR failed for previous screenshot: {ocr_err}"
                        )
                        ocr_results = []
                else:
                    ocr_results = []

            fused_xml = fuse_ocr_with_xml(ui_tree, ocr_results or [])
            _ex.logger.info("Successfully loaded and fused UI hierarchy for previous screenshot.")
        elif screenshot_path == getattr(state, "latest_screenshot", None):
            fused_xml = getattr(state, "latest_ui_hierarchy", None)
        return list(fused_xml) if isinstance(fused_xml, (list, tuple)) else []

    def _annotate_initial_screenshot(
        self, fused_xml, screenshot_path: str, image_name, minimal_list: str
    ) -> tuple[str, object]:
        """Draws labeled dots for the fused hierarchy; returns (minimal_list, marked_path).

        Every numbered label is registered against its element so a
        candidate the model later submits by number inherits real bounds.
        """
        _ex = facade()
        ctx = self.ctx
        marked_path = None
        try:
            _ex.logger.info(
                "Explorer self-annotating initial screenshot using latest_ui_hierarchy..."
            )
            formatted_list, elements, labels = format_minimal_list_with_elements(
                fused_xml, self.width, self.height
            )
            minimal_list = formatted_list
            points = [list(entry["center"]) for entry in elements]
            for label, entry in zip(labels, elements, strict=True):
                self._register_label(label, self._registry_element(entry))
            self.global_label_idx = len(points) + 1

            base_dir = (
                Path(ctx.data_engine.base_dir)
                if ctx.data_engine and getattr(ctx.data_engine, "base_dir", None)
                else None
            )
            if not base_dir:
                base_dir = _ex.settings.TRACES_PATH
            images_dir = base_dir / "images"
            initial_marked_dir = images_dir / "initial_marked"
            initial_marked_dir.mkdir(parents=True, exist_ok=True)

            existing_files = glob.glob(
                str(initial_marked_dir / f"{image_name or 'temp_image'}_*.jpg")
            )
            max_seq = 0
            for f in existing_files:
                match = re.search(r"_(\d+)\.jpg$", f)
                if match:
                    max_seq = max(max_seq, int(match.group(1)))
            seq = max_seq + 1
            marked_path = initial_marked_dir / f"{image_name or 'temp_image'}_{seq}.jpg"

            # Draw dots on the raw screenshot
            _ex.draw_dots(screenshot_path, points, labels, str(marked_path))
            _ex.logger.info(
                f"Successfully drew {len(points)} dots and saved marked image to {marked_path}"
            )
        except Exception as e:
            _ex.logger.error(f"Failed to self-annotate initial screenshot: {e}")
        return minimal_list, marked_path

    def _prepare_initial_annotation(
        self, fused_xml, screenshot_path: str, minimal_list: str, image_name
    ) -> tuple[str, str]:
        """Generates initial visual annotations when no minimal list is provided.

        Returns the (possibly updated) minimal list and the image path the
        model should read (marked screenshot when available, raw otherwise).
        """
        marked_path = None
        if not minimal_list and fused_xml:
            minimal_list, marked_path = self._annotate_initial_screenshot(
                fused_xml, screenshot_path, image_name, minimal_list
            )

        if marked_path and os.path.exists(str(marked_path)):
            img_to_read = str(marked_path)
        else:
            img_to_read = screenshot_path
        return minimal_list, img_to_read
