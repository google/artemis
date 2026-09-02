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
Patched collaborators (``settings``, ``draw_dots``, ``search_ui_func``,
``search_by_coordinates_func``, ``_run_object_detection``, ``StorageManager``,
``logger``) are resolved through the facade module at call time; see
``artemis.agents.explorer._facade``.
"""

import asyncio
import glob
import json
from pathlib import Path
import re

import cv2

from artemis.agents.explorer._facade import facade
from artemis.agents.image_processor.image_processor import ImageProcessor
from artemis.data_engine.trace import trace


def load_detector_templates() -> tuple[list, object]:
    """Load object-detector prompt templates and the optional global timeout.

    Shared by ``exec_detect_objects`` and the flash-mode ``run`` path; the two
    call sites historically carried an identical inline copy of this block.
    """
    _ex = facade()
    detector_prompt_path = (
        Path(__file__).parent.parent / "object_detector" / "object_detector.json"
    )
    global_timeout = None
    try:
        with open(detector_prompt_path, encoding="utf-8") as f:
            detector_config = json.load(f)
        detector_templates = detector_config.get("templates", [])
        detector_instructions = detector_config.get("instructions", "")
        global_timeout = detector_config.get("global_timeout", None)
    except Exception as e:
        _ex.logger.warning(f"Failed to load detector prompt config: {e}")
        detector_templates = [
            "Point to the following objects in the provided image: {labels_str}."
        ]
        detector_instructions = ""

    templates = [f"{t}\n\n{detector_instructions}" for t in detector_templates]
    return templates, global_timeout


class PerceptionToolsMixin:
    """Perception/vision tool method group of :class:`Explorer`."""

    async def _search_ui_helper(self, query: str, prefix: str = "S", color: str = "red") -> dict:
        _ex = facade()
        if not self.image_name:
            return {
                "text": (
                    "Error: UI data for the current screen is not found in Data"
                    " Engine. It might not be synced yet."
                ),
                "image_path": None,
            }

        # 1. Try with high precision threshold
        raw_res = _ex.search_ui_func(self.image_name, query, 0.7)
        if "error" in raw_res:
            return {"text": raw_res["error"], "image_path": None}

        matches = raw_res.get("matches", [])

        # 2. If fewer than 3 matches, try with a lower threshold and merge
        if len(matches) < 3:
            raw_res_low = _ex.search_ui_func(self.image_name, query, 0.4)
            if "error" not in raw_res_low:
                low_matches = raw_res_low.get("matches", [])
                existing_keys = {
                    (
                        m.get("matched_text"),
                        tuple(m.get("bounds")) if m.get("bounds") else None,
                    )
                    for m in matches
                }
                for m in low_matches:
                    key = (
                        m.get("matched_text"),
                        tuple(m.get("bounds")) if m.get("bounds") else None,
                    )
                    if key not in existing_keys:
                        matches.append(m)
                        existing_keys.add(key)

        if not matches:
            return {"text": "No matches found.", "image_path": None}

        points = []
        labels = []
        text_output = []

        for m in matches:
            m_type = m.get("type", "xml")
            m_prefix = "O" if m_type == "ocr" else prefix
            label_id = f"{m_prefix}{self.global_label_idx}"
            self.global_label_idx += 1
            bounds = m.get("bounds")
            matched_text = m.get("matched_text", "")

            if bounds and len(bounds) == 4:
                left, top, right, bottom = bounds
                cx_pixel = (left + right) // 2
                cy_pixel = (top + bottom) // 2

                points.append([cx_pixel, cy_pixel])
                labels.append(label_id)

                cx_norm = int(max(0, min(1000, cx_pixel * 1000 / self.width)))
                cy_norm = int(max(0, min(1000, cy_pixel * 1000 / self.height)))

                text_output.append(f"[{label_id}] '{matched_text}' at [{cx_norm},{cy_norm}]")
            else:
                text_output.append(f"[{label_id}] '{matched_text}' at unknown")

        if not points:
            return {"text": " | ".join(text_output), "image_path": None}

        base_dir = _ex.settings.TRACES_PATH
        images_dir = base_dir / "images"
        search_ui_dir = images_dir / "search_ui"
        search_ui_dir.mkdir(parents=True, exist_ok=True)

        existing_files = glob.glob(str(search_ui_dir / f"{self.image_name}_*.jpg"))
        max_seq = 0
        for f in existing_files:
            match = re.search(r"_(\d+)\.jpg$", f)
            if match:
                max_seq = max(max_seq, int(match.group(1)))
        seq = max_seq + 1
        output_path = search_ui_dir / f"{self.image_name}_{seq}.jpg"

        try:
            _ex.draw_dots(
                self.screenshot_path,
                points,
                labels,
                str(output_path),
                color=color,
            )
            image_path = str(output_path)
        except Exception as e:
            _ex.logger.warning(f"Failed to draw dots: {e}")
            image_path = None

        return {"text": " | ".join(text_output), "image_path": image_path}

    async def _search_by_coords_helper(
        self, nx: int, ny: int, prefix: str = "X", color: str = "blue"
    ) -> dict:
        _ex = facade()
        if not self.image_name:
            return {
                "text": (
                    "Error: UI data for the current screen is not found in Data"
                    " Engine. It might not be synced yet."
                ),
                "image_path": None,
            }

        x = int(max(0, min(self.width, nx * self.width / 1000)))
        y = int(max(0, min(self.height, ny * self.height / 1000)))
        raw_res = _ex.search_by_coordinates_func(self.image_name, x, y)

        label_id = f"{prefix}{self.global_label_idx}"
        self.global_label_idx += 1

        # Draw a dot at the query coordinates
        base_dir = _ex.settings.TRACES_PATH
        images_dir = base_dir / "images"
        coords_audit_dir = images_dir / "coords_audit"
        coords_audit_dir.mkdir(parents=True, exist_ok=True)

        existing_files = glob.glob(str(coords_audit_dir / f"{self.image_name}_*.jpg"))
        max_seq = 0
        for f in existing_files:
            match = re.search(r"_(\d+)\.jpg$", f)
            if match:
                max_seq = max(max_seq, int(match.group(1)))
        seq = max_seq + 1
        output_path = coords_audit_dir / f"{self.image_name}_{seq}.jpg"

        try:
            _ex.draw_dots(
                self.screenshot_path,
                [[x, y]],
                [label_id],
                str(output_path),
                color=color,
            )
            image_path = str(output_path)
        except Exception as e:
            _ex.logger.warning(f"Failed to draw blue dots: {e}")
            image_path = None

        raw_res_str = str(raw_res).replace("\n", " | ").replace(" |  | ", " | ")
        return {
            "text": (f"Matched element at [{nx},{ny}]: [{label_id}] | {raw_res_str}"),
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
        _ex = facade()
        try:
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

            result = await _ex._run_object_detection(
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

                base_dir = _ex.settings.TRACES_PATH
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
                        " the original 1080x2400 screen space from"
                        f" {target_image_id}]"
                    )

                for item in detected_items:
                    label_id = f"{prefix}{self.global_label_idx}"
                    self.global_label_idx += 1
                    pos = item.get("point")
                    if pos and isinstance(pos, list) and len(pos) == 2:
                        x_norm, y_norm = pos

                        t_img = cv2.imread(target_path)
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

                _ex.draw_dots(
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
                _ex.logger.warning(f"Failed to process detection output for annotation: {e}")
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
        if coord_text and coord_text != "No elements found.":
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
        else:
            return {
                "text": f"ImageProcessor error: {err_msg}",
                "image_paths": [],
            }

        output_path = result.get("output_path")
        trans = result.get("transform")
        new_image_id = f"img_{len(self.image_pool)}"
        if output_path and trans:
            self.image_pool[new_image_id] = {
                "path": output_path,
                "transform": trans,
            }

        text_msg = (
            f"Image processed successfully. New image saved as"
            f" '{new_image_id}'. You can now use '{new_image_id}' as"
            " 'target_image_id' in subsequent tool calls."
        )
        return {
            "text": text_msg,
            "image_path": output_path,
            "image_id": new_image_id,
        }

    @trace(type="tool", name="get_ocr_list")
    async def exec_get_ocr_list(self, prefix: str = "O", color: str = "green") -> dict:
        _ex = facade()
        if not self.image_name:
            return {
                "text": ("Error: UI data for the current screen is not found in Data Engine."),
                "image_path": None,
            }

        try:
            db_path = _ex.settings.DATA_ENGINE_DB_PATH
            base_dir = _ex.settings.TRACES_PATH
            storage = _ex.StorageManager(db_path=str(db_path), base_dir=base_dir)

            record = storage.get_ui_record(self.image_name)
            if not record or not record.ocr_result:
                return {
                    "text": "No text elements detected on the screen.",
                    "image_path": None,
                }

            points = []
            labels = []
            text_output = []

            for item in record.ocr_result:
                text = item.get("text", "").strip()
                bounds = item.get("bounds", [])
                if text and len(bounds) == 4:
                    left, top, right, bottom = bounds
                    cx = (left + right) // 2
                    cy = (top + bottom) // 2
                    if 0 <= cx <= self.width and 0 <= cy <= self.height:
                        label_id = f"{prefix}{self.global_label_idx}"
                        self.global_label_idx += 1
                        points.append([cx, cy])
                        labels.append(label_id)

                        cx_norm = int(max(0, min(1000, cx * 1000 / self.width)))
                        cy_norm = int(max(0, min(1000, cy * 1000 / self.height)))

                        text_output.append(f"[{label_id}] '{text}' coords: [{cx_norm},{cy_norm}]")

            if not points:
                return {
                    "text": "No valid OCR results found.",
                    "image_path": None,
                }

            images_dir = base_dir / "images"
            ocr_dir = images_dir / "ocr"
            ocr_dir.mkdir(parents=True, exist_ok=True)

            output_path = ocr_dir / f"{self.image_name}_{self.global_label_idx}.jpg"
            _ex.draw_dots(self.screenshot_path, points, labels, str(output_path), color=color)

            return {
                "text": "\n".join(text_output),
                "image_path": str(output_path),
            }

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
        _ex = facade()
        try:
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

            base_dir = _ex.settings.TRACES_PATH
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
