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

"""Static tool declarations for the Explorer agent.

Split out of ``artemis.agents.explorer.explorer`` as pure data:
``UNIVERSAL_EXPLORER_TOOLS`` (OpenAI-style dict schemas for the universal
LangChain path).
"""

UNIVERSAL_EXPLORER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "ask_perception_tool",
            "description": (
                "[Perception] Concurrently executes and awaits three sub-tasks in parallel,"
                " including UI-tree text search, coordinate audit, and visual object detection."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "search_query": {
                        "type": "string",
                        "description": "Required. Text query to search for in UI elements.",
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
                "[Perception] Locates elements, shapes and more using a vision-language model."
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
                                    "description": "Reference label (e.g. X1, O2, D3, or a number like 3)",
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
