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
import base64
import glob
import hashlib
import json
import os
from pathlib import Path
import random
import re
import time
from typing import Any, Literal

import cv2
from google import genai
from google.genai import types
from google.genai.errors import APIError
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
from artemis.services.llm import get_llm

# Import diagnostic functions directly
from artemis.agents.explorer.constants import EXPLORE_DESCRIPTIONS
from artemis.mcp.xml_search_server import (
    search_by_coordinates as search_by_coordinates_func,
    search_ui as search_ui_func,
)
from artemis.utils.logger import get_logger
from artemis.utils.ocr_api import perform_ocr
from artemis.utils.ocr_xml_fusion import fuse_ocr_with_xml
from artemis.utils.visualization import draw_dots, format_minimal_list_with_points

logger = get_logger(__name__)

UNIVERSAL_EXPLORER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "ask_perception_tool",
            "description": (
                "[Perception] Concurrently executes and awaits three sub-tasks in parallel,"
                " including database search, coordinate search, and visual object detection."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "search_query": {
                        "type": "string",
                        "description": "Required. Text query to search for in XML/OCR elements.",
                    },
                    "nx": {
                        "type": "integer",
                        "description": "Required. Normalized X coordinate (0-1000 range).",
                    },
                    "ny": {
                        "type": "integer",
                        "description": "Required. Normalized Y coordinate (0-1000 range).",
                    },
                    "detect_queries": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Required. List of query strings to visually detect.",
                    },
                },
                "required": ["search_query", "nx", "ny", "detect_queries"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "detect_objects",
            "description": (
                "[Perception] Locates elements, shapes and more using powerful VLM."
                " Returns their normalized coordinates (0-1000 scale) with label dots."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_image_id": {
                        "type": "string",
                        "description": "The ID of the image to process.",
                    },
                    "queries": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "A list of single query strings to detect on the screen.",
                    },
                },
                "required": ["queries", "target_image_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_ocr_list",
            "description": (
                "[Perception] Retrieves all text elements detected on the screen via OCR."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_image_processor",
            "description": ("[Transformation] A scripting tool for pixel-level image processing."),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_image_id": {
                        "type": "string",
                        "description": "The ID of the image in the Image Pool to start from.",
                    },
                    "instruction": {
                        "type": "string",
                        "description": "1-2 precise commands describing a simple task.",
                    },
                },
                "required": ["instruction", "target_image_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_region",
            "description": (
                "[Perception] Crops and resizes a bounding box region of the original screenshot."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "x_min": {"type": "integer", "description": "Left X coordinate (0-1000)."},
                    "y_min": {"type": "integer", "description": "Top Y coordinate (0-1000)."},
                    "x_max": {"type": "integer", "description": "Right X coordinate (0-1000)."},
                    "y_max": {"type": "integer", "description": "Bottom Y coordinate (0-1000)."},
                    "zoom_factor": {
                        "type": "number",
                        "description": "Zoom factor to upscale the cropped region (1.0 to 4.0).",
                    },
                },
                "required": ["x_min", "y_min", "x_max", "y_max", "zoom_factor"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_answer",
            "description": (
                "[Finalization] Submits the final list of ranked candidate UI elements matching"
                " the search query, or a fallback message if no elements were found."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "candidates": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {
                                    "type": "string",
                                    "description": "Reference label (e.g. S1, D2, T3)",
                                },
                                "description": {
                                    "type": "string",
                                    "description": "Brief description of this candidate element",
                                },
                                "coords": {
                                    "type": "array",
                                    "items": {"type": "integer"},
                                    "description": (
                                        "[nx, ny] coordinates in normalized 0-1000 scale"
                                    ),
                                },
                            },
                            "required": ["label", "coords"],
                        },
                        "description": "A ranked list of candidate UI elements matching the query.",
                    },
                    "fallback_message": {
                        "type": "string",
                        "description": (
                            "Optional fallback explanation if target elements were not found."
                        ),
                    },
                },
                "required": ["candidates"],
            },
        },
    },
]


class Explorer:
    BLACKLIST_TEMPLATE = (
        "\n# TOOL BLACKLIST\n- The following tools are blacklisted and cannot be used: {tools}\n"
    )
    TOOLS = [
        types.FunctionDeclaration(
            name="ask_perception_tool",
            description=(
                "[Perception] Concurrently executes and awaits three sub-tasks"
                " in parallel, including database search, coordinate search,"
                " and visual object detection on the screen.\n\nOutput"
                " Format:\nReturns:'text' (str): A single string consolidating"
                " results with 0-1000 normalized coordinates, including xml"
                " search matches labeled [X], ocr search matches labeled [O],"
                " coordinate search matches labeled [X], and visually detected"
                " object targets labeled [D].\n(Annotated screenshots are also"
                " added to the context containing corresponding colored label"
                " dots)."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "search_query": types.Schema(
                        type=types.Type.STRING,
                        description=("Required. Text query to search for in XML/OCR elements."),
                    ),
                    "nx": types.Schema(
                        type=types.Type.INTEGER,
                        description=(
                            "Required. Normalized X coordinate (0-1000 range)"
                            " to audit for overlapping elements."
                        ),
                    ),
                    "ny": types.Schema(
                        type=types.Type.INTEGER,
                        description=(
                            "Required. Normalized Y coordinate (0-1000 range)"
                            " to audit for overlapping elements."
                        ),
                    ),
                    "detect_queries": types.Schema(
                        type=types.Type.ARRAY,
                        items=types.Schema(type=types.Type.STRING),
                        description=(
                            "Required. List of query strings to visually detect on the screen."
                        ),
                    ),
                },
                required=["search_query", "nx", "ny", "detect_queries"],
            ),
        ),
        types.FunctionDeclaration(
            name="detect_objects",
            description=(
                "[Perception] Locates elements, shapes and more using powerful"
                " VLM. Returns their normalized coordinates (in a [0, 1000]"
                " scale) labeled with [D1], [D2] etc. in the text output, along"
                " with an annotated image containing corresponding visual label"
                " dots."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "target_image_id": types.Schema(
                        type=types.Type.STRING,
                        description="The ID of the image to process.",
                    ),
                    "queries": types.Schema(
                        type=types.Type.ARRAY,
                        items=types.Schema(type=types.Type.STRING),
                        description=("A list of single query strings to detect on the screen."),
                    ),
                },
                required=["queries", "target_image_id"],
            ),
        ),
        types.FunctionDeclaration(
            name="get_ocr_list",
            description=(
                "[Perception] Retrieves all text elements detected on the"
                " original, unprocessed screen via OCR. Returns their"
                " normalized coordinates (in a [0, 1000] scale) labeled with"
                " [T1], [T2] etc. in the text output, along with an annotated"
                " image containing corresponding visual label dots."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={},
            ),
        ),
        types.FunctionDeclaration(
            name="ask_image_processor",
            description=(
                "[Transformation] A scripting tool for pixel-level image"
                " processing. CAUTION: This tool is slow and expensive. Returns"
                " a summary of the operations performed and new image IDs,"
                " along with new labels and screen coordinates."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "target_image_id": types.Schema(
                        type=types.Type.STRING,
                        description=(
                            "The ID of the image in the Image Pool to start"
                            " from (e.g. 'img_0', 'img_1')."
                        ),
                    ),
                    "instruction": types.Schema(
                        type=types.Type.STRING,
                        description=(
                            "1-2 precise commands describing a simple task."
                            " Include all necessary coordinates and other"
                            " parameters.\n"
                        ),
                    ),
                },
                required=["instruction", "target_image_id"],
            ),
        ),
        types.FunctionDeclaration(
            name="submit_answer",
            description=(
                "[Finalization] Submits the final list of ranked candidate UI"
                " elements matching the search query, or a fallback message if"
                " no elements were found, to complete the search task. Fails"
                " validation if: (1) candidates is empty and no"
                " fallback_message is provided, (2) coordinates are not exactly"
                " 2 integers [nx, ny], or (3) coordinates lie outside"
                " normalized [0, 1000] range."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "candidates": types.Schema(
                        type=types.Type.ARRAY,
                        items=types.Schema(
                            type=types.Type.OBJECT,
                            properties={
                                "label": types.Schema(
                                    type=types.Type.STRING,
                                    description=(
                                        "Reference label (e.g., S1, D2, T3, or number like 3)"
                                    ),
                                ),
                                "coords": types.Schema(
                                    type=types.Type.ARRAY,
                                    items=types.Schema(type=types.Type.INTEGER),
                                    description=("[nx, ny] coordinates in normalized 0-1000 scale"),
                                ),
                                "description": types.Schema(
                                    type=types.Type.STRING,
                                    description=("Brief description of this candidate element"),
                                ),
                            },
                            required=["label", "coords"],
                        ),
                        description=(
                            "A ranked list of candidate UI elements that match"
                            " the query, sorted from highest to lowest"
                            " confidence (maximum 10 candidates). Leave empty"
                            " if no matching elements were found on the screen."
                        ),
                    ),
                    "fallback_message": types.Schema(
                        type=types.Type.STRING,
                        description=(
                            "Optional fallback explanation if target elements"
                            " were not found or if there are special"
                            " observations."
                        ),
                    ),
                },
                required=["candidates"],
            ),
        ),
        types.FunctionDeclaration(
            name="inspect_region",
            description=(
                "[Perception] Crops and resizes a bounding box region of the"
                " original screenshot. Returns the cropped and resized image"
                " directly, without any image ID or coordinate transform."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "x_min": types.Schema(
                        type=types.Type.INTEGER,
                        description="Left X coordinate (0-1000).",
                    ),
                    "y_min": types.Schema(
                        type=types.Type.INTEGER,
                        description="Top Y coordinate (0-1000).",
                    ),
                    "x_max": types.Schema(
                        type=types.Type.INTEGER,
                        description="Right X coordinate (0-1000).",
                    ),
                    "y_max": types.Schema(
                        type=types.Type.INTEGER,
                        description="Bottom Y coordinate (0-1000).",
                    ),
                    "zoom_factor": types.Schema(
                        type=types.Type.NUMBER,
                        description=("Zoom factor to upscale the cropped region (1.0 to 4.0)."),
                    ),
                },
                required=["x_min", "y_min", "x_max", "y_max", "zoom_factor"],
            ),
        ),
    ]

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
            blacklisted_config = (
                getattr(agent_cfg, "blacklisted_tools", {}).get("explorer", []) if agent_cfg else []
            )
            self.blacklisted_tools = set(blacklisted_config)
        except (TypeError, AttributeError):
            self.blacklisted_tools = set()
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
            llm_cfg.model if (llm_cfg and hasattr(llm_cfg, "model")) else "gemini-3.6-flash"
        ).lower()
        self.model_name = (
            llm_cfg.model if (llm_cfg and hasattr(llm_cfg, "model")) else "gemini-3.6-flash"
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
        """Returns the list of tools, filtering out blacklisted ones."""
        tools = [tool for tool in self.TOOLS if tool.name not in self.blacklisted_tools]
        if only_submit:
            tools = [tool for tool in tools if tool.name == "submit_answer"]
        return tools

    async def _search_ui_helper(self, query: str, prefix: str = "S", color: str = "red") -> dict:
        if not self.image_name:
            return {
                "text": (
                    "Error: UI data for the current screen is not found in Data"
                    " Engine. It might not be synced yet."
                ),
                "image_path": None,
            }

        # 1. Try with high precision threshold
        raw_res = search_ui_func(self.image_name, query, 0.7)
        if "error" in raw_res:
            return {"text": raw_res["error"], "image_path": None}

        matches = raw_res.get("matches", [])

        # 2. If fewer than 3 matches, try with a lower threshold and merge
        if len(matches) < 3:
            raw_res_low = search_ui_func(self.image_name, query, 0.4)
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

        base_dir = settings.TRACES_PATH
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
            draw_dots(
                self.screenshot_path,
                points,
                labels,
                str(output_path),
                color=color,
            )
            image_path = str(output_path)
        except Exception as e:
            logger.warning(f"Failed to draw dots: {e}")
            image_path = None

        return {"text": " | ".join(text_output), "image_path": image_path}

    async def _search_by_coords_helper(
        self, nx: int, ny: int, prefix: str = "X", color: str = "blue"
    ) -> dict:
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
        raw_res = search_by_coordinates_func(self.image_name, x, y)

        label_id = f"{prefix}{self.global_label_idx}"
        self.global_label_idx += 1

        # Draw a dot at the query coordinates
        base_dir = settings.TRACES_PATH
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
            draw_dots(
                self.screenshot_path,
                [[x, y]],
                [label_id],
                str(output_path),
                color=color,
            )
            image_path = str(output_path)
        except Exception as e:
            logger.warning(f"Failed to draw blue dots: {e}")
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
                logger.warning(f"Failed to load detector prompt config: {e}")
                detector_templates = [
                    "Point to the following objects in the provided image: {labels_str}."
                ]
                detector_instructions = ""

            templates = [f"{t}\n\n{detector_instructions}" for t in detector_templates]

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
            text_parts.append(f"XML/OCR Text Search Results are: {xml_text}")

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
        if not self.image_name:
            return {
                "text": ("Error: UI data for the current screen is not found in Data Engine."),
                "image_path": None,
            }

        try:
            db_path = settings.DATA_ENGINE_DB_PATH
            base_dir = settings.TRACES_PATH
            storage = StorageManager(db_path=str(db_path), base_dir=base_dir)

            record = storage.get_ui_record(self.image_name)
            if not record or not record.ocr_result:
                return {
                    "text": ("No OCR results found in Data Engine for the current screen."),
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
            draw_dots(self.screenshot_path, points, labels, str(output_path), color=color)

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

    async def _run_universal(
        self,
        query: str,
        context_feedback: str,
        screenshot_path: str,
        state: State,
        minimal_list: str,
        version: str,
        prompt_template: str,
        max_iterations: int,
    ) -> str:
        """Executes Explorer reasoning loop via Universal LangChain ChatModel."""
        llm = get_llm(self.ctx, name="explorer")

        # Filter universal tools based on blacklist
        exposed_tools = [
            t
            for t in UNIVERSAL_EXPLORER_TOOLS
            if t["function"]["name"] not in self.blacklisted_tools
        ]
        bound_llm = llm.bind_tools(exposed_tools)

        with open(screenshot_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("utf-8")

        user_content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    f"Operator Request:\n- Query: {query}\n"
                    f"- Context Feedback: {context_feedback}\n\n"
                    "Initial marked UI elements list:\n"
                    f"{minimal_list}\n"
                ),
            },
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
            },
        ]

        messages: list[BaseMessage] = [
            SystemMessage(content=prompt_template),
            HumanMessage(content=user_content),
        ]

        iterations = 0
        agent_outcome = ""

        while iterations < max_iterations:
            iterations += 1
            is_final_turn = iterations == max_iterations

            if is_final_turn:
                messages.append(
                    HumanMessage(
                        content=(
                            "[WARNING] This is your final iteration. You MUST"
                            " call 'submit_answer' to submit your final result."
                        )
                    )
                )
                submit_only_tools = [
                    t for t in exposed_tools if t["function"]["name"] == "submit_answer"
                ]
                current_llm = llm.bind_tools(submit_only_tools) if submit_only_tools else bound_llm
            else:
                current_llm = bound_llm

            response = await asyncio.wait_for(current_llm.ainvoke(messages), timeout=180)
            messages.append(response)

            tool_calls = getattr(response, "tool_calls", None) or []
            if not tool_calls:
                logger.warning(
                    f"Universal Explorer iteration {iterations}: No tool calls generated."
                )
                if is_final_turn:
                    return json.dumps(
                        {
                            "candidates": [],
                            "fallback_message": str(response.content) or "No candidates found.",
                        },
                        ensure_ascii=False,
                    )
                continue

            # Check for submit_answer
            submit_call = next((tc for tc in tool_calls if tc.get("name") == "submit_answer"), None)
            if submit_call:
                args = submit_call.get("args", {})
                candidates = args.get("candidates", [])
                fallback_message = args.get("fallback_message", "")

                # Validate coordinates
                valid_candidates = []
                for cand in candidates:
                    if isinstance(cand, dict) and "coords" in cand:
                        coords = cand["coords"]
                        if isinstance(coords, list) and len(coords) == 2:
                            try:
                                nx, ny = int(coords[0]), int(coords[1])
                                if 0 <= nx <= 1000 and 0 <= ny <= 1000:
                                    valid_candidates.append(cand)
                            except (ValueError, TypeError):
                                pass

                agent_outcome = json.dumps(
                    {
                        "candidates": valid_candidates,
                        "fallback_message": fallback_message,
                    },
                    ensure_ascii=False,
                )
                return agent_outcome

            # Execute non-submit tools
            for tc in tool_calls:
                name = tc.get("name")
                args = tc.get("args", {})
                call_id = tc.get("id", f"call_{iterations}_{name}")

                if name in self.blacklisted_tools:
                    messages.append(
                        ToolMessage(
                            tool_call_id=call_id,
                            name=name,
                            content=f"Tool '{name}' is blacklisted and unavailable.",
                        )
                    )
                    continue

                try:
                    if name == "ask_perception_tool":
                        res = await self.exec_ask_perception_tool(
                            search_query=args.get("search_query"),
                            nx=args.get("nx"),
                            ny=args.get("ny"),
                            detect_queries=args.get("detect_queries"),
                        )
                    elif name == "detect_objects":
                        res = await self.exec_detect_objects(
                            queries=args.get("queries"),
                            target_image_id=args.get("target_image_id", "img_0"),
                        )
                    elif name == "get_ocr_list":
                        res = await self.exec_get_ocr_list()
                    elif name == "ask_image_processor":
                        res = await self.exec_ask_image_processor(
                            instruction=args.get("instruction"),
                            target_image_id=args.get("target_image_id", "img_0"),
                        )
                    elif name == "inspect_region":
                        res = await self.exec_inspect_region(
                            x_min=args.get("x_min"),
                            y_min=args.get("y_min"),
                            x_max=args.get("x_max"),
                            y_max=args.get("y_max"),
                            zoom_factor=args.get("zoom_factor", 2.0),
                        )
                    else:
                        res = {"text": f"Error: Tool '{name}' is not recognized."}

                    res_text = res.get("text") or res.get("result") or str(res)
                    messages.append(ToolMessage(tool_call_id=call_id, name=name, content=res_text))

                except Exception as tool_err:
                    logger.error(f"Error executing tool '{name}': {tool_err}")
                    messages.append(
                        ToolMessage(
                            tool_call_id=call_id,
                            name=name,
                            content=f"Error executing tool '{name}': {tool_err}",
                        )
                    )

        if not agent_outcome:
            agent_outcome = json.dumps(
                {
                    "candidates": [],
                    "fallback_message": (
                        "Explorer reached max iterations without finding candidates."
                    ),
                },
                ensure_ascii=False,
            )

        return agent_outcome

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
        ctx = self.ctx
        if version == "flash":
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
                logger.warning(f"Failed to load detector prompt config: {e}")
                detector_templates = [
                    "Point to the following objects in the provided image: {labels_str}."
                ]
                detector_instructions = ""

            templates = [f"{t}\n\n{detector_instructions}" for t in detector_templates]

            self.screenshot_path = screenshot_path
            self.image_name = None

            queries = [q.strip() for q in query.split("|") if q.strip()]

            try:
                result = await _run_object_detection(
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
                    {
                        "candidates": candidates,
                        "fallback_message": fallback_message,
                    },
                    ensure_ascii=False,
                )
            except Exception as e:
                logger.error(f"Flash mode object detection failed: {e}")
                return json.dumps(
                    {
                        "candidates": [],
                        "fallback_message": f"Flash mode detection error: {e}",
                    },
                    ensure_ascii=False,
                )

        self.http_client = httpx.AsyncClient()

        # 1. Compute current image hash and verify with Data Engine
        image_name = None
        record = None
        try:
            sha256_hash = hashlib.sha256()
            with open(screenshot_path, "rb") as f:
                for byte_block in iter(lambda: f.read(4096), b""):
                    sha256_hash.update(byte_block)
            computed_hash = sha256_hash.hexdigest()
            logger.info(f"Computed screenshot hash: {computed_hash}")

            # Check if it exists in DB
            db_path = settings.DATA_ENGINE_DB_PATH
            base_dir = settings.TRACES_PATH
            storage = StorageManager(db_path, base_dir)
            record = storage.get_image(computed_hash)

            if record:
                image_name = computed_hash
            else:
                logger.warning(
                    f"Image hash {computed_hash} not found in Data Engine DB."
                    " Data Engine might not be synced yet."
                )

        except Exception as e:
            logger.warning(f"Failed to compute hash or check DB: {e}")

        self.image_name = image_name
        self.screenshot_path = screenshot_path

        # Resolve parameters
        mode = version.capitalize()
        version_blacklist = set()
        if version == "ultra":
            max_iterations = 8
        elif version == "pro":
            max_iterations = 3
            version_blacklist = {
                "ask_image_processor",
                "get_ocr_list",
                "inspect_region",
                "detect_objects",
            }
        else:
            max_iterations = 3
            version_blacklist = {
                "ask_image_processor",
                "get_ocr_list",
                "inspect_region",
            }

        try:
            blacklisted_config = (
                self.ctx.agent_config.blacklisted_tools.get("explorer", [])
                if self.ctx.agent_config
                else []
            )
            self.blacklisted_tools = version_blacklist.union(set(blacklisted_config))
        except (TypeError, AttributeError):
            self.blacklisted_tools = version_blacklist

        # 2. Prepare Native Tools declarations
        self.width = 1080
        self.height = 2400
        operator_raw_data = getattr(state, "operator_raw_data", {}) or {}
        w_raw = operator_raw_data.get("width")
        h_raw = operator_raw_data.get("height")
        if isinstance(w_raw, int) and isinstance(h_raw, int):
            self.width = w_raw
            self.height = h_raw
        else:
            if ctx.device and getattr(ctx.device, "device_width", None):
                self.width = ctx.device.device_width
            if ctx.device and getattr(ctx.device, "device_height", None):
                self.height = ctx.device.device_height

        # Initialize Image Pool
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

        # 4. Initialize or reuse dynamic context-level GenAI client for connection pooling
        client = getattr(ctx, "_genai_client", None)
        if client is None:
            logger.info(
                "Initializing new GenAI client on context for connection pooling (Explorer)..."
            )
            client = genai.Client(
                api_key=settings.GOOGLE_API_KEY.get_secret_value()
                if settings.GOOGLE_API_KEY
                else None
            )
            ctx._genai_client = client

        llm_cfg = ctx.llm_config.explorer
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
            model_name = "gemini-3.6-flash"

        # 5. Construct Prompt & Initial Message List
        prompt_path = Path(__file__).parent / "explorer.json"
        if not prompt_path.exists():
            return "Error: Explorer prompt template not found."

        try:
            content = prompt_path.read_text(encoding="utf-8")
            content_no_comments = re.sub(r"(?<!:)\/\/.*", "", content)
            content_no_comments = re.sub(r"/\*.*?\*/", "", content_no_comments, flags=re.DOTALL)
            content_no_comments = re.sub(r",\s*([\]}])", r"\1", content_no_comments)
            data = json.loads(content_no_comments)
        except Exception as e:
            return f"Error loading or parsing explorer.json: {e}"

        prompt_parts = []
        for section, content_val in data.items():
            prompt_parts.append(f"# {section}")
            if isinstance(content_val, list):
                for bullet in content_val:
                    prompt_parts.append(f"- {bullet}")
            else:
                prompt_parts.append(content_val)
            prompt_parts.append("")

        prompt_template = "\n".join(prompt_parts)
        prompt_template += "\n{blakclist}"
        if self.blacklisted_tools:
            blacklist_section = self.BLACKLIST_TEMPLATE.format(
                tools=", ".join(sorted(self.blacklisted_tools))
            )
        else:
            blacklist_section = ""
        prompt_template = prompt_template.replace("{blakclist}", blacklist_section)

        explore_info = EXPLORE_DESCRIPTIONS.get(version, EXPLORE_DESCRIPTIONS["pro"])
        version_prompt = explore_info.get("version_prompt")
        if version_prompt:
            version_prompt = version_prompt.format(mode=mode, max_iterations=max_iterations)
            prompt_template += f"\n# EXECUTION CONSTRAINT\n- {version_prompt}\n"

        # Generate initial visual annotations if minimal_list is empty
        marked_path = None
        if not minimal_list:
            fused_xml = []
            if record and getattr(record, "ui_tree", None):
                ui_tree = record.ui_tree
                ocr_results = getattr(record, "ocr_result", None)
                if ocr_results is None:
                    try:
                        logger.info("Previous screenshot OCR is missing. Running OCR on-the-fly...")
                        with open(screenshot_path, "rb") as img_file:
                            img_b64 = base64.b64encode(img_file.read()).decode("utf-8")
                        ocr_results = await perform_ocr(img_b64, client=self.http_client)
                    except Exception as ocr_err:
                        logger.error(f"On-the-fly OCR failed for previous screenshot: {ocr_err}")
                        ocr_results = []

                fused_xml = fuse_ocr_with_xml(ui_tree, ocr_results or [])
                logger.info("Successfully loaded and fused UI hierarchy for previous screenshot.")
            elif hasattr(state, "latest_ui_hierarchy"):
                if screenshot_path == getattr(state, "latest_screenshot", None):
                    fused_xml = state.latest_ui_hierarchy

            if fused_xml:
                try:
                    logger.info(
                        "Explorer self-annotating initial screenshot using latest_ui_hierarchy..."
                    )
                    formatted_list, points, labels = format_minimal_list_with_points(
                        fused_xml, self.width, self.height
                    )
                    minimal_list = formatted_list
                    self.global_label_idx = len(points) + 1

                    base_dir = (
                        Path(ctx.data_engine.base_dir)
                        if ctx.data_engine and getattr(ctx.data_engine, "base_dir", None)
                        else None
                    )
                    if not base_dir:
                        db_path = settings.DATA_ENGINE_DB_PATH
                        base_dir = settings.TRACES_PATH
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
                    draw_dots(screenshot_path, points, labels, str(marked_path))
                    logger.info(
                        f"Successfully drew {len(points)} dots and saved marked"
                        f" image to {marked_path}"
                    )
                except Exception as e:
                    logger.error(f"Failed to self-annotate initial screenshot: {e}")

        if marked_path and os.path.exists(str(marked_path)):
            img_to_read = str(marked_path)
        else:
            img_to_read = screenshot_path

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

        use_file_api = os.getenv("ARTEMIS_USE_FILE_API", "false").lower() == "true"
        uploaded_files = []

        def get_image_part(file_path: str) -> types.Part:
            if use_file_api:
                file_ref = client.files.upload(file=file_path)
                uploaded_files.append(file_ref)
                return types.Part(
                    file_data=types.FileData(
                        file_uri=file_ref.uri,
                        mime_type=file_ref.mime_type or "image/jpeg",
                    )
                )
            else:
                with open(file_path, "rb") as f:
                    img_bytes = f.read()
                return types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg")

        cached_content = None
        try:
            # Upload initial screenshot
            initial_part = get_image_part(img_to_read)

            contents = [
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_text(
                            text=f"""Operator Request:
- Query: {query}
- Context Feedback: {context_feedback}

Initial marked UI elements list (corresponding to numbers ①, ②... in the image):
{minimal_list}
"""
                        ),
                        initial_part,
                    ],
                )
            ]

            # Caching initialization
            if enable_caching is None:
                env_cache = os.getenv("ARTEMIS_EXPLORER_CACHING", "").lower()
                if env_cache in ["true", "false"]:
                    enable_caching = env_cache == "true"
                else:
                    enable_caching = getattr(settings, "EXPLORER_CACHING", True)

            cached_content = None
            if enable_caching:
                try:
                    logger.info("Creating cache resource for Explorer...")
                    cached_content = client.caches.create(
                        model=model_name,
                        config=types.CreateCachedContentConfig(
                            contents=[contents[0]],
                            system_instruction=prompt_template,
                            tools=[types.Tool(function_declarations=self.get_exposed_tools())],
                            tool_config=types.ToolConfig(
                                function_calling_config=types.FunctionCallingConfig(
                                    mode=types.FunctionCallingConfigMode.ANY
                                ),
                                include_server_side_tool_invocations=True,
                            ),
                            ttl="300s",
                        ),
                    )
                    logger.info(f"Cache resource created successfully: {cached_content.name}")
                except Exception as cache_err:
                    logger.error(f"Failed to create cache resource: {cache_err}")
                    cached_content = None

            # 6. Execution Loop (Native SDK Tools Dispatching)
            iterations = 0
            agent_outcome = ""
            self.turn_latencies = []
            self.turn_cached_tokens = []
            self.trace_history = []

            while iterations < max_iterations:
                iterations += 1
                logger.info(f"Iteration {iterations}: Invoking Native Gemini SDK for Explorer...")

                self._prune_historical_images(contents, keep_last=1)

                is_final_turn = iterations == max_iterations
                if is_final_turn:
                    warning_msg = (
                        "\n[WARNING] This is your final iteration. You MUST"
                        " call 'submit_answer' to submit your final result. No"
                        " other tools are available."
                    )
                    contents.append(
                        types.Content(
                            role="user",
                            parts=[types.Part.from_text(text=warning_msg)],
                        )
                    )

                start_turn = time.perf_counter()
                with TraceSpan(name="gemini_explorer_call", ctx=ctx) as span:
                    if cached_content and not is_final_turn:
                        generate_config = types.GenerateContentConfig(
                            cached_content=cached_content.name,
                            temperature=temperature,
                            safety_settings=SAFETY_SETTINGS_BLOCK_NONE,
                            **(
                                {
                                    "thinking_config": types.ThinkingConfig(
                                        thinking_level=thinking_level
                                    )
                                }
                                if thinking_level
                                else {}
                            ),
                        )
                    else:
                        generate_config = types.GenerateContentConfig(
                            system_instruction=prompt_template,
                            temperature=temperature,
                            safety_settings=SAFETY_SETTINGS_BLOCK_NONE,
                            tools=[
                                types.Tool(
                                    function_declarations=self.get_exposed_tools(
                                        only_submit=is_final_turn
                                    )
                                )
                            ],
                            tool_config=types.ToolConfig(
                                function_calling_config=types.FunctionCallingConfig(
                                    mode=types.FunctionCallingConfigMode.ANY
                                ),
                                include_server_side_tool_invocations=True,
                            ),
                            **(
                                {
                                    "thinking_config": types.ThinkingConfig(
                                        thinking_level=thinking_level
                                    )
                                }
                                if thinking_level
                                else {}
                            ),
                        )

                    max_call_retries = 5
                    for attempt in range(max_call_retries + 1):
                        try:
                            response = await asyncio.wait_for(
                                client.aio.models.generate_content(
                                    model=model_name,
                                    contents=contents,
                                    config=generate_config,
                                ),
                                timeout=180,
                            )
                            break
                        except APIError as api_err:
                            code = getattr(api_err, "code", None)
                            if code in [429, 503]:
                                if attempt >= max_call_retries:
                                    raise api_err
                                backoff = (2.0**attempt) + random.uniform(0.1, 1.0)
                                logger.warning(
                                    f"APIError {code} (overload/rate limit) on"
                                    f" attempt {attempt + 1}. Retrying in"
                                    f" {backoff:.2f}s..."
                                )
                                await asyncio.sleep(backoff)
                            else:
                                raise api_err
                        except Exception as e:
                            if attempt >= max_call_retries:
                                raise e
                            backoff = (2.0**attempt) + random.uniform(0.1, 1.0)
                            logger.warning(
                                f"Unexpected error on attempt {attempt + 1}:"
                                f" {e}. Retrying in {backoff:.2f}s..."
                            )
                            await asyncio.sleep(backoff)
                    end_turn = time.perf_counter()
                    turn_dur = end_turn - start_turn
                    self.turn_latencies.append(turn_dur)

                    cached_tokens = 0
                    if getattr(response, "usage_metadata", None) and getattr(
                        response.usage_metadata,
                        "cached_content_token_count",
                        None,
                    ):
                        cached_tokens = response.usage_metadata.cached_content_token_count
                    self.turn_cached_tokens.append(cached_tokens)
                    span.result = (
                        f"Function calls: {len(response.function_calls)}"
                        if response.function_calls
                        else "Final answer"
                    )
                    # Extract and log model thoughts and text if present
                    thinking_parts = []
                    text_parts = []
                    candidates = getattr(response, "candidates", None)
                    if not candidates and isinstance(response, dict):
                        candidates = response.get("candidates")

                    if candidates and len(candidates) > 0:
                        candidate = candidates[0]
                        content = getattr(candidate, "content", None)
                        if not content and isinstance(candidate, dict):
                            content = candidate.get("content")

                        parts = getattr(content, "parts", None)
                        if not parts and isinstance(content, dict):
                            parts = content.get("parts")

                        if parts:
                            for part in parts:
                                is_thought = False
                                part_text = None
                                if isinstance(part, dict):
                                    is_thought = part.get("thought", False)
                                    part_text = part.get("text", None)
                                else:
                                    is_thought = getattr(part, "thought", False)
                                    part_text = getattr(part, "text", None)

                                if is_thought and part_text:
                                    thinking_parts.append(part_text)
                                elif part_text:
                                    text_parts.append(part_text)
                    if thinking_parts:
                        span.payload["explorer_thought"] = "\n".join(thinking_parts)
                    if text_parts:
                        span.payload["explorer_text"] = "\n".join(text_parts)

                turn_record = {
                    "iteration": iterations,
                    "thoughts": ("\n".join(thinking_parts) if thinking_parts else ""),
                    "tool_calls": [],
                }

                function_calls = response.function_calls
                if not function_calls:
                    logger.warning(
                        f"Explorer iteration {iterations}: Model hallucinated"
                        " plain text. Forcing retry."
                    )
                    turn_record["tool_calls"].append(
                        {
                            "name": "hallucinated_plain_text",
                            "args": {"text": "\n".join(text_parts) if text_parts else ""},
                            "response": {
                                "error": (
                                    "Forced retry because model failed to call"
                                    " submit_answer or any other tool"
                                )
                            },
                        }
                    )
                    self.trace_history.append(turn_record)
                    contents.append(response.candidates[0].content)
                    contents.append(
                        types.Content(
                            role="user",
                            parts=[
                                types.Part.from_text(
                                    text=(
                                        "You have not called 'submit_answer'"
                                        " to submit your final answer yet. If"
                                        " you are not certain about the final"
                                        " answer, you can continue exploring"
                                        f" {max_iterations - iterations} more"
                                        " time(s)."
                                    )
                                )
                            ],
                        )
                    )
                    continue

                # Record model's function call request in history
                if response.candidates and response.candidates[0].content:
                    contents.append(response.candidates[0].content)
                else:
                    contents.append(
                        types.Content(
                            role="model",
                            parts=[types.Part(function_call=fc) for fc in function_calls],
                        )
                    )

                # Execute function calls
                tool_response_parts = []

                # 6.1 Intercept and validate submit_answer to manage task lifecycle
                submit_call = next(
                    (
                        fc
                        for fc in function_calls
                        if (fc.name.split(":")[-1] if ":" in fc.name else fc.name)
                        == "submit_answer"
                    ),
                    None,
                )
                if submit_call:
                    args = submit_call.args or {}
                    candidates = args.get("candidates", [])
                    errors = []

                    if len(function_calls) > 1:
                        errors.append(
                            "The tool 'submit_answer' MUST be called"
                            " individually in your final turn without any other"
                            " parallel tool calls. Please remove other tool"
                            " calls and re-submit."
                        )

                    fallback_message = args.get("fallback_message", "")
                    if not candidates and not fallback_message:
                        errors.append(
                            "The candidates list is empty. You must provide at"
                            " least one candidate matching the query, or"
                            " explain observations via fallback_message."
                        )

                    for i, cand in enumerate(candidates):
                        label = cand.get("label")
                        coords = cand.get("coords")
                        if not label:
                            errors.append(f"Candidate at index {i} is missing a 'label'.")
                        if not coords or not isinstance(coords, list) or len(coords) != 2:
                            errors.append(
                                f"Candidate '{label or i}' must have 'coords'"
                                " as an array of exactly 2 integers `[nx,"
                                " ny]`."
                            )
                        else:
                            try:
                                nx, ny = int(coords[0]), int(coords[1])
                                if not (0 <= nx <= 1000) or not (0 <= ny <= 1000):
                                    errors.append(
                                        f"Candidate '{label or i}' coordinates"
                                        f" `[{nx}, {ny}]` must strictly be in"
                                        " the `[0-1000]` normalized scale"
                                        " range inclusive."
                                    )
                            except (ValueError, TypeError, IndexError):
                                errors.append(
                                    f"Candidate '{label or i}' coordinates are invalid integers."
                                )

                    if errors:
                        logger.warning(f"Explorer submit_answer validation failed: {errors}")
                        turn_record["tool_calls"].append(
                            {
                                "name": "submit_answer",
                                "args": args,
                                "response": {"error": ("Validation Failed:\n" + "\n".join(errors))},
                            }
                        )
                        tool_response_parts.append(
                            types.Part.from_function_response(
                                name="submit_answer",
                                response={
                                    "error": (
                                        "Validation Failed:\n"
                                        + "\n".join(errors)
                                        + "\nPlease correct these formatting"
                                        " errors and re-submit using"
                                        " submit_answer."
                                    )
                                },
                            )
                        )
                        # Allow ReAct loop to continue so LLM can self-correct and re-submit
                    else:
                        logger.info("Explorer submit_answer validation passed successfully!")
                        agent_outcome = json.dumps(args, ensure_ascii=False)
                        turn_record["tool_calls"].append(
                            {
                                "name": "submit_answer",
                                "args": args,
                                "response": {"result": "success"},
                            }
                        )
                        self.trace_history.append(turn_record)
                        break

                for fc in function_calls:
                    name = fc.name.split(":")[-1] if ":" in fc.name else fc.name
                    if name == "submit_answer":
                        continue
                    args = fc.args or {}
                    tool_call_trace = {
                        "name": name,
                        "args": args,
                    }
                    if name in self.blacklisted_tools:
                        logger.warning(
                            "Explorer attempted to call blacklisted tool"
                            f" '{name}'. Blocking execution."
                        )
                        tool_call_trace["response"] = {
                            "error": (f"Tool '{name}' is blacklisted and unavailable.")
                        }
                        turn_record["tool_calls"].append(tool_call_trace)
                        tool_response_parts.append(
                            types.Part.from_function_response(
                                name=fc.name,
                                response={
                                    "error": (f"Tool '{name}' is blacklisted and unavailable.")
                                },
                            )
                        )
                        continue

                    logger.info(f"Explorer executing tool '{name}' sequentially...")

                    try:
                        if name == "ask_perception_tool":
                            res = await self.exec_ask_perception_tool(
                                search_query=args.get("search_query"),
                                nx=args.get("nx"),
                                ny=args.get("ny"),
                                detect_queries=args.get("detect_queries"),
                            )
                            tool_call_trace["response"] = res
                            tool_response_parts.append(
                                types.Part.from_function_response(
                                    name=name,
                                    response={
                                        "text": res.get("text"),
                                    },
                                )
                            )
                            if res.get("image_paths"):
                                for img_p in res["image_paths"]:
                                    try:
                                        tool_response_parts.append(get_image_part(img_p))
                                    except Exception as e:
                                        logger.warning(
                                            f"Failed to load image response part for {img_p}: {e}"
                                        )

                        elif name == "detect_objects":
                            res = await self.exec_detect_objects(
                                queries=args.get("queries"),
                                target_image_id=args.get("target_image_id", "img_0"),
                            )
                            tool_call_trace["response"] = res

                            if res.get("image_path"):
                                tool_response_parts.append(
                                    types.Part.from_function_response(
                                        name=name,
                                        response={
                                            "result": res.get("text"),
                                        },
                                    )
                                )
                                tool_response_parts.append(get_image_part(res["image_path"]))
                            else:
                                tool_response_parts.append(
                                    types.Part.from_function_response(
                                        name=name,
                                        response={"result": res.get("text")},
                                    )
                                )

                        elif name == "ask_image_processor":
                            res = await self.exec_ask_image_processor(
                                instruction=args.get("instruction"),
                                target_image_id=args.get("target_image_id", "img_0"),
                            )
                            tool_call_trace["response"] = res

                            tool_response_parts.append(
                                types.Part.from_function_response(
                                    name=name,
                                    response={"result": res.get("text")},
                                )
                            )
                            if res.get("image_paths"):
                                for img_p in res["image_paths"]:
                                    try:
                                        tool_response_parts.append(get_image_part(img_p))
                                    except Exception as e:
                                        logger.warning(
                                            "Failed to upload image path"
                                            f" {img_p} for ask_image_processor"
                                            f" tool response: {e}"
                                        )

                        elif name == "get_ocr_list":
                            res = await self.exec_get_ocr_list()
                            tool_call_trace["response"] = res

                            if res.get("image_path"):
                                tool_response_parts.append(
                                    types.Part.from_function_response(
                                        name=name,
                                        response={
                                            "result": res.get("text"),
                                        },
                                    )
                                )
                                tool_response_parts.append(get_image_part(res["image_path"]))
                            else:
                                tool_response_parts.append(
                                    types.Part.from_function_response(
                                        name=name,
                                        response={"result": res.get("text")},
                                    )
                                )

                        elif name == "inspect_region":
                            res = await self.exec_inspect_region(
                                x_min=args.get("x_min"),
                                y_min=args.get("y_min"),
                                x_max=args.get("x_max"),
                                y_max=args.get("y_max"),
                                zoom_factor=args.get("zoom_factor"),
                            )
                            tool_call_trace["response"] = res

                            if res.get("image_path"):
                                tool_response_parts.append(
                                    types.Part.from_function_response(
                                        name=name,
                                        response={
                                            "result": res.get("text"),
                                        },
                                    )
                                )
                                tool_response_parts.append(get_image_part(res["image_path"]))
                            else:
                                tool_response_parts.append(
                                    types.Part.from_function_response(
                                        name=name,
                                        response={"result": res.get("text")},
                                    )
                                )
                        else:
                            tool_call_trace["response"] = {"error": f"Tool {name} not found"}
                            tool_response_parts.append(
                                types.Part.from_function_response(
                                    name=name,
                                    response={"error": f"Tool {name} not found"},
                                )
                            )
                    except Exception as e:
                        logger.error(f"Explorer tool {name} execution failed: {e}")
                        tool_call_trace["response"] = {"error": str(e)}
                        tool_response_parts.append(
                            types.Part.from_function_response(name=name, response={"error": str(e)})
                        )
                    finally:
                        turn_record["tool_calls"].append(tool_call_trace)

                if tool_response_parts:
                    # Native Gemini SDK allows 'tool' role to return mixed parts
                    contents.append(types.Content(role="user", parts=tool_response_parts))

                self.trace_history.append(turn_record)

            if iterations >= max_iterations and not agent_outcome:
                agent_outcome = (
                    "Error: Explorer reached maximum iterations without a conclusive answer."
                )

        except Exception as e:
            logger.error(f"Explorer execution loop failed: {e}")

            err_msg = str(e)
            if "preempted" in err_msg.lower():
                clean_msg = (
                    "Model request was preempted by the server due to high"
                    " demand. Please try again later."
                )
            elif "overloaded" in err_msg.lower() or "503" in err_msg:
                clean_msg = "Model service is currently overloaded. Please try again later."
            elif "quota" in err_msg.lower() or "429" in err_msg:
                clean_msg = "Model API quota exceeded or rate limited."
            elif "thinkingconfig" in err_msg.lower():
                clean_msg = (
                    "Model configuration error: ThinkingConfig is not"
                    f" supported for model {model_name}."
                )
            else:
                clean_msg = f"Explorer agent execution failed: {err_msg}"

            agent_outcome = json.dumps(
                {"candidates": [], "fallback_message": clean_msg},
                ensure_ascii=False,
            )
        finally:
            if self.http_client:
                try:
                    await self.http_client.aclose()
                    logger.info("Closed Explorer HTTP client.")
                except Exception as close_err:
                    logger.warning(f"Failed to close Explorer HTTP client: {close_err}")
            if cached_content:
                try:
                    logger.info(f"Deleting cache resource: {cached_content.name}")
                    client.caches.delete(name=cached_content.name)
                except Exception as cleanup_cache_err:
                    logger.warning(f"Failed to delete cache resource: {cleanup_cache_err}")
            for file_ref in uploaded_files:
                try:
                    client.files.delete(name=file_ref.name)
                except Exception as cleanup_err:
                    logger.warning(f"Failed to delete uploaded file {file_ref.name}: {cleanup_err}")
            logger.info(f"Explorer turn latencies: {self.turn_latencies}")

        return agent_outcome
