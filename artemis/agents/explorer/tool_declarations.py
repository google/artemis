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
LangChain path) and ``NATIVE_EXPLORER_TOOL_DECLARATIONS`` (google-genai
``FunctionDeclaration`` objects for the native Gemini path).  Both are
re-exported by the ``explorer`` facade module.
"""

from google.genai import types

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


NATIVE_EXPLORER_TOOL_DECLARATIONS = [
    types.FunctionDeclaration(
        name="ask_perception_tool",
        description=(
            "[Perception] Concurrently executes and awaits three sub-tasks"
            " in parallel, including UI-tree text search, coordinate audit,"
            " and visual object detection on the screen.\n\nOutput"
            " Format:\nReturns:'text' (str): A single string consolidating"
            " results with 0-1000 normalized coordinates, including UI"
            " search matches labeled [X],"
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
                    description=("Required. Text query to search for in UI elements."),
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
            " [O1], [O2] etc. in the text output, along with an annotated"
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
                                    "Reference label (e.g. X1, O2, D3, or a number like 3)"
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
