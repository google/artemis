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

"""Perception and vision tool implementations for the Explorer agent.

Split out of ``artemis.agents.explorer.explorer``: the ``exec_*`` tool method
group plus its search helpers, packaged as a mixin consumed by ``Explorer``.
Text search, the coordinate audit and the OCR inventory all answer from the
run's in-memory :class:`~artemis.agents.explorer.screen_index.ScreenIndex`;
no Data Engine round trip is involved, so an unsynced frame never degrades
to "no UI data".
"""

import asyncio
import glob
import json
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any

import cv2

from artemis.agents.explorer.geometry import norm_to_pixel, pixel_to_norm
from artemis.agents.explorer.screen_index import ScreenElement, ScreenIndex, normalize_text
from artemis.agents.image_processor.image_processor import ImageProcessor
from artemis.agents.object_detector.object_detector import _run_object_detection
from artemis.config import settings
from artemis.data_engine.trace import trace
from artemis.utils.logger import get_logger
from artemis.utils.visualization import draw_dots

logger = get_logger(__name__)

#: Text-search answer when the screen could not be indexed at all.
NO_UI_TREE_MESSAGE = "No UI-tree data is available for this screen; rely on visual detection."


def load_detector_templates() -> tuple[list[str], float]:
    """Load object-detector prompt templates and the optional global timeout.

    Shared by ``exec_detect_objects`` and the flash-mode ``run`` path; the two
    call sites historically carried an identical inline copy of this block.
    """
    detector_prompt_path = Path(__file__).parent.parent / "object_detector" / "object_detector.json"
    global_timeout = 30.0
    try:
        with open(detector_prompt_path, encoding="utf-8") as f:
            detector_config = json.load(f)
        detector_templates = detector_config.get("templates", [])
        detector_instructions = detector_config.get("instructions", "")
        raw_timeout = detector_config.get("global_timeout")
        if isinstance(raw_timeout, (int, float)) and raw_timeout > 0:
            global_timeout = float(raw_timeout)
    except Exception as e:
        logger.warning(f"Failed to load detector prompt config: {e}")
        detector_templates = ["Point to the following objects in the provided image: {labels_str}."]
        detector_instructions = ""

    templates = [f"{t}\n\n{detector_instructions}" for t in detector_templates]
    return templates, global_timeout


class PerceptionToolsMixin:
    """Perception/vision tool method group of :class:`Explorer`."""

    # Runtime initialization lives in ``Explorer``/``RunSetupMixin``.  Keep a
    # precise declaration here so this mixin's host requirements are explicit.
    if TYPE_CHECKING:
        from artemis.context import ArtemisContext

        ctx: ArtemisContext
        global_label_idx: int
        width: int
        height: int
        image_name: str | None
        screenshot_path: str | None
        image_pool: dict[str, dict[str, Any]]
        next_img_id: int
        screen_index: ScreenIndex
        label_registry: dict[str, ScreenElement]

    # ------------------------------------------------------------------ #
    # Label registry and annotation helpers
    # ------------------------------------------------------------------ #

    def _register_label(self, label: str, element: ScreenElement) -> None:
        """Remembers which element a label shown to the model stands for.

        Only labels backed by a real UI-tree or OCR element are registered;
        detection points (``D``) carry no bounds and are deliberately absent.
        """
        self.label_registry[label] = element

    def _registry_element(self, entry: dict[str, Any]) -> ScreenElement:
        """Maps a numbered-list entry back to its indexed element.

        The index is preferred over rebuilding the element from the entry
        because it carries the interaction flags the entry dropped.
        """
        raw_bounds = entry["bounds"]
        bounds = (int(raw_bounds[0]), int(raw_bounds[1]), int(raw_bounds[2]), int(raw_bounds[3]))
        text = str(entry.get("text") or "")
        source = "ocr" if entry.get("is_ocr") else "xml"
        for element in self.screen_index.elements:
            if (
                element.bounds == bounds
                and element.source == source
                and normalize_text(element.text) == normalize_text(text)
            ):
                return element
        return ScreenElement(
            text=text,
            bounds=bounds,
            source=source,
            class_name=str(entry["class"]) if entry.get("class") else None,
            resource_id=str(entry["resource_id"]) if entry.get("resource_id") else None,
        )

    def _next_output_path(self, subdir: str) -> Path:
        """Next free ``images/<subdir>/<image>_<n>.jpg`` under the traces directory."""
        out_dir = Path(settings.TRACES_PATH) / "images" / subdir
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = self.image_name or "temp_image"
        max_seq = 0
        for existing in glob.glob(str(out_dir / f"{stem}_*.jpg")):
            match = re.search(r"_(\d+)\.jpg$", existing)
            if match:
                max_seq = max(max_seq, int(match.group(1)))
        return out_dir / f"{stem}_{max_seq + 1}.jpg"

    def _draw_labeled_dots(
        self, subdir: str, points: list[list[int]], labels: list[str], color: str
    ) -> str | None:
        """Draws ``labels`` at ``points`` on the screenshot; returns the annotated path.

        Returns None when the screenshot is missing or drawing fails: the
        textual result is still delivered, so one bad annotation never sinks
        the tool call.
        """
        if not self.screenshot_path:
            return None
        try:
            output_path = self._next_output_path(subdir)
            draw_dots(self.screenshot_path, points, labels, str(output_path), color=color)
            return str(output_path)
        except Exception as e:
            logger.warning(f"Failed to draw {subdir} dots: {e}")
            return None

    # ------------------------------------------------------------------ #
    # Screen-index searches
    # ------------------------------------------------------------------ #

    async def _search_ui_helper(self, query: str, prefix: str = "S", color: str = "red") -> dict:
        """Fuzzy text search over the screen index, annotated with labeled dots.

        A strict pass (0.7) keeps the answer short when the label is
        unambiguous; when it yields fewer than three hits a lenient pass
        (0.4) is merged in so near-misses (abbreviations, truncated labels)
        still surface for the model to judge.
        """
        if not len(self.screen_index):
            return {"text": NO_UI_TREE_MESSAGE, "image_path": None}

        matches = list(self.screen_index.search_text(query, 0.7))
        if len(matches) < 3:
            seen = {(m.matched_text, m.element.bounds) for m in matches}
            for m in self.screen_index.search_text(query, 0.4):
                key = (m.matched_text, m.element.bounds)
                if key not in seen:
                    matches.append(m)
                    seen.add(key)

        if not matches:
            return {"text": "No matches found.", "image_path": None}

        points: list[list[int]] = []
        labels: list[str] = []
        text_output: list[str] = []
        for m in matches:
            element = m.element
            m_prefix = "O" if element.source == "ocr" else prefix
            label_id = f"{m_prefix}{self.global_label_idx}"
            self.global_label_idx += 1
            self._register_label(label_id, element)
            cx, cy = element.center
            points.append([cx, cy])
            labels.append(label_id)
            nx, ny = pixel_to_norm(cx, cy, self.width, self.height)
            text_output.append(f"[{label_id}] '{m.matched_text}' at [{nx},{ny}]")

        image_path = self._draw_labeled_dots("search_ui", points, labels, color)
        return {"text": " | ".join(text_output), "image_path": image_path}

    async def _search_by_coords_helper(
        self, nx: int, ny: int, prefix: str = "X", color: str = "blue"
    ) -> dict:
        """Coordinate audit: names what actually sits under a normalized point.

        UI-tree hits come innermost first (see ``ScreenIndex.elements_at``),
        so the label is bound to the node a tap would reach.  The blue dot is
        drawn even without a hit so the model can see where it probed.
        """
        x, y = norm_to_pixel(nx, ny, self.width, self.height)
        found = self.screen_index.elements_at(x, y)

        label_id = f"{prefix}{self.global_label_idx}"
        self.global_label_idx += 1
        image_path = self._draw_labeled_dots("coords_audit", [[x, y]], [label_id], color)

        if not found:
            return {"text": f"No elements found at [{nx},{ny}].", "image_path": image_path}

        self._register_label(label_id, found[0])
        described = " | ".join(element.describe() for element in found)
        return {
            "text": f"Matched element at [{nx},{ny}]: [{label_id}] | {described}",
            "image_path": image_path,
        }

    @trace(type="tool", name="detect_objects")
    async def exec_detect_objects(
        self,
        queries: list[str],
        target_image_id: str = "img_0",
        prefix: str = "D",
        color: str = "red",
    ) -> dict:
        try:
            if not self.screenshot_path:
                return {
                    "text": "Error: Original screenshot is not available.",
                    "image_path": None,
                }
            target_info = self.image_pool.get(target_image_id)
            if not target_info:
                return {
                    "text": (f"Error: {target_image_id} not found in Image Pool."),
                    "image_path": None,
                }

            target_path = target_info["path"]
            transform = target_info["transform"]

            # Load object detector templates
            templates, global_timeout = load_detector_templates()

            result = await _run_object_detection(
                self.ctx,
                target_path,
                queries,
                templates,
                global_timeout=global_timeout,
            )
            try:
                detected_items = result.get("detected", [])
                failed_queries = result.get("failed", [])

                if not detected_items:
                    text_output = ["No objects detected."]
                    if failed_queries:
                        text_output.append(
                            "\n[NOTE: Failed to detect the following queries:"
                            f" {', '.join(failed_queries)}]"
                        )
                    return {
                        "text": "".join(text_output),
                        "image_path": str(target_path),
                    }

                base_dir = settings.TRACES_PATH
                images_dir = base_dir / "images"
                object_detect_dir = images_dir / "object_detect"
                object_detect_dir.mkdir(parents=True, exist_ok=True)

                image_name_safe = self.image_name or "temp_image"
                existing_files = glob.glob(str(object_detect_dir / f"{image_name_safe}_*.jpg"))
                max_seq = 0
                for f in existing_files:
                    match = re.search(r"_(\d+)\.jpg$", f)
                    if match:
                        max_seq = max(max_seq, int(match.group(1)))
                seq = max_seq + 1
                output_path = object_detect_dir / f"{image_name_safe}_{seq}.jpg"

                points = []
                d_labels = []
                text_output = []

                if target_image_id != "img_0":
                    text_output.append(
                        "[NOTE: Coordinates are automatically mapped back to"
                        f" the original {self.width}x{self.height} screen space from"
                        f" {target_image_id}]"
                    )

                for item in detected_items:
                    label_id = f"{prefix}{self.global_label_idx}"
                    self.global_label_idx += 1
                    pos = item.get("point")
                    if pos and isinstance(pos, list) and len(pos) == 2:
                        x_norm, y_norm = pos

                        t_img = cv2.imread(target_path)
                        if t_img is None:
                            raise ValueError(f"Failed to read target image at {target_path}")
                        t_h, t_w = t_img.shape[:2]

                        # Pixels in target image
                        x_target_pixel = (x_norm / 1000) * t_w
                        y_target_pixel = (y_norm / 1000) * t_h

                        # Pixels in original image
                        x_orig_pixel = (x_target_pixel / transform["scale_x"]) + transform[
                            "offset_x"
                        ]
                        y_orig_pixel = (y_target_pixel / transform["scale_y"]) + transform[
                            "offset_y"
                        ]

                        points.append([int(x_orig_pixel), int(y_orig_pixel)])
                        d_labels.append(label_id)

                        # Original image norm
                        x_orig_norm = int(max(0, min(1000, x_orig_pixel * 1000 / self.width)))
                        y_orig_norm = int(max(0, min(1000, y_orig_pixel * 1000 / self.height)))

                        text_output.append(
                            f'[{label_id}] "{item.get("label")}" at [{x_orig_norm},{y_orig_norm}]'
                        )

                draw_dots(
                    self.screenshot_path,
                    points,
                    d_labels,
                    str(output_path),
                    color=color,
                )

                if failed_queries:
                    text_output.append(
                        " | [NOTE: Failed to detect the following queries:"
                        f" {', '.join(failed_queries)}]"
                    )

                return {
                    "text": " | ".join(text_output),
                    "image_path": str(output_path),
                }

            except Exception as e:
                logger.warning(f"Failed to process detection output for annotation: {e}")
                return {"text": str(result), "image_path": None}
        except Exception as e:
            return {
                "text": f"Error: Object detection failed: {e}",
                "image_path": None,
            }

    @trace(type="tool", name="ask_perception_tool")
    async def exec_ask_perception_tool(
        self, search_query: str, nx: int, ny: int, detect_queries: list[str]
    ) -> dict:
        if nx is None or ny is None or not (0 <= nx <= 1000) or not (0 <= ny <= 1000):
            coords_invalid = True
        else:
            coords_invalid = False

        # Run tasks concurrently in parallel!
        tasks = [
            self._search_ui_helper(search_query, prefix="X", color="green"),
            self._search_by_coords_helper(nx, ny, prefix="X", color="blue")
            if not coords_invalid
            else asyncio.sleep(
                0,
                result={
                    "text": "Error: Invalid coordinates format",
                    "image_path": None,
                },
            ),
            self.exec_detect_objects(
                detect_queries, target_image_id="img_0", prefix="D", color="red"
            ),
        ]

        results = await asyncio.gather(*tasks)

        # Consolidate text results
        text_parts = []

        xml_text = results[0].get("text", "No matches found.")
        if xml_text and xml_text != "No matches found.":
            text_parts.append(f"Text Search Results are: {xml_text}")

        coord_text = results[1].get("text", "No elements found.")
        if coord_text and not coord_text.startswith("No elements found"):
            text_parts.append(f"Coordinate Search Results are: {coord_text}")

        obj_text = results[2].get("text", "No objects detected.")
        if obj_text:
            if obj_text.startswith("No objects detected."):
                note_part = obj_text.replace("No objects detected.", "").strip()
                if note_part:
                    text_parts.append(f"Object Detection Results are: {note_part}")
            else:
                text_parts.append(f"Object Detection Results are: {obj_text}")

        if not text_parts:
            unified_text = "No matches or objects found across any perception method."
        else:
            unified_text = ". ".join(text_parts) + "."

        # Collect image paths
        image_paths = []
        for r in results:
            if isinstance(r, dict) and r.get("image_path"):
                image_paths.append(r["image_path"])

        return {
            "text": unified_text,
            "image_paths": image_paths,
        }

    @trace(type="tool", name="ask_image_processor")
    async def exec_ask_image_processor(
        self, instruction: str, target_image_id: str = "img_0"
    ) -> dict:
        coder = ImageProcessor(self.ctx)
        target_info = self.image_pool.get(target_image_id)
        if not target_info:
            return {
                "text": f"Error: {target_image_id} not found in Image Pool.",
                "image_path": None,
            }

        target_path = target_info["path"]
        result = await coder.run(instruction, target_path)

        if not isinstance(result, dict) or "error" in result:
            err_msg = result.get("error") if isinstance(result, dict) else "Unknown error"
            return {
                "text": f"Image Processor failed: {err_msg}",
                "image_paths": [],
            }

        outputs = result.get("outputs", [])
        summary = result.get("summary", "")

        parent_transform = target_info["transform"]
        registered_ids = []
        annotations_sections = []
        image_paths = []

        for entry in outputs:
            new_id = f"img_{self.next_img_id}"
            self.next_img_id += 1

            new_path = entry["path"]
            image_paths.append(new_path)
            transform = entry["transform"]

            final_scale_x = parent_transform["scale_x"] * transform["scale_x"]
            final_scale_y = parent_transform["scale_y"] * transform["scale_y"]
            final_offset_x = (
                transform["offset_x"] / parent_transform["scale_x"]
            ) + parent_transform["offset_x"]
            final_offset_y = (
                transform["offset_y"] / parent_transform["scale_y"]
            ) + parent_transform["offset_y"]

            self.image_pool[new_id] = {
                "path": new_path,
                "transform": {
                    "offset_x": final_offset_x,
                    "offset_y": final_offset_y,
                    "scale_x": final_scale_x,
                    "scale_y": final_scale_y,
                },
                "description": (f"Generated by coder from {target_image_id}: {summary}"),
            }
            registered_ids.append(new_id)

            # Parse and translate annotations
            annotations = entry.get("annotations", {})
            if annotations:
                lines = [f"{new_id}:"]
                for label, coord in sorted(annotations.items(), key=lambda item: item[0]):
                    px, py = coord
                    sx = int(round((px / final_scale_x) + final_offset_x))
                    sy = int(round((py / final_scale_y) + final_offset_y))
                    lines.append(f"  - [{label}]: [{sx}, {sy}]")
                annotations_sections.append("\n".join(lines))

        if registered_ids:
            ids_str = ", ".join(registered_ids)
            text = (
                "Image Processor completed successfully. New image(s) saved as"
                f" {ids_str} in Image Pool. Summary: {summary}"
            )
            if annotations_sections:
                text += "\n\nAnnotations (wrt original screenshot):\n" + "\n".join(
                    annotations_sections
                )
            return {
                "text": text,
                "image_paths": image_paths,
            }
        return {
            "text": "ImageProcessor error: no output images were produced.",
            "image_paths": [],
        }

    @trace(type="tool", name="get_ocr_list")
    async def exec_get_ocr_list(self, prefix: str = "O", color: str = "green") -> dict:
        """Lists every OCR-detected text on the screen with a labeled dot."""
        try:
            ocr_elements = self.screen_index.ocr_elements
            if not ocr_elements:
                return {
                    "text": "No text elements detected on the screen.",
                    "image_path": None,
                }

            points: list[list[int]] = []
            labels: list[str] = []
            text_output: list[str] = []
            for element in ocr_elements:
                cx, cy = element.center
                if not (0 <= cx <= self.width and 0 <= cy <= self.height):
                    continue
                label_id = f"{prefix}{self.global_label_idx}"
                self.global_label_idx += 1
                self._register_label(label_id, element)
                points.append([cx, cy])
                labels.append(label_id)
                nx, ny = pixel_to_norm(cx, cy, self.width, self.height)
                text_output.append(f"[{label_id}] '{element.text}' coords: [{nx},{ny}]")

            if not points:
                return {"text": "No valid OCR results found.", "image_path": None}

            image_path = self._draw_labeled_dots("ocr", points, labels, color)
            return {"text": "\n".join(text_output), "image_path": image_path}

        except Exception as e:
            return {
                "text": f"Error retrieving or processing OCR results: {e}",
                "image_path": None,
            }

    @trace(type="tool", name="inspect_region")
    async def exec_inspect_region(
        self,
        x_min: int,
        y_min: int,
        x_max: int,
        y_max: int,
        zoom_factor: float = 2.0,
    ) -> dict:
        try:
            if not self.screenshot_path:
                return {
                    "text": "Error: Original screenshot is not available.",
                    "image_path": None,
                }
            px_start = int(max(0, min(self.width, x_min * self.width / 1000.0)))
            py_start = int(max(0, min(self.height, y_min * self.height / 1000.0)))
            px_end = int(max(0, min(self.width, x_max * self.width / 1000.0)))
            py_end = int(max(0, min(self.height, y_max * self.height / 1000.0)))

            if px_end <= px_start or py_end <= py_start:
                return {
                    "text": (
                        f"Error: Invalid crop coordinates. x_min={x_min},"
                        f" x_max={x_max}, y_min={y_min}, y_max={y_max}."
                        " Bounding box dimensions must be strictly positive."
                    ),
                    "image_path": None,
                }

            img = cv2.imread(self.screenshot_path)
            if img is None:
                return {
                    "text": (
                        f"Error: Failed to read original screenshot at {self.screenshot_path}"
                    ),
                    "image_path": None,
                }

            cropped = img[py_start:py_end, px_start:px_end]

            if zoom_factor is None:
                zoom_factor = 2.0
            zoom_factor = max(1.0, min(4.0, float(zoom_factor)))

            c_h, c_w = cropped.shape[:2]
            if c_h > 0 and c_w > 0:
                new_w = int(round(c_w * zoom_factor))
                new_h = int(round(c_h * zoom_factor))
                if new_w > 0 and new_h > 0:
                    cropped = cv2.resize(cropped, (new_w, new_h))

            base_dir = settings.TRACES_PATH
            images_dir = base_dir / "images"
            inspect_region_dir = images_dir / "inspect_region"
            inspect_region_dir.mkdir(parents=True, exist_ok=True)

            image_name_safe = self.image_name or "temp_image"
            output_path = inspect_region_dir / f"{image_name_safe}_{self.global_label_idx}.jpg"
            cv2.imwrite(str(output_path), cropped)

            return {
                "text": (f"Inspected region coordinates: [{x_min}, {y_min}] to [{x_max}, {y_max}]"),
                "image_path": str(output_path),
            }

        except Exception as e:
            return {
                "text": f"Error: Region inspection failed: {e}",
                "image_path": None,
            }
