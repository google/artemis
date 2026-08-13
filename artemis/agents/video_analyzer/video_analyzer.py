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
from datetime import datetime
import glob
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import tempfile
import time
from typing import Any

from google import genai
from google.genai import types
from google.genai.errors import APIError
from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from artemis.agents.video_analyzer.conflict_resolution import (
    ConflictResolutionService,
)
from artemis.config import settings
from artemis.constants import SAFETY_SETTINGS_BLOCK_NONE
from artemis.context import ArtemisContext
from artemis.controllers.controller_factory import get_controller
from artemis.data_engine.trace import CURRENT_TRACE_ID, TraceSpan, trace
from artemis.services.llm import get_llm
from artemis.utils.logger import get_logger
from artemis.utils.video import (
    compress_video_for_api,
    extract_audio_from_video,
    extract_keyframes_from_video,
)

try:
    from datetime import UTC
except ImportError:
    UTC = UTC

logger = get_logger(__name__)

TRANSCODE_SEMAPHORE = asyncio.Semaphore(2)
MAIN_AGENT_SEMAPHORE = asyncio.Semaphore(2)
API_SEMAPHORE = asyncio.Semaphore(5)
SLOWDOWN_THRESHOLD_SECONDS = 30.0
SLOWDOWN_FACTOR = 2.0
_LAST_CLEANUP_TIME = 0.0
_CLEANUP_LOCK = asyncio.Lock()


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


async def cleanup_abandoned_gemini_files(client) -> None:
    """Scan and delete remaining cloud video files whose creation time has elapsed standard TTL."""
    global _LAST_CLEANUP_TIME
    if _CLEANUP_LOCK.locked():
        return

    async with _CLEANUP_LOCK:
        now_ts = time.time()
        if now_ts - _LAST_CLEANUP_TIME < 3600:
            return
        _LAST_CLEANUP_TIME = now_ts

    try:
        pager = await asyncio.wait_for(client.aio.files.list(), timeout=30)
        files = []
        async for f in pager:
            files.append(f)
        now = datetime.now(UTC)
        files_to_delete = []

        for f in files:
            display_name = getattr(f, "display_name", "") or ""

            if (
                display_name.startswith("compressed_")
                or display_name.startswith("audio_")
                or "artemis" in display_name
            ):
                create_time = getattr(f, "created_at", None) or getattr(f, "create_time", None)
                if not create_time:
                    continue

                if isinstance(create_time, str):
                    try:
                        parsed_time = datetime.fromisoformat(create_time.replace("Z", "+00:00"))
                    except Exception:
                        continue
                elif isinstance(create_time, datetime):
                    parsed_time = create_time
                else:
                    continue

                if parsed_time.tzinfo is None:
                    parsed_time = parsed_time.replace(tzinfo=UTC)

                age_seconds = (now - parsed_time).total_seconds()

                if age_seconds > 7200:
                    files_to_delete.append(f)
                    logger.info(
                        f"Marked expired cloud asset for deletion: {f.name}"
                        f" ({display_name}), Age: {age_seconds / 3600:.1f}h"
                    )

        if files_to_delete:
            logger.info(f"Purging {len(files_to_delete)} expired cloud assets in parallel...")
            tasks = [
                asyncio.wait_for(client.aio.files.delete(name=f.name), timeout=30)
                for f in files_to_delete
            ]
            await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as e:
        logger.warning(f"Routine cloud maintenance skipped: {e}")


class VideoAnalyzer:
    def __init__(self, ctx: ArtemisContext):
        self.ctx = ctx
        self.blackboard_entries = []
        agent_config = getattr(ctx, "agent_config", None)
        self.enable_ledger = getattr(agent_config, "enable_video_ledger", True)
        self.local_files_to_cleanup = set()
        self.local_dirs_to_cleanup = set()
        self.cloud_files_to_cleanup = set()

        sub_prompt_path = Path(__file__).parent / "video_sub_agent.md"
        self.sub_system_prompt = (
            sub_prompt_path.read_text(encoding="utf-8") if sub_prompt_path.exists() else ""
        )

        audio_prompt_path = Path(__file__).parent / "video_sub_agent_audio.md"
        self.audio_system_prompt = (
            audio_prompt_path.read_text(encoding="utf-8") if audio_prompt_path.exists() else ""
        )

        self._init_tools()
        self._init_engine()

    def _init_tools(self) -> None:
        """Initializes native and sub-agent tool declarations."""
        self.tools_declaration = [
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

        self.submit_answer_tool = types.FunctionDeclaration(
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

    def _init_engine(self) -> None:
        """Initializes model engine and decides whether to use native Gemini or Universal path."""
        ctx = self.ctx
        llm_config = getattr(ctx, "llm_config", None)
        utils_cfg = getattr(llm_config, "utils", None) if llm_config else None
        llm_cfg = getattr(utils_cfg, "video_analyzer", None) if utils_cfg else None
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

        # If client is already set on ctx or explicit Gemini configuration
        self.client = getattr(ctx, "_genai_client", None)
        if self.client is not None:
            self.use_native_gemini = True
        elif has_google_key and is_gemini_model:
            try:
                self.client = genai.Client(api_key=settings.GOOGLE_API_KEY.get_secret_value())
                ctx._genai_client = self.client
                self.use_native_gemini = True
                asyncio.create_task(cleanup_abandoned_gemini_files(self.client))
            except Exception as e:
                logger.warning(
                    f"Failed to initialize native Gemini client: {e}. Using universal engine."
                )
                self.use_native_gemini = False
        else:
            self.use_native_gemini = False

    @property
    def blackboard_ledger(self) -> str:
        self.blackboard_entries = ConflictResolutionService.clean(self.blackboard_entries)
        if not self.blackboard_entries:
            return "No video segments analyzed yet."
        lines = []
        for e in self.blackboard_entries:
            lines.append(f"{e['start']}s - {e['end']}s: {e['summary']}")
        return " | ".join(lines)

    async def upload_and_poll_file(self, compressed_path: Path) -> any:
        logger.info(f"Uploading {compressed_path} to Gemini File API...")
        file_size_mb = compressed_path.stat().st_size / (1024 * 1024)
        upload_timeout = max(30.0, min(120.0, file_size_mb * 2.0))

        file = await asyncio.wait_for(
            self.client.aio.files.upload(file=compressed_path),
            timeout=upload_timeout,
        )
        self.cloud_files_to_cleanup.add(file.name)

        max_wait = max(60, min(180, 60 + int(file_size_mb * 2)))

        wait_interval = 0.5
        start_wait = time.time()
        retry_count = 0
        max_retries = 3

        while True:
            try:
                f_state = await asyncio.wait_for(
                    self.client.aio.files.get(name=file.name), timeout=20
                )
                retry_count = 0
            except Exception as poll_error:
                retry_count += 1
                logger.warning(
                    f"Temporary issue polling file {file.name} (attempt"
                    f" {retry_count}/{max_retries}): {poll_error}"
                )
                if retry_count > max_retries:
                    raise RuntimeError(
                        f"Failed to poll Gemini File API after {max_retries} attempts: {poll_error}"
                    )
                await asyncio.sleep(2.0)
                continue

            if f_state.state.name == "ACTIVE":
                break
            elif f_state.state.name == "FAILED":
                raise RuntimeError(f"Gemini File API processing failed for {file.name}")

            if time.time() - start_wait > max_wait:
                raise TimeoutError(
                    f"Gemini File API processing timeout for {file.name}"
                    f" (Waited {max_wait}s for size {file_size_mb:.1f}MB)"
                )

            logger.info(f"File {file.name} is {f_state.state.name}, waiting {wait_interval}s...")
            await asyncio.sleep(wait_interval)
            wait_interval = min(3.0, wait_interval * 1.5)
        return file

    def get_overlapping_warnings(self, start_time: float, end_time: float | None) -> list[dict]:
        warnings = []
        if not isinstance(start_time, (int, float)) or (
            end_time is not None and not isinstance(end_time, (int, float))
        ):
            return warnings
        if end_time is None or (end_time - start_time) <= 0:
            return warnings
        req_duration = end_time - start_time
        for entry in self.blackboard_entries:
            if isinstance(entry, dict) and entry.get("end") != "unknown":
                try:
                    if not isinstance(entry.get("start"), (int, float, str)) or not isinstance(
                        entry.get("end"), (int, float, str)
                    ):
                        continue
                    overlap_start = max(start_time, float(entry["start"]))
                    overlap_end = min(end_time, float(entry["end"]))
                    overlap_duration = max(0.0, overlap_end - overlap_start)
                    if (overlap_duration / req_duration) > 0.8:
                        warnings.append(
                            {
                                "target": entry.get("target", ""),
                                "summary": entry.get("summary", ""),
                            }
                        )
                except (ValueError, TypeError):
                    pass
        return warnings

    @trace(type="tool", name="extract_segment_metadata")
    async def exec_extract_segment_metadata(
        self, start_time: float, end_time: float | None = None
    ) -> str:
        controller = get_controller(self.ctx)
        async with TRANSCODE_SEMAPHORE:
            result = await controller.extract_segment_metadata(start_time, end_time)
        if result.success and result.video_path:
            path = Path(result.video_path)
            self.local_files_to_cleanup.add(path)
            self.local_dirs_to_cleanup.add(path.parent)
            size_mb = (
                result.file_size_mb
                if result.file_size_mb is not None
                else (path.stat().st_size / (1024 * 1024) if path.exists() else 0.0)
            )

            if result.duration_seconds is not None:
                duration = result.duration_seconds
            elif end_time is not None:
                duration = end_time - start_time
            else:
                try:
                    duration_cmd = [
                        "ffprobe",
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "default=noprint_wrappers=1:nokey=1",
                        str(path),
                    ]
                    proc = await asyncio.create_subprocess_exec(
                        *duration_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout, _ = await proc.communicate()
                    duration = float(stdout.decode().strip())
                except Exception as e:
                    logger.warning(f"Failed to resolve duration via ffprobe for {path}: {e}")
                    duration = 0.0
            if isinstance(duration, (int, float)):
                duration = round(duration, 1)

            return json.dumps(
                {
                    "duration_seconds": duration,
                    "file_size_mb": (
                        round(size_mb, 2) if isinstance(size_mb, (int, float)) else size_mb
                    ),
                }
            )
        raise RuntimeError(f"Failed to get video segment: {result.message}")

    @trace(type="tool", name="spawn_sub_agent")
    async def exec_spawn_sub_agent(
        self, start_time: float, end_time: float | None, specific_query: str
    ) -> str:

        controller = get_controller(self.ctx)
        async with TRANSCODE_SEMAPHORE:
            metadata_result = await controller.extract_segment_metadata(start_time, end_time)
        if not metadata_result.success:
            return f"Error fetching segment metadata: {metadata_result.message}"

        duration = getattr(metadata_result, "duration_seconds", None)
        if not isinstance(duration, (int, float)):
            duration = (
                (end_time - start_time)
                if (isinstance(end_time, (int, float)) and isinstance(start_time, (int, float)))
                else 60.0
            )
        if isinstance(duration, (int, float)):
            duration = round(duration, 1)
        target_chunk_size = 60.0
        num_workers = min(5, math.ceil(duration / target_chunk_size))
        actual_chunk_size = duration / num_workers
        chunks = []
        for i in range(num_workers):
            cs = round(start_time + (i * actual_chunk_size), 1)
            ce = (
                round(cs + actual_chunk_size, 1)
                if i < num_workers - 1
                else round(start_time + duration, 1)
            )
            chunks.append((cs, ce))
        logger.info(
            f"Dynamically chunking video into {num_workers} workers. Chunk"
            f" size: {actual_chunk_size:.1f}s"
        )

        async def bounded_spawn(cs, ce, query):
            async with API_SEMAPHORE:
                return await self._exec_single_chunk(cs, ce, query)

        tasks = [bounded_spawn(cs, ce, specific_query) for cs, ce in chunks]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        full_text_logs = []
        for chunk, res in zip(chunks, results):
            if isinstance(res, Exception):
                full_text_logs.append(f"Chunk {chunk[0]:.1f}s - {chunk[1]:.1f}s FAILED: {res}")
                logger.error(f"Chunk failed: {res}")
            else:
                full_text_logs.append(f"Chunk {chunk[0]:.1f}s - {chunk[1]:.1f}s {res}")

        # Apply NMS to the ledger now that all chunks have appended their events
        self.blackboard_entries = ConflictResolutionService.clean(self.blackboard_entries)

        valid_results = [res for res in results if isinstance(res, str)]
        if not valid_results:
            return "All sub-agent chunks failed."
        return " ".join(valid_results)

    async def _exec_single_chunk(
        self, start_time: float, end_time: float | None, specific_query: str
    ) -> str:
        max_retries = 2
        current_start = round(start_time, 1) if isinstance(start_time, (int, float)) else start_time
        current_end = round(end_time, 1) if isinstance(end_time, (int, float)) else end_time

        for attempt in range(max_retries + 1):
            try:
                logger.info(
                    f"Attempt {attempt + 1}/{max_retries + 1} for sub-agent:"
                    f" start={current_start}, end={current_end}"
                )
                controller = get_controller(self.ctx)
                async with TRANSCODE_SEMAPHORE:
                    result = await controller.extract_segment_metadata(current_start, current_end)
                if not result.success or not result.video_path:
                    raise Exception(f"Failed to get video segment: {result.message}")

                path = Path(result.video_path)
                self.local_files_to_cleanup.add(path)
                self.local_dirs_to_cleanup.add(path.parent)

                actual_start = getattr(result, "actual_start_relative_time", current_start)
                if not isinstance(actual_start, (int, float)):
                    actual_start = current_start

                slowdown_factor = 1.0
                if (
                    current_end is not None
                    and (current_end - current_start) <= SLOWDOWN_THRESHOLD_SECONDS
                ):
                    slowdown_factor = SLOWDOWN_FACTOR

                async with TRANSCODE_SEMAPHORE:
                    compressed_path = await compress_video_for_api(
                        path,
                        force_compress=True,
                        start_offset_seconds=actual_start,
                        slowdown_factor=slowdown_factor,
                    )
                if compressed_path != path:
                    self.local_files_to_cleanup.add(compressed_path)
                    self.local_dirs_to_cleanup.add(compressed_path.parent)

                actual_start = getattr(result, "actual_start_relative_time", current_start)
                if not isinstance(actual_start, (int, float)):
                    actual_start = current_start

                warnings = self.get_overlapping_warnings(start_time, end_time)
                warning_block = ""
                if warnings:
                    lines = [
                        "WARNING: The following queries were already tried in this timeframe:\n"
                    ]
                    for w in warnings:
                        lines.append(f"Searched for: {w['target']} -> {w['summary']}")
                    warning_block = "\n".join(lines) + "\n\n"

                duration_secs = getattr(result, "duration_seconds", None)
                actual_end = (
                    actual_start + duration_secs
                    if isinstance(duration_secs, (int, float))
                    else None
                )
                end_str = f" to {actual_end:.1f}s" if actual_end is not None else ""
                warning_val = getattr(result, "warning", None)
                truncation_note = (
                    f" (NOTE: {warning_val})"
                    if isinstance(warning_val, str) and warning_val
                    else ""
                )

                slowdown_note = ""
                if slowdown_factor != 1.0:
                    slowdown_note = (
                        "WARNING: This video is slowed down to capture fast micro-actions.\n\n"
                    )

                prompt_with_context = (
                    "IMPORTANT CONTEXT: This video segment corresponds to"
                    " the test's relative time from"
                    f" {actual_start:.1f}s{end_str}{truncation_note}. A"
                    " timestamp showing the exact test relative time"
                    " (formatted as '<seconds> s') is burned in the"
                    " top-right corner of the video. Please watch this"
                    " burned-in timestamp and use its value to report all"
                    " timestamps (e.g., if the burned timestamp shows '90"
                    f" s', report 90.0s).\n\n{slowdown_note}"
                )
                if self.enable_ledger:
                    prompt_with_context += (
                        "BLACKBOARD"
                        f" LEDGER:\n{self.blackboard_ledger}\nDirective:"
                        " Use the Blackboard to understand what happened"
                        " outside your assigned timeframe. Do not"
                        " duplicate findings already on the board.\n\n"
                    )
                if warning_block:
                    prompt_with_context += (
                        f"{warning_block}Directive: Avoid repeating"
                        " searches that have already failed. Try to"
                        " approach the problem from a different angle or"
                        " use a different search strategy.\n\n"
                    )
                prompt_with_context += f"{specific_query}"

                if not self.use_native_gemini:
                    return await self._exec_single_chunk_universal(
                        compressed_path=compressed_path,
                        raw_path=path,
                        start_time=current_start,
                        end_time=current_end,
                        actual_start=actual_start,
                        prompt_with_context=prompt_with_context,
                        specific_query=specific_query,
                    )

                file = None
                try:
                    with TraceSpan(name="upload_video_to_gemini") as span:
                        file = await self.upload_and_poll_file(compressed_path)
                        span.result = f"Uploaded {file.name}"
                    logger.info(
                        "Invoking Gemini for sub-agent task with model"
                        f" {self.model_name} (streaming)..."
                    )
                    trace_id = CURRENT_TRACE_ID.get()

                    if self.ctx.data_engine and trace_id:
                        self.ctx.data_engine.record_trace(
                            type="llm_call",
                            name="video_sub_agent",
                            payload={
                                "contents": [
                                    f"file://{file.name}",
                                    prompt_with_context,
                                ],
                                "system_instruction": self.sub_system_prompt,
                            },
                            parent_trace_id=trace_id,
                        )

                    sub_agent_contents = [
                        types.Content(
                            role="user",
                            parts=[
                                types.Part.from_uri(file_uri=file.uri, mime_type=file.mime_type)
                                if hasattr(file, "uri")
                                else file,
                                types.Part.from_text(text=prompt_with_context),
                            ],
                        )
                    ]

                    sub_max_iterations = 2
                    sub_iterations = 0
                    final_analysis = "No analysis provided."
                    final_summary = "No summary provided."
                    final_full_text = ""

                    while sub_iterations < sub_max_iterations:
                        sub_iterations += 1
                        if sub_iterations == sub_max_iterations - 1:
                            sub_agent_contents.append(
                                types.Content(
                                    role="user",
                                    parts=[
                                        types.Part.from_text(
                                            text=(
                                                "[WARNING] You must call the"
                                                " submit_answer tool now, time"
                                                " is running out."
                                            )
                                        )
                                    ],
                                )
                            )

                        with TraceSpan(name="gemini_stream_content_main") as span:
                            stream = await asyncio.wait_for(
                                self.client.aio.models.generate_content_stream(
                                    model=self.model_name,
                                    contents=sub_agent_contents,
                                    config=types.GenerateContentConfig(
                                        system_instruction=self.sub_system_prompt,
                                        tools=[
                                            types.Tool(
                                                function_declarations=[self.submit_answer_tool]
                                            )
                                        ],
                                        safety_settings=SAFETY_SETTINGS_BLOCK_NONE,
                                    ),
                                ),
                                timeout=60,
                            )

                        async def read_stream(target_stream, current_trace_id):
                            text = ""
                            function_calls = []
                            accumulated_parts = []
                            async for chunk in target_stream:
                                if hasattr(chunk, "usage_metadata") and chunk.usage_metadata:
                                    span.payload["usage_metadata"] = {
                                        "prompt_token_count": getattr(
                                            chunk.usage_metadata,
                                            "prompt_token_count",
                                            0,
                                        ),
                                        "candidates_token_count": getattr(
                                            chunk.usage_metadata,
                                            "candidates_token_count",
                                            0,
                                        ),
                                        "total_token_count": getattr(
                                            chunk.usage_metadata,
                                            "total_token_count",
                                            0,
                                        ),
                                    }

                                chunk_text = chunk.text or ""
                                if chunk_text:
                                    text += chunk_text
                                    if self.ctx.data_engine and current_trace_id:
                                        self.ctx.data_engine.stream_output(
                                            current_trace_id, chunk_text
                                        )
                                if chunk.function_calls:
                                    function_calls.extend(chunk.function_calls)

                                candidates = getattr(chunk, "candidates", None)
                                if (
                                    candidates
                                    and isinstance(candidates, list)
                                    and len(candidates) > 0
                                ):
                                    content = getattr(candidates[0], "content", None)
                                    parts = getattr(content, "parts", None) if content else None
                                    if parts:
                                        for part in parts:
                                            accumulated_parts.append(part)
                                            if getattr(part, "thought", False) and part.text:
                                                if self.ctx.data_engine and current_trace_id:
                                                    self.ctx.data_engine.stream_output(
                                                        current_trace_id,
                                                        part.text,
                                                        is_thinking=True,
                                                    )
                                else:
                                    if chunk.function_calls:
                                        for fc in chunk.function_calls:
                                            accumulated_parts.append(types.Part(function_call=fc))

                            return text, function_calls, accumulated_parts

                        full_text, function_calls, original_parts = await asyncio.wait_for(
                            read_stream(stream, trace_id), timeout=180
                        )
                        final_full_text += full_text + "\n"

                        if function_calls:
                            answered = False
                            for fc in function_calls:
                                if (
                                    fc.name.split(":")[-1] if ":" in fc.name else fc.name
                                ) == "submit_answer":
                                    args = fc.args
                                    timeline_events = args.get("timeline_events", [])
                                    final_summary = (
                                        args.get("summary", "No summary provided.")
                                        .strip()
                                        .replace("\n", " ")
                                    )
                                    final_analysis = (
                                        args.get("analysis", "No analysis provided.")
                                        .strip()
                                        .replace("\n", " ")
                                    )

                                    for event in timeline_events:
                                        if (
                                            "confidence_score" not in event
                                            or not isinstance(
                                                event["confidence_score"],
                                                (int, float),
                                            )
                                            or isinstance(event["confidence_score"], bool)
                                            or not (0.0 <= float(event["confidence_score"]) <= 1.0)
                                        ):
                                            error_msg = (
                                                "Error: 'confidence_score' is"
                                                " missing or out of bounds in"
                                                " one of the timeline_events."
                                                " It must be a float between"
                                                " 0.0 and 1.0 inclusive. Fix"
                                                " the payload and call"
                                                " submit_answer again."
                                            )
                                            sub_agent_contents.append(
                                                types.Content(
                                                    role="model",
                                                    parts=original_parts
                                                    if original_parts
                                                    else [
                                                        types.Part(function_call=fc)
                                                        for fc in function_calls
                                                    ]
                                                    + (
                                                        [types.Part.from_text(text=full_text)]
                                                        if full_text
                                                        else []
                                                    ),
                                                )
                                            )
                                            sub_agent_contents.append(
                                                types.Content(
                                                    role="user",
                                                    parts=[
                                                        types.Part.from_function_response(
                                                            name=fc.name,
                                                            response={"error": (error_msg)},
                                                        )
                                                    ],
                                                )
                                            )
                                            answered = "error"
                                            break

                                        if (
                                            "verification_timestamp_secs" not in event
                                            or not isinstance(
                                                event["verification_timestamp_secs"],
                                                (int, float),
                                            )
                                            or isinstance(
                                                event["verification_timestamp_secs"], bool
                                            )
                                        ):
                                            error_msg = (
                                                "Error:"
                                                " 'verification_timestamp_secs'"
                                                " is missing or invalid in one"
                                                " of the timeline_events. Fix"
                                                " the payload and call"
                                                " submit_answer again."
                                            )
                                            sub_agent_contents.append(
                                                types.Content(
                                                    role="model",
                                                    parts=original_parts
                                                    if original_parts
                                                    else [
                                                        types.Part(function_call=fc)
                                                        for fc in function_calls
                                                    ]
                                                    + (
                                                        [types.Part.from_text(text=full_text)]
                                                        if full_text
                                                        else []
                                                    ),
                                                )
                                            )
                                            sub_agent_contents.append(
                                                types.Content(
                                                    role="user",
                                                    parts=[
                                                        types.Part.from_function_response(
                                                            name=fc.name,
                                                            response={"error": (error_msg)},
                                                        )
                                                    ],
                                                )
                                            )
                                            answered = "error"
                                            break

                                        verification_ts = float(
                                            event.get(
                                                "verification_timestamp_secs",
                                                start_time,
                                            )
                                        )
                                        confidence = float(event.get("confidence_score", 0.0))

                                        if verification_ts < start_time or (
                                            end_time is not None and verification_ts > end_time
                                        ):
                                            error_msg = (
                                                "Error:"
                                                " 'verification_timestamp_secs'"
                                                f" {verification_ts} must fall"
                                                " within the start_time"
                                                f" ({start_time}) and end_time"
                                                f" ({end_time}) boundaries. Fix"
                                                " the payload and call"
                                                " submit_answer again."
                                            )
                                            sub_agent_contents.append(
                                                types.Content(
                                                    role="model",
                                                    parts=original_parts
                                                    if original_parts
                                                    else [
                                                        types.Part(function_call=fc)
                                                        for fc in function_calls
                                                    ]
                                                    + (
                                                        [types.Part.from_text(text=full_text)]
                                                        if full_text
                                                        else []
                                                    ),
                                                )
                                            )
                                            sub_agent_contents.append(
                                                types.Content(
                                                    role="user",
                                                    parts=[
                                                        types.Part.from_function_response(
                                                            name=fc.name,
                                                            response={"error": (error_msg)},
                                                        )
                                                    ],
                                                )
                                            )
                                            answered = "error"
                                            break

                                    if answered == "error":
                                        break

                                    for event in timeline_events:
                                        verification_ts = float(
                                            event.get(
                                                "verification_timestamp_secs",
                                                start_time,
                                            )
                                        )
                                        confidence = float(event.get("confidence_score", 0.0))
                                        relative_offset = max(0.0, verification_ts - actual_start)

                                        out_dir = Path("/tmp/video_analyzer")
                                        out_dir.mkdir(parents=True, exist_ok=True)
                                        existing_imgs = glob.glob(f"{out_dir}/img_*.jpg")
                                        max_idx = -1
                                        for img in existing_imgs:
                                            match = re.search(r"img_(\d+)\.jpg$", img)
                                            if match:
                                                max_idx = max(max_idx, int(match.group(1)))
                                        next_idx = max_idx + 1
                                        screenshot_output_path = out_dir / f"img_{next_idx}.jpg"
                                        while screenshot_output_path.exists():
                                            next_idx += 1
                                            screenshot_output_path = out_dir / f"img_{next_idx}.jpg"
                                        screenshot_output_path.touch()
                                        self.local_files_to_cleanup.add(screenshot_output_path)
                                        ffmpeg_cmd = [
                                            "ffmpeg",
                                            "-ss",
                                            str(relative_offset),
                                            "-i",
                                            str(path),
                                            "-vframes",
                                            "1",
                                            "-q:v",
                                            "2",
                                            "-y",
                                            str(screenshot_output_path),
                                        ]
                                        try:
                                            proc = await asyncio.create_subprocess_exec(
                                                *ffmpeg_cmd,
                                                stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE,
                                            )
                                            await proc.communicate()
                                        except Exception as e:
                                            logger.warning(f"Failed to extract frame: {e}")

                                        screenshot_path_str = str(screenshot_output_path)
                                        entry = {
                                            "start": event.get("start_time", start_time),
                                            "end": event.get("end_time", end_time),
                                            "target": specific_query,
                                            "summary": event.get("transcription", ""),
                                            "confidence_score": confidence,
                                        }
                                        if Path(screenshot_output_path).exists():
                                            entry["screenshot"] = str(screenshot_path_str)

                                        self.blackboard_entries.append(entry)

                                    answered = True
                                    break

                            if answered is True:
                                break
                            elif answered == "error":
                                pass
                            else:
                                sub_agent_contents.append(
                                    types.Content(
                                        role="model",
                                        parts=original_parts
                                        if original_parts
                                        else [types.Part(function_call=fc) for fc in function_calls]
                                        + (
                                            [types.Part.from_text(text=full_text)]
                                            if full_text
                                            else []
                                        ),
                                    )
                                )
                                sub_agent_contents.append(
                                    types.Content(
                                        role="user",
                                        parts=[
                                            types.Part.from_function_response(
                                                name=function_calls[0].name,
                                                response={
                                                    "error": (
                                                        "Tool not recognized."
                                                        " Please use"
                                                        " submit_answer."
                                                    )
                                                },
                                            )
                                        ],
                                    )
                                )
                        else:
                            sub_agent_contents.append(
                                types.Content(
                                    role="model",
                                    parts=[types.Part.from_text(text=full_text)],
                                )
                            )

                    end_str = f"{current_end:.1f}s" if current_end is not None else "unknown"
                    return (
                        f"[from {current_start:.1f}s to {end_str}] Summary:"
                        f" {final_summary} Analysis: {final_analysis}"
                    )
                finally:
                    if file:
                        logger.info(f"Cleaning up cloud file {file.name}...")
                        try:
                            await asyncio.wait_for(
                                self.client.aio.files.delete(name=file.name),
                                timeout=30,
                            )
                            self.cloud_files_to_cleanup.discard(file.name)
                        except Exception as ce:
                            logger.error(f"Failed to delete cloud file {file.name}: {ce}")

            except APIError as api_err:
                logger.warning(f"Sub-agent APIError attempt {attempt + 1} failed: {api_err}")
                code = getattr(api_err, "code", None)
                if code == 429:
                    if attempt >= max_retries:
                        raise api_err

                    backoff = (2.0**attempt) + random.uniform(0.1, 1.0)
                    logger.info(
                        f"Rate limited (429). Backing off for {backoff:.2f}s"
                        " without expanding boundaries..."
                    )
                    await asyncio.sleep(backoff)
                    continue
                elif code in [400, 401, 403, 404]:
                    raise api_err

                if attempt >= max_retries:
                    raise api_err
                backoff = 2.0**attempt
                await asyncio.sleep(backoff)
            except Exception as e:
                logger.warning(f"Sub-agent attempt {attempt + 1} failed: {e}")
                if attempt >= max_retries:
                    raise e

                backoff = 2.0**attempt
                logger.info(f"Backing off for {backoff}s before retry...")
                await asyncio.sleep(backoff)

    @trace(type="tool", name="analyze_audio_only")
    async def exec_analyze_audio_only(
        self, start_time: float, end_time: float | None, specific_query: str
    ) -> str:
        max_retries = 2
        current_start = round(start_time, 1) if isinstance(start_time, (int, float)) else start_time
        current_end = round(end_time, 1) if isinstance(end_time, (int, float)) else end_time

        for attempt in range(max_retries + 1):
            try:
                logger.info(
                    f"Attempt {attempt + 1}/{max_retries + 1} for"
                    f" analyze_audio_only: start={current_start},"
                    f" end={current_end}"
                )
                controller = get_controller(self.ctx)
                async with TRANSCODE_SEMAPHORE:
                    result = await controller.extract_segment_metadata(current_start, current_end)
                if not result.success or not result.video_path:
                    raise Exception(f"Failed to get video segment: {result.message}")

                path = Path(result.video_path)
                self.local_files_to_cleanup.add(path)
                self.local_dirs_to_cleanup.add(path.parent)

                async with TRANSCODE_SEMAPHORE:
                    audio_path = await extract_audio_from_video(path)
                self.local_files_to_cleanup.add(audio_path)
                self.local_dirs_to_cleanup.add(audio_path.parent)
                actual_start = getattr(result, "actual_start_relative_time", current_start)
                if not isinstance(actual_start, (int, float)):
                    actual_start = current_start

                warnings = self.get_overlapping_warnings(start_time, end_time)
                warning_block = ""
                if warnings:
                    lines = [
                        "WARNING: The following queries were already tried in this timeframe:\n"
                    ]
                    for w in warnings:
                        lines.append(f"Searched for: {w['target']} -> {w['summary']}")
                    warning_block = "\n".join(lines) + "\n\n"

                duration_secs = getattr(result, "duration_seconds", None)
                actual_end = (
                    actual_start + duration_secs
                    if isinstance(duration_secs, (int, float))
                    else None
                )
                end_str = f" to {actual_end:.1f}s" if actual_end is not None else ""
                warning_val = getattr(result, "warning", None)
                truncation_note = (
                    f" (NOTE: {warning_val})"
                    if isinstance(warning_val, str) and warning_val
                    else ""
                )

                prompt_with_context = (
                    "IMPORTANT CONTEXT: This audio segment corresponds to"
                    " the test's relative time from"
                    f" {actual_start:.1f}s{end_str}{truncation_note}. When"
                    " you report timestamps, remember that your 00:00 is"
                    f" exactly {actual_start:.1f}s in the test relative"
                    " time. Please calculate and output all timestamps as"
                    " the exact test relative time (e.g., if you hear"
                    " something at 00:05, report"
                    f" {actual_start + 5:.1f}s).\n\n"
                )
                if self.enable_ledger:
                    prompt_with_context += (
                        "BLACKBOARD"
                        f" LEDGER:\n{self.blackboard_ledger}\nDirective:"
                        " Use the Blackboard to understand what happened"
                        " outside your assigned timeframe. Do not"
                        " duplicate findings already on the board.\n\n"
                    )
                if warning_block:
                    prompt_with_context += (
                        f"{warning_block}Directive: Do not repeat the"
                        " failed searches listed above. Shift your"
                        " attention to the current task.\n\n"
                    )
                prompt_with_context += f"{specific_query}"

                if not self.use_native_gemini:
                    return await self._exec_analyze_audio_universal(
                        audio_path=audio_path,
                        start_time=current_start,
                        end_time=current_end,
                        actual_start=actual_start,
                        prompt_with_context=prompt_with_context,
                        specific_query=specific_query,
                    )

                file = None
                try:
                    with TraceSpan(name="upload_audio_to_gemini") as span:
                        file = await self.upload_and_poll_file(audio_path)
                        span.result = f"Uploaded {file.name}"
                    logger.info(
                        f"Invoking Gemini for audio-only task with model {self.model_name}..."
                    )

                    sub_agent_contents = [
                        types.Content(
                            role="user",
                            parts=[
                                types.Part.from_uri(file_uri=file.uri, mime_type=file.mime_type)
                                if hasattr(file, "uri")
                                else file,
                                types.Part.from_text(text=prompt_with_context),
                            ],
                        )
                    ]

                    sub_max_iterations = 2
                    sub_iterations = 0
                    final_confidence_score = 0.0
                    final_summary = "No summary provided."
                    final_analysis = "No analysis provided."
                    final_full_text = ""

                    while sub_iterations < sub_max_iterations:
                        sub_iterations += 1
                        if sub_iterations == sub_max_iterations - 1:
                            sub_agent_contents.append(
                                types.Content(
                                    role="user",
                                    parts=[
                                        types.Part.from_text(
                                            text=(
                                                "[WARNING] You must call the"
                                                " submit_answer tool now, time"
                                                " is running out."
                                            )
                                        )
                                    ],
                                )
                            )

                        with TraceSpan(name="gemini_generate_audio_content") as span:
                            response = await asyncio.wait_for(
                                self.client.aio.models.generate_content(
                                    model=self.model_name,
                                    contents=sub_agent_contents,
                                    config=types.GenerateContentConfig(
                                        system_instruction=self.audio_system_prompt,
                                        tools=[
                                            types.Tool(
                                                function_declarations=[self.submit_answer_tool]
                                            )
                                        ],
                                        safety_settings=SAFETY_SETTINGS_BLOCK_NONE,
                                    ),
                                ),
                                timeout=180,
                            )

                        full_text = response.text or ""
                        final_full_text += full_text + "\n"
                        function_calls = response.function_calls or []

                        if function_calls:
                            answered = False
                            for fc in function_calls:
                                if (
                                    fc.name.split(":")[-1] if ":" in fc.name else fc.name
                                ) == "submit_answer":
                                    args = fc.args
                                    if (
                                        "confidence_score" not in args
                                        or not isinstance(
                                            args["confidence_score"],
                                            (int, float),
                                        )
                                        or not (0.0 <= float(args["confidence_score"]) <= 1.0)
                                    ):
                                        error_msg = (
                                            "Error: 'confidence_score' is"
                                            " missing or out of bounds. It must"
                                            " be a float between 0.0 and 1.0"
                                            " inclusive. Fix the payload and"
                                            " call submit_answer again."
                                        )
                                        sub_agent_contents.append(
                                            types.Content(
                                                role="model",
                                                parts=response.candidates[0].content.parts,
                                            )
                                        )
                                        sub_agent_contents.append(
                                            types.Content(
                                                role="user",
                                                parts=[
                                                    types.Part.from_function_response(
                                                        name=fc.name,
                                                        response={"error": error_msg},
                                                    )
                                                ],
                                            )
                                        )
                                        answered = "error"
                                        break

                                    final_summary = (
                                        args.get("summary", "No summary provided.")
                                        .strip()
                                        .replace("\n", " ")
                                    )
                                    final_analysis = (
                                        args.get("analysis", "No analysis provided.")
                                        .strip()
                                        .replace("\n", " ")
                                    )
                                    final_confidence_score = float(args["confidence_score"])
                                    answered = True
                                    break

                            if answered is True:
                                break
                            elif answered == "error":
                                pass
                            else:
                                sub_agent_contents.append(
                                    types.Content(
                                        role="model",
                                        parts=response.candidates[0].content.parts,
                                    )
                                )
                                sub_agent_contents.append(
                                    types.Content(
                                        role="user",
                                        parts=[
                                            types.Part.from_function_response(
                                                name=function_calls[0].name,
                                                response={
                                                    "error": (
                                                        "Tool not recognized."
                                                        " Please use"
                                                        " submit_answer."
                                                    )
                                                },
                                            )
                                        ],
                                    )
                                )
                        else:
                            sub_agent_contents.append(
                                types.Content(
                                    role="model",
                                    parts=[types.Part.from_text(text=full_text)],
                                )
                            )

                    end_val = end_time if end_time is not None else "unknown"
                    self.blackboard_entries.append(
                        {
                            "start": start_time,
                            "end": end_val,
                            "target": specific_query,
                            "summary": final_summary,
                            "confidence_score": final_confidence_score,
                        }
                    )

                    end_str = f"{end_time:.1f}s" if end_time is not None else "unknown"
                    return (
                        f"[from {start_time:.1f}s to {end_str}] Summary:"
                        f" {final_summary} Analysis: {final_analysis}"
                    )
                finally:
                    if file:
                        logger.info(f"Cleaning up cloud file {file.name}...")
                        try:
                            await asyncio.wait_for(
                                self.client.aio.files.delete(name=file.name),
                                timeout=30,
                            )
                            self.cloud_files_to_cleanup.discard(file.name)
                        except Exception as ce:
                            logger.error(f"Failed to delete cloud file {file.name}: {ce}")

            except APIError as api_err:
                logger.warning(f"Audio APIError attempt {attempt + 1} failed: {api_err}")
                code = getattr(api_err, "code", None)
                if code == 429:
                    if attempt >= max_retries:
                        raise api_err

                    backoff = (2.0**attempt) + random.uniform(0.1, 1.0)
                    logger.info(
                        f"Rate limited (429). Backing off for {backoff:.2f}s"
                        " without expanding boundaries..."
                    )
                    await asyncio.sleep(backoff)
                    continue
                elif code in [400, 401, 403, 404]:
                    raise api_err

                if attempt >= max_retries:
                    raise api_err
                backoff = 2.0**attempt
                await asyncio.sleep(backoff)
            except Exception as e:
                logger.warning(f"Audio attempt {attempt + 1} failed: {e}")
                if attempt >= max_retries:
                    raise e

                backoff = 2.0**attempt
                logger.info(f"Backing off for {backoff}s before retry...")
                await asyncio.sleep(backoff)

    async def _exec_single_chunk_universal(
        self,
        compressed_path: Path,
        raw_path: Path,
        start_time: float,
        end_time: float | None,
        actual_start: float,
        prompt_with_context: str,
        specific_query: str,
    ) -> str:
        """Executes sub-agent chunk analysis via keyframe extraction and LangChain ChatModel."""
        keyframes = extract_keyframes_from_video(
            compressed_path,
            fps=1.0,
            max_frames=30,
            max_dimension=1080,
        )

        user_blocks: list[dict[str, Any]] = [{"type": "text", "text": prompt_with_context}]

        for ts_sec, frame_bytes in keyframes:
            b64_str = base64.b64encode(frame_bytes).decode("utf-8")
            abs_time = actual_start + ts_sec
            user_blocks.append(
                {
                    "type": "text",
                    "text": f"--- Video Keyframe at {abs_time:.1f}s ---",
                }
            )
            user_blocks.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64_str}"},
                }
            )

        messages = [
            SystemMessage(content=self.sub_system_prompt),
            HumanMessage(content=user_blocks),
        ]

        trace_id = CURRENT_TRACE_ID.get()
        if self.ctx.data_engine and trace_id:
            self.ctx.data_engine.record_trace(
                type="llm_call",
                name="video_sub_agent_universal",
                payload={
                    "num_keyframes": len(keyframes),
                    "prompt": prompt_with_context,
                    "system_instruction": self.sub_system_prompt,
                },
                parent_trace_id=trace_id,
            )

        llm = get_llm(self.ctx, name="video_analyzer")
        bound_llm = llm.bind_tools([UNIVERSAL_SUBMIT_ANSWER_TOOL])

        with TraceSpan(name="universal_sub_agent_call") as span:
            response = await asyncio.wait_for(bound_llm.ainvoke(messages), timeout=180)
            span.result = (
                f"Received response (tool_calls={len(getattr(response, 'tool_calls', []))})"
            )

        final_summary = "No summary provided."
        final_analysis = "No analysis provided."
        timeline_events = []

        if getattr(response, "tool_calls", None):
            for tc in response.tool_calls:
                if tc.get("name") == "submit_answer":
                    args = tc.get("args", {})
                    final_summary = (
                        str(args.get("summary", "")).strip().replace("\n", " ") or final_summary
                    )
                    final_analysis = (
                        str(args.get("analysis", "")).strip().replace("\n", " ") or final_analysis
                    )
                    timeline_events = args.get("timeline_events", [])
                    break
        elif getattr(response, "content", None):
            text_content = str(response.content).strip()
            final_summary = text_content[:200].replace("\n", " ")
            final_analysis = text_content

        # Process timeline events & save screenshots for the blackboard ledger
        for event in timeline_events:
            if not isinstance(event, dict):
                continue
            v_ts = float(event.get("verification_timestamp_secs", start_time))
            conf = float(event.get("confidence_score", 0.8))

            screenshot_path_str = None
            try:
                rel_offset = max(0.0, v_ts - actual_start)
                out_dir = Path("/tmp/video_analyzer")
                out_dir.mkdir(parents=True, exist_ok=True)

                existing_imgs = glob.glob(f"{out_dir}/img_*.jpg")
                max_idx = -1
                for img in existing_imgs:
                    m = re.search(r"img_(\d+)\.jpg$", img)
                    if m:
                        max_idx = max(max_idx, int(m.group(1)))
                next_idx = max_idx + 1
                shot_path = out_dir / f"img_{next_idx}.jpg"

                ffmpeg_cmd = [
                    "ffmpeg",
                    "-ss",
                    str(rel_offset),
                    "-i",
                    str(raw_path),
                    "-vframes",
                    "1",
                    "-q:v",
                    "2",
                    "-y",
                    str(shot_path),
                ]
                proc = await asyncio.create_subprocess_exec(
                    *ffmpeg_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.communicate()
                if shot_path.exists():
                    self.local_files_to_cleanup.add(shot_path)
                    screenshot_path_str = str(shot_path)
            except Exception as fe:
                logger.warning(f"Failed extracting keyframe for event {v_ts}s: {fe}")

            entry = {
                "start": event.get("start_time", start_time),
                "end": event.get("end_time", end_time),
                "target": specific_query,
                "summary": event.get("transcription", final_summary),
                "confidence_score": conf,
            }
            if screenshot_path_str:
                entry["screenshot"] = screenshot_path_str
            self.blackboard_entries.append(entry)

        end_str = f"{end_time:.1f}s" if end_time is not None else "unknown"
        return (
            f"[from {start_time:.1f}s to {end_str}] Summary: {final_summary}"
            f" Analysis: {final_analysis}"
        )

    async def _exec_analyze_audio_universal(
        self,
        audio_path: Path,
        start_time: float,
        end_time: float | None,
        actual_start: float,
        prompt_with_context: str,
        specific_query: str,
    ) -> str:
        """Executes audio analysis via Universal LangChain ChatModel."""
        messages = [
            SystemMessage(content=self.audio_system_prompt),
            HumanMessage(content=prompt_with_context),
        ]
        llm = get_llm(self.ctx, name="video_analyzer")
        bound_llm = llm.bind_tools([UNIVERSAL_SUBMIT_ANSWER_TOOL])

        response = await asyncio.wait_for(bound_llm.ainvoke(messages), timeout=180)
        final_summary = "No audio summary provided."
        final_analysis = "No audio analysis provided."

        if getattr(response, "tool_calls", None):
            for tc in response.tool_calls:
                if tc.get("name") == "submit_answer":
                    args = tc.get("args", {})
                    final_summary = (
                        str(args.get("summary", "")).strip().replace("\n", " ") or final_summary
                    )
                    final_analysis = (
                        str(args.get("analysis", "")).strip().replace("\n", " ") or final_analysis
                    )
                    break
        elif getattr(response, "content", None):
            final_summary = str(response.content)[:200].replace("\n", " ")
            final_analysis = str(response.content)

        end_val = end_time if end_time is not None else "unknown"
        self.blackboard_entries.append(
            {
                "start": start_time,
                "end": end_val,
                "target": specific_query,
                "summary": final_summary,
                "confidence_score": 0.8,
            }
        )

        end_str = f"{end_time:.1f}s" if end_time is not None else "unknown"
        return (
            f"[from {start_time:.1f}s to {end_str}] Audio Summary: {final_summary}"
            f" Analysis: {final_analysis}"
        )

    async def _run_universal(
        self, time_description: str, purpose: str, system_prompt: str
    ) -> tuple[str, str]:
        """Runs VideoAnalyzer main reasoning loop using Universal LangChain BaseChatModel."""
        llm = get_llm(self.ctx, name="video_analyzer")
        bound_llm = llm.bind_tools(UNIVERSAL_MAIN_TOOLS)

        messages: list[BaseMessage] = [
            SystemMessage(content=system_prompt),
            HumanMessage(
                content=(
                    f"Time description: {time_description}\n"
                    f"Purpose: {purpose}\n"
                    "Please coordinate video metadata extraction and sub-agent analysis."
                )
            ),
        ]

        max_iterations = 6
        iterations = 0
        agent_outcome = ""
        status = "success"

        try:
            while iterations < max_iterations:
                iterations += 1
                response = await asyncio.wait_for(bound_llm.ainvoke(messages), timeout=300)
                messages.append(response)

                if not getattr(response, "tool_calls", None):
                    agent_outcome = str(response.content)
                    break

                for tc in response.tool_calls:
                    t_name = tc["name"]
                    t_args = tc.get("args", {})
                    t_id = tc.get("id", f"call_{iterations}")
                    logger.info(f"Executing universal tool '{t_name}' with args {t_args}")

                    try:
                        if t_name == "extract_segment_metadata":
                            res = await self.exec_extract_segment_metadata(
                                t_args.get("start_time"), t_args.get("end_time")
                            )
                        elif t_name == "spawn_sub_agent":
                            res = await self.exec_spawn_sub_agent(
                                t_args.get("start_time"),
                                t_args.get("end_time"),
                                t_args.get("specific_query"),
                            )
                        elif t_name == "analyze_audio_only":
                            res = await self.exec_analyze_audio_only(
                                t_args.get("start_time"),
                                t_args.get("end_time"),
                                t_args.get("specific_query"),
                            )
                        else:
                            res = f"Error: Tool '{t_name}' is not recognized."
                    except Exception as tool_err:
                        logger.error(f"Error executing tool '{t_name}': {tool_err}")
                        res = f"Error executing tool '{t_name}': {tool_err}"

                    messages.append(ToolMessage(tool_call_id=t_id, name=t_name, content=str(res)))

            if not agent_outcome:
                agent_outcome = "Video analyzer completed all turns without error."

        except Exception as e:
            logger.error(f"Universal video analyzer failed: {e}")
            agent_outcome = f"Video analysis failed: {e}"
            status = "error"

        return agent_outcome, status

    @trace(type="agent", name="video_analyzer")
    async def run(self, time_description: str, purpose: str) -> tuple[str, str]:
        ctx = self.ctx
        async with MAIN_AGENT_SEMAPHORE:
            logger.info(
                f"Video analyzer invoked with time: '{time_description}', purpose: '{purpose}'"
            )

            # Initialize engine & cleanup tracking
            self._init_engine()

            # Resolve model name and temperature from config
            llm_config = getattr(ctx, "llm_config", None)
            utils_cfg = getattr(llm_config, "utils", None) if llm_config else None
            llm_cfg = getattr(utils_cfg, "video_analyzer", None) if utils_cfg else None
            temperature = 0.2
            thinking_level = None
            if llm_cfg:
                self.model_name = llm_cfg.model
                if "/" in self.model_name:
                    self.model_name = self.model_name.split("/")[-1]
                if getattr(llm_cfg, "temperature", None) is not None:
                    temperature = llm_cfg.temperature
                if getattr(llm_cfg, "thinking_level", None) is not None:
                    thinking_level = llm_cfg.thinking_level
            else:
                self.model_name = "gemini-3.6-flash"

            # Track files for cleanup
            self.local_files_to_cleanup = set()
            self.local_dirs_to_cleanup = set()
            self.cloud_files_to_cleanup = set()

        # 1. Use initialized native tools declarations
        tools_declaration = self.tools_declaration

        sub_prompt_path = Path(__file__).parent / "video_sub_agent.md"
        self.sub_system_prompt = sub_prompt_path.read_text(encoding="utf-8")

        audio_prompt_path = Path(__file__).parent / "video_sub_agent_audio.md"
        self.audio_system_prompt = audio_prompt_path.read_text(encoding="utf-8")

        prompt_path = Path(__file__).parent / "video_analyzer.md"
        system_prompt = prompt_path.read_text(encoding="utf-8")
        if self.enable_ledger:
            system_prompt += (
                "\n## Trust Protocol\nConsult Blackboard Ledger before"
                " searching. Evaluate if new search is necessary based on"
                " previous context. Explain reasoning before aborting or"
                " proceeding.\n"
            )

        if not self.use_native_gemini:
            return await self._run_universal(
                time_description=time_description,
                purpose=purpose,
                system_prompt=system_prompt,
            )

        contents = [
            types.Content(
                role="user",
                parts=[
                    types.Part.from_text(
                        text=(f"Time description: {time_description}\nPurpose: {purpose}")
                    )
                ],
            )
        ]

        agent_outcome = ""
        status = "success"
        max_iterations = 10
        iterations = 0
        last_ledger_index = 0

        try:
            while iterations < max_iterations:
                iterations += 1
                logger.info(f"Iteration {iterations}: Invoking Native Gemini SDK...")

                self.blackboard_entries = ConflictResolutionService.clean(self.blackboard_entries)

                new_entries = self.blackboard_entries[last_ledger_index:]
                if self.enable_ledger and new_entries:
                    lines = []
                    image_parts = []
                    for e in new_entries:
                        line = f"[{e['start']}s - {e['end']}s] SUMMARY: {e['summary']}"
                        screenshot_file = e.get("screenshot") or e.get("screenshot_path")
                        if screenshot_file and Path(screenshot_file).exists():
                            line += f" PROOF: {Path(screenshot_file).name}"
                            image_parts.append(
                                types.Part.from_text(text=f"PROOF: {Path(screenshot_file).name}")
                            )
                            image_parts.append(
                                types.Part.from_bytes(
                                    data=Path(screenshot_file).read_bytes(),
                                    mime_type="image/jpeg",
                                )
                            )
                        lines.append(line)
                    diff_text = "NEW BLACKBOARD ENTRIES ADDED: " + " | ".join(lines)
                    contents.append(
                        types.Content(
                            role="user",
                            parts=[types.Part.from_text(text=diff_text)],
                        )
                    )
                    if image_parts:
                        contents.append(types.Content(role="user", parts=image_parts))
                    last_ledger_index = len(self.blackboard_entries)

                is_final_turn = iterations == max_iterations
                if is_final_turn:
                    contents.append(
                        types.Content(
                            role="user",
                            parts=[
                                types.Part.from_text(
                                    text=(
                                        "This is your final iteration; all"
                                        " tools are stripped, and you must"
                                        " provide your final answer directly."
                                    )
                                )
                            ],
                        )
                    )
                else:
                    if iterations > 1:
                        contents.append(
                            types.Content(
                                role="user",
                                parts=[
                                    types.Part.from_text(
                                        text=(
                                            "You have not completed the video"
                                            " analysis yet (iteration"
                                            f" {iterations} of"
                                            f" {max_iterations})."
                                        )
                                    )
                                ],
                            )
                        )

                with TraceSpan(name="gemini_main_agent_call") as span:
                    trace_id = CURRENT_TRACE_ID.get()

                    if ctx.data_engine and trace_id:
                        serialized_contents = []
                        for c in contents:
                            if hasattr(c, "model_dump"):
                                dumped = c.model_dump()
                                if (
                                    isinstance(dumped, dict)
                                    and "parts" in dumped
                                    and dumped["parts"]
                                ):
                                    for p in dumped["parts"]:
                                        if (
                                            isinstance(p, dict)
                                            and "inline_data" in p
                                            and p["inline_data"]
                                            and "data" in p["inline_data"]
                                        ):
                                            p["inline_data"]["data"] = "<image_bytes_sanitized>"
                                serialized_contents.append(dumped)
                            else:
                                serialized_contents.append(str(c))

                        ctx.data_engine.record_trace(
                            type="llm_call",
                            name="video_analyzer_main",
                            payload={
                                "contents": serialized_contents,
                                "system_instruction": system_prompt,
                                "tools": (
                                    [t.name for t in tools_declaration]
                                    if (tools_declaration and not is_final_turn)
                                    else []
                                ),
                            },
                            parent_trace_id=trace_id,
                        )

                    async def run_stream():
                        stream = await asyncio.wait_for(
                            self.client.aio.models.generate_content_stream(
                                model=self.model_name,
                                contents=contents,
                                config=types.GenerateContentConfig(
                                    system_instruction=system_prompt,
                                    temperature=temperature,
                                    tools=[]
                                    if is_final_turn
                                    else [types.Tool(function_declarations=tools_declaration)],
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
                                ),
                            ),
                            timeout=60,
                        )

                        text = ""
                        accumulated_parts = []
                        function_calls = []

                        async for chunk in stream:
                            if hasattr(chunk, "usage_metadata") and chunk.usage_metadata:
                                span.payload["usage_metadata"] = {
                                    "prompt_token_count": getattr(
                                        chunk.usage_metadata,
                                        "prompt_token_count",
                                        0,
                                    ),
                                    "candidates_token_count": getattr(
                                        chunk.usage_metadata,
                                        "candidates_token_count",
                                        0,
                                    ),
                                    "total_token_count": getattr(
                                        chunk.usage_metadata,
                                        "total_token_count",
                                        0,
                                    ),
                                }

                            chunk_text = chunk.text or ""
                            text += chunk_text
                            if ctx.data_engine and trace_id and chunk_text:
                                ctx.data_engine.stream_output(
                                    trace_id, chunk_text, is_thinking=False
                                )

                            if chunk.function_calls:
                                function_calls.extend(chunk.function_calls)

                            candidates = getattr(chunk, "candidates", None)
                            if candidates and isinstance(candidates, list) and len(candidates) > 0:
                                content = getattr(candidates[0], "content", None)
                                parts = getattr(content, "parts", None) if content else None
                                if parts:
                                    for part in parts:
                                        accumulated_parts.append(part)
                                        if getattr(part, "thought", False) and part.text:
                                            if ctx.data_engine and trace_id:
                                                ctx.data_engine.stream_output(
                                                    trace_id,
                                                    part.text,
                                                    is_thinking=True,
                                                )
                            else:
                                if chunk.function_calls:
                                    for fc in chunk.function_calls:
                                        accumulated_parts.append(types.Part(function_call=fc))

                        class DummyResponse:
                            def __init__(self, text, function_calls, parts):
                                self.text = text
                                self.function_calls = function_calls
                                self.candidates = [
                                    types.Candidate(
                                        content=types.Content(
                                            role="model",
                                            parts=parts
                                            if parts
                                            else (
                                                [types.Part.from_text(text=text)] if text else []
                                            ),
                                        )
                                    )
                                ]

                        return DummyResponse(text, function_calls, accumulated_parts)

                    response = await asyncio.wait_for(run_stream(), timeout=300)
                    span.result = (
                        f"Function calls: {len(response.function_calls)}"
                        if response.function_calls
                        else "Final answer"
                    )

                function_calls = response.function_calls
                if not function_calls:
                    agent_outcome = response.text
                    break

                if response.candidates and response.candidates[0].content:
                    contents.append(response.candidates[0].content)
                else:
                    contents.append(
                        types.Content(
                            role="model",
                            parts=[types.Part(function_call=fc) for fc in function_calls],
                        )
                    )

                sub_agent_calls = [
                    fc
                    for fc in function_calls
                    if (fc.name.split(":")[-1] if ":" in fc.name else fc.name) == "spawn_sub_agent"
                ]
                audio_only_calls = [
                    fc
                    for fc in function_calls
                    if (fc.name.split(":")[-1] if ":" in fc.name else fc.name)
                    == "analyze_audio_only"
                ]
                other_calls = [
                    fc
                    for fc in function_calls
                    if (fc.name.split(":")[-1] if ":" in fc.name else fc.name)
                    not in ["spawn_sub_agent", "analyze_audio_only"]
                ]

                tool_response_parts = []

                if sub_agent_calls:
                    logger.info(f"Executing {len(sub_agent_calls)} sub-agent calls in parallel...")

                    async def bounded_spawn(fc):
                        async with API_SEMAPHORE:
                            return await self.exec_spawn_sub_agent(
                                fc.args.get("start_time"),
                                fc.args.get("end_time"),
                                fc.args.get("specific_query"),
                            )

                    tasks = [bounded_spawn(fc) for fc in sub_agent_calls]
                    results = await asyncio.gather(*tasks, return_exceptions=True)

                    for fc, result in zip(sub_agent_calls, results):
                        if isinstance(result, Exception):
                            error_msg = f"Sub-agent failed. Error: {result}"
                            logger.error(error_msg)
                            tool_response_parts.append(
                                types.Part.from_function_response(
                                    name=fc.name, response={"error": error_msg}
                                )
                            )
                        else:
                            tool_response_parts.append(
                                types.Part.from_function_response(
                                    name=fc.name, response={"result": result}
                                )
                            )

                if audio_only_calls:
                    logger.info(
                        f"Executing {len(audio_only_calls)} analyze_audio_only calls in parallel..."
                    )

                    async def bounded_audio_spawn(fc):
                        async with API_SEMAPHORE:
                            return await self.exec_analyze_audio_only(
                                fc.args.get("start_time"),
                                fc.args.get("end_time"),
                                fc.args.get("specific_query"),
                            )

                    tasks = [bounded_audio_spawn(fc) for fc in audio_only_calls]
                    results = await asyncio.gather(*tasks, return_exceptions=True)

                    for fc, result in zip(audio_only_calls, results):
                        if isinstance(result, Exception):
                            error_msg = f"Audio analysis failed. Error: {result}"
                            logger.error(error_msg)
                            tool_response_parts.append(
                                types.Part.from_function_response(
                                    name=fc.name, response={"error": error_msg}
                                )
                            )
                        else:
                            tool_response_parts.append(
                                types.Part.from_function_response(
                                    name=fc.name, response={"result": result}
                                )
                            )

                for fc in other_calls:
                    name = fc.name.split(":")[-1] if ":" in fc.name else fc.name
                    args = fc.args
                    logger.info(f"Executing tool '{name}' sequentially...")

                    if name == "extract_segment_metadata":
                        try:
                            res = await self.exec_extract_segment_metadata(
                                args.get("start_time"), args.get("end_time")
                            )
                            tool_response_parts.append(
                                types.Part.from_function_response(
                                    name=name, response={"result": res}
                                )
                            )
                        except Exception as e:
                            tool_response_parts.append(
                                types.Part.from_function_response(
                                    name=name, response={"error": str(e)}
                                )
                            )

                if tool_response_parts:
                    contents.append(types.Content(role="user", parts=tool_response_parts))

            if iterations >= max_iterations and not agent_outcome:
                agent_outcome = (
                    "Error: Video analyzer agent reached maximum iterations"
                    " without settling on a final answer."
                )
                status = "error"

        except Exception as e:
            logger.error(f"Video analyzer failed: {e}")
            agent_outcome = f"Video analysis failed: {e}"
            status = "error"
        finally:
            if self.cloud_files_to_cleanup:
                logger.info(
                    f"Cleaning up {len(self.cloud_files_to_cleanup)} remaining"
                    " cloud files in parallel..."
                )
                tasks = [
                    asyncio.wait_for(self.client.aio.files.delete(name=name), timeout=30)
                    for name in list(self.cloud_files_to_cleanup)
                ]
                await asyncio.gather(*tasks, return_exceptions=True)
                self.cloud_files_to_cleanup.clear()

            if os.environ.get("KEEP_VIDEOS") or os.environ.get("ARTEMIS_DEBUG"):
                logger.info(
                    "Skipping cleanup of local video files and directories due to debug mode."
                )
            else:
                for path in list(self.local_files_to_cleanup):
                    try:
                        if path.exists():
                            logger.info(f"Cleaning up local file {path}...")
                            path.unlink()
                    except Exception as e:
                        logger.error(f"Failed to delete local file {path}: {e}")

                for dir_path in list(self.local_dirs_to_cleanup):
                    try:
                        resolved = dir_path.resolve()
                        system_dirs = {
                            Path("/").resolve(),
                            Path("/tmp").resolve(),
                            Path("/var/tmp").resolve(),
                            Path.home().resolve(),
                            Path(tempfile.gettempdir()).resolve(),
                        }
                        if resolved in system_dirs:
                            logger.debug(f"Skipping deletion of root/system directory: {dir_path}")
                            continue

                        if dir_path.exists():
                            logger.info(f"Cleaning up local directory {dir_path}...")
                            shutil.rmtree(dir_path, ignore_errors=True)
                    except Exception as e:
                        logger.error(f"Failed to delete local directory {dir_path}: {e}")

        return agent_outcome, status
