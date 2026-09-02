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

"""Tool schema declarations for the video analyzer.

Holds both the OpenAI-style function schemas used by the universal
(LangChain) engine and the builders for the native Gemini
``FunctionDeclaration`` equivalents.  Pure data/declarations only.
"""

from typing import Any

from google.genai import types

UNIVERSAL_SUBMIT_ANSWER_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "submit_answer",
        "description": "Submit the final findings for the requested segment analysis.",
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "A short paragraph summarizing your findings.",
                },
                "analysis": {
                    "type": "string",
                    "description": "A short paragraph of detailed analysis.",
                },
                "timeline_events": {
                    "type": "array",
                    "description": (
                        "List of detected events matching the query. If no relevant"
                        " events occurred, return an empty array."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "start_time": {
                                "type": "number",
                                "description": "Event start time (seconds)",
                            },
                            "end_time": {
                                "type": "number",
                                "description": "Event end time (seconds)",
                            },
                            "transcription": {
                                "type": "string",
                                "description": (
                                    "A concise transcription of what happened in the event."
                                ),
                            },
                            "confidence_score": {
                                "type": "number",
                                "description": "Confidence score between 0.0 and 1.0",
                            },
                            "verification_timestamp_secs": {
                                "type": "number",
                                "description": (
                                    "Exact timestamp (seconds) for verification screenshot."
                                ),
                            },
                        },
                        "required": [
                            "start_time",
                            "end_time",
                            "transcription",
                            "confidence_score",
                            "verification_timestamp_secs",
                        ],
                    },
                },
            },
            "required": ["summary", "analysis"],
        },
    },
}

UNIVERSAL_MAIN_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "extract_segment_metadata",
            "description": (
                "Retrieves metadata (duration_seconds, file_size_mb) for a video segment."
                " Does not return video content or perform content analysis."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_time": {
                        "type": "number",
                        "description": "Start time in seconds",
                    },
                    "end_time": {
                        "type": "number",
                        "description": (
                            "End time in seconds (optional, defaults to latest available time)"
                        ),
                    },
                },
                "required": ["start_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spawn_sub_agent",
            "description": (
                "Spawns a sub-agent to analyze visual and audio content of a video segment."
                " Parallelizes chunk analysis across workers. Returns combined summary."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_time": {
                        "type": "number",
                        "description": "Start time in seconds",
                    },
                    "end_time": {
                        "type": "number",
                        "description": "End time in seconds",
                    },
                    "specific_query": {
                        "type": "string",
                        "description": "Specific query or intent for the sub-agent",
                    },
                },
                "required": ["start_time", "specific_query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_audio_only",
            "description": (
                "Extracts and analyzes only the audio track from a video segment."
                " More efficient for non-visual tasks."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_time": {
                        "type": "number",
                        "description": "Start time in seconds",
                    },
                    "end_time": {
                        "type": "number",
                        "description": "End time in seconds (optional)",
                    },
                    "specific_query": {
                        "type": "string",
                        "description": "Specific query or intent for the audio content",
                    },
                },
                "required": ["start_time", "specific_query"],
            },
        },
    },
]


def build_native_tools_declaration() -> list[types.FunctionDeclaration]:
    """Builds the native Gemini declarations for the coordinator tools."""
    return [
        types.FunctionDeclaration(
            name="extract_segment_metadata",
            description=(
                "Retrieves metadata (such as duration_seconds and"
                " file_size_mb) for a specific video segment. Does not"
                " return the video content or perform content analysis."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "start_time": types.Schema(
                        type=types.Type.NUMBER,
                        description="Start time in seconds",
                    ),
                    "end_time": types.Schema(
                        type=types.Type.NUMBER,
                        description=(
                            "End time in seconds (optional, defaults to latest available time)"
                        ),
                    ),
                },
                required=["start_time"],
            ),
        ),
        types.FunctionDeclaration(
            name="spawn_sub_agent",
            description=(
                "Spawns a sub-agent to analyze both visual and audio"
                " content of a video segment. Parallelizes chunk analysis"
                " across workers. Returns combined summary and analysis."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "start_time": types.Schema(
                        type=types.Type.NUMBER,
                        description="Start time in seconds",
                    ),
                    "end_time": types.Schema(
                        type=types.Type.NUMBER,
                        description=(
                            "End time in seconds. Use the maximum available"
                            " time if not specified."
                        ),
                    ),
                    "specific_query": types.Schema(
                        type=types.Type.STRING,
                        description=("Specific query or intent for the sub-agent"),
                    ),
                },
                required=["start_time", "specific_query"],
            ),
        ),
        types.FunctionDeclaration(
            name="analyze_audio_only",
            description=(
                "Extracts and analyzes only the audio track from a video"
                " segment. More efficient for non-visual tasks."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "start_time": types.Schema(
                        type=types.Type.NUMBER,
                        description="Start time in seconds",
                    ),
                    "end_time": types.Schema(
                        type=types.Type.NUMBER,
                        description="End time in seconds (optional)",
                    ),
                    "specific_query": types.Schema(
                        type=types.Type.STRING,
                        description=("Specific query or intent for the audio content"),
                    ),
                },
                required=["start_time", "specific_query"],
            ),
        ),
    ]


def build_submit_answer_declaration() -> types.FunctionDeclaration:
    """Builds the native Gemini declaration for the sub-agent submit_answer tool."""
    return types.FunctionDeclaration(
        name="submit_answer",
        description=("Submit the final findings for the requested segment analysis."),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "summary": types.Schema(
                    type=types.Type.STRING,
                    description=("A short paragraph summarizing of your findings."),
                ),
                "analysis": types.Schema(
                    type=types.Type.STRING,
                    description="A short paragraph of detailed analysis.",
                ),
                "timeline_events": types.Schema(
                    type=types.Type.ARRAY,
                    description=(
                        "List of detected events matching the query. If no"
                        " relevant events occurred in the segment, return"
                        " an empty array."
                    ),
                    items=types.Schema(
                        type=types.Type.OBJECT,
                        properties={
                            "start_time": types.Schema(
                                type=types.Type.NUMBER,
                                description="Event start time (seconds)",
                            ),
                            "end_time": types.Schema(
                                type=types.Type.NUMBER,
                                description="Event end time (seconds)",
                            ),
                            "transcription": types.Schema(
                                type=types.Type.STRING,
                                description=(
                                    "A concise transcription of what happened in the event.  "
                                ),
                            ),
                            "confidence_score": types.Schema(
                                type=types.Type.NUMBER,
                                description=("Confidence score between 0 and 1"),
                            ),
                            "verification_timestamp_secs": types.Schema(
                                type=types.Type.NUMBER,
                                description=(
                                    "Exact timestamp (seconds) for verification screenshot."
                                ),
                            ),
                        },
                        required=[
                            "start_time",
                            "end_time",
                            "transcription",
                            "confidence_score",
                            "verification_timestamp_secs",
                        ],
                    ),
                ),
            },
            required=["summary", "analysis", "timeline_events"],
        ),
    )
