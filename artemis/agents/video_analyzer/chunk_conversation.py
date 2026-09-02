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

"""Native Gemini sub-agent conversation and submit_answer handling for chunks.

Extracted from ``video_analyzer.py`` as a pure structural split.  Patchable
collaborators are looked up late through the facade module so
``mock.patch("artemis.agents.video_analyzer.video_analyzer.<name>")`` targets
keep working.
"""

import asyncio
import glob
from pathlib import Path
import re

from google.genai import types

from artemis.agents.video_analyzer import video_analyzer as _va
from artemis.constants import SAFETY_SETTINGS_BLOCK_NONE
from artemis.data_engine.trace import CURRENT_TRACE_ID, TraceSpan
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


class _ChunkMedia:
    """Mutable holder for per-attempt media artifacts shared with failure handling."""

    __slots__ = ("path", "compressed_path", "actual_start", "prompt_with_context")

    def __init__(self):
        self.path: Path | None = None
        self.compressed_path: Path | None = None
        self.actual_start: float | None = None
        self.prompt_with_context: str | None = None


async def _run_native_chunk_conversation(
    analyzer,
    media: _ChunkMedia,
    current_start: float,
    current_end: float,
    start_time: float,
    end_time: float | None,
    specific_query: str,
    lease_owner: str,
) -> str:
    """Uploads the chunk and drives the native Gemini sub-agent to a committed answer."""
    file = None
    try:
        with TraceSpan(name="upload_video_to_gemini") as span:
            file = await analyzer.upload_and_poll_file(media.compressed_path)
            span.result = f"Uploaded {file.name}"
        logger.info(
            "Invoking Gemini for sub-agent task with model"
            f" {analyzer.model_name} (streaming)..."
        )
        trace_id = CURRENT_TRACE_ID.get()

        if analyzer.ctx.data_engine and trace_id:
            analyzer.ctx.data_engine.record_trace(
                type="llm_call",
                name="video_sub_agent",
                payload={
                    "contents": [
                        f"file://{file.name}",
                        media.prompt_with_context,
                    ],
                    "system_instruction": analyzer.sub_system_prompt,
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
                    types.Part.from_text(text=media.prompt_with_context),
                ],
            )
        ]

        final_summary, final_analysis = await _drive_sub_agent_loop(
            analyzer,
            sub_agent_contents,
            trace_id,
            media,
            start_time,
            end_time,
            specific_query,
        )

        end_str = f"{current_end:.1f}s" if current_end is not None else "unknown"
        chunk_result = (
            f"[from {current_start:.1f}s to {end_str}] Summary:"
            f" {final_summary} Analysis: {final_analysis}"
        )
        analyzer.blackboard.complete_segment(
            current_start,
            current_end,
            specific_query,
            lease_owner,
            final_summary,
            final_analysis,
        )
        return chunk_result
    finally:
        if file:
            logger.info(f"Cleaning up cloud file {file.name}...")
            try:
                await asyncio.wait_for(
                    analyzer.client.aio.files.delete(name=file.name),
                    timeout=30,
                )
                analyzer.cloud_files_to_cleanup.discard(file.name)
            except Exception as ce:
                logger.error(f"Failed to delete cloud file {file.name}: {ce}")


async def _drive_sub_agent_loop(
    analyzer,
    sub_agent_contents: list,
    trace_id,
    media: _ChunkMedia,
    start_time: float,
    end_time: float | None,
    specific_query: str,
) -> tuple[str, str]:
    """Iterates the sub-agent conversation until submit_answer is accepted."""
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

        full_text, function_calls, original_parts = await _stream_sub_agent_turn(
            analyzer, sub_agent_contents, trace_id
        )
        final_full_text += full_text + "\n"

        if function_calls:
            answered, final_summary, final_analysis = await _handle_sub_agent_calls(
                analyzer,
                sub_agent_contents,
                function_calls,
                original_parts,
                full_text,
                media,
                start_time,
                end_time,
                specific_query,
                final_summary,
                final_analysis,
            )
            if answered is True:
                break
        else:
            sub_agent_contents.append(
                types.Content(
                    role="model",
                    parts=[types.Part.from_text(text=full_text)],
                )
            )

    return final_summary, final_analysis


async def _stream_sub_agent_turn(
    analyzer, sub_agent_contents: list, trace_id
) -> tuple[str, list, list]:
    """Starts one streamed Gemini turn and consumes it fully."""
    with TraceSpan(name="gemini_stream_content_main") as span:
        stream = await asyncio.wait_for(
            analyzer.client.aio.models.generate_content_stream(
                model=analyzer.model_name,
                contents=sub_agent_contents,
                config=types.GenerateContentConfig(
                    system_instruction=analyzer.sub_system_prompt,
                    tools=[
                        types.Tool(
                            function_declarations=[analyzer.submit_answer_tool]
                        )
                    ],
                    safety_settings=SAFETY_SETTINGS_BLOCK_NONE,
                ),
            ),
            timeout=60,
        )

    full_text, function_calls, original_parts = await asyncio.wait_for(
        _read_sub_agent_stream(analyzer, span, stream, trace_id),
        timeout=analyzer.model_call_timeout_seconds,
    )
    return full_text, function_calls, original_parts


async def _read_sub_agent_stream(analyzer, span, target_stream, current_trace_id):
    """Consumes a streamed response, mirroring text/thoughts into the trace."""
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
            if analyzer.ctx.data_engine and current_trace_id:
                analyzer.ctx.data_engine.stream_output(
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
                        if analyzer.ctx.data_engine and current_trace_id:
                            analyzer.ctx.data_engine.stream_output(
                                current_trace_id,
                                part.text,
                                is_thinking=True,
                            )
        else:
            if chunk.function_calls:
                for fc in chunk.function_calls:
                    accumulated_parts.append(types.Part(function_call=fc))

    return text, function_calls, accumulated_parts


async def _handle_sub_agent_calls(
    analyzer,
    sub_agent_contents: list,
    function_calls: list,
    original_parts: list,
    full_text: str,
    media: _ChunkMedia,
    start_time: float,
    end_time: float | None,
    specific_query: str,
    final_summary: str,
    final_analysis: str,
):
    """Processes function calls from one turn; returns (answered, summary, analysis)."""
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

            error_msg = _validate_timeline_events(timeline_events, start_time, end_time)
            if error_msg is not None:
                _append_rejection(
                    sub_agent_contents,
                    original_parts,
                    function_calls,
                    full_text,
                    fc.name,
                    error_msg,
                )
                answered = "error"
                break

            await _persist_timeline_events(
                analyzer, timeline_events, media, start_time, end_time, specific_query
            )

            answered = True
            break

    if answered is True or answered == "error":
        return answered, final_summary, final_analysis

    _append_rejection(
        sub_agent_contents,
        original_parts,
        function_calls,
        full_text,
        function_calls[0].name,
        (
            "Tool not recognized."
            " Please use"
            " submit_answer."
        ),
    )
    return answered, final_summary, final_analysis


def _validate_timeline_events(
    timeline_events: list, start_time: float, end_time: float | None
) -> str | None:
    """Returns the first validation error message for the submitted events, if any."""
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
            return (
                "Error: 'confidence_score' is"
                " missing or out of bounds in"
                " one of the timeline_events."
                " It must be a float between"
                " 0.0 and 1.0 inclusive. Fix"
                " the payload and call"
                " submit_answer again."
            )

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
            return (
                "Error:"
                " 'verification_timestamp_secs'"
                " is missing or invalid in one"
                " of the timeline_events. Fix"
                " the payload and call"
                " submit_answer again."
            )

        verification_ts = float(
            event.get(
                "verification_timestamp_secs",
                start_time,
            )
        )
        confidence = float(event.get("confidence_score", 0.0))  # noqa: F841

        if verification_ts < start_time or (
            end_time is not None and verification_ts > end_time
        ):
            return (
                "Error:"
                " 'verification_timestamp_secs'"
                f" {verification_ts} must fall"
                " within the start_time"
                f" ({start_time}) and end_time"
                f" ({end_time}) boundaries. Fix"
                " the payload and call"
                " submit_answer again."
            )
    return None


def _append_rejection(
    sub_agent_contents: list,
    original_parts: list,
    function_calls: list,
    full_text: str,
    response_name: str,
    error_msg: str,
) -> None:
    """Appends the model turn plus an error function response to the conversation."""
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
                    name=response_name,
                    response={"error": (error_msg)},
                )
            ],
        )
    )


async def _persist_timeline_events(
    analyzer,
    timeline_events: list,
    media: _ChunkMedia,
    start_time: float,
    end_time: float | None,
    specific_query: str,
) -> None:
    """Captures verification screenshots and records validated events on the blackboard."""
    for event in timeline_events:
        verification_ts = float(
            event.get(
                "verification_timestamp_secs",
                start_time,
            )
        )
        confidence = float(event.get("confidence_score", 0.0))
        relative_offset = max(0.0, verification_ts - media.actual_start)

        out_dir = _va.get_temp_dir("video_analyzer")
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
        analyzer.local_files_to_cleanup.add(screenshot_output_path)
        ffmpeg_cmd = [
            "ffmpeg",
            "-ss",
            str(relative_offset),
            "-i",
            str(media.path),
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

        analyzer._record_blackboard_entry(entry)
