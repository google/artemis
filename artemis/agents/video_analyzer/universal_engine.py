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

"""Universal (OpenAI-compatible / LangChain) execution paths for the video analyzer.

Extracted from ``video_analyzer.py`` as a pure structural split.  Patchable
collaborators (``extract_keyframes_from_video``, ``extract_audio_from_video``,
semaphores, retry helper, ...) are looked up late through the facade module so
``mock.patch("artemis.agents.video_analyzer.video_analyzer.<name>")`` targets
keep working.
"""

import asyncio
import base64
import glob
from pathlib import Path
import re
from typing import Any

from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from artemis.agents.video_analyzer import video_analyzer as _va
from artemis.agents.video_analyzer.reliability import classify_video_failure
from artemis.data_engine.trace import CURRENT_TRACE_ID, TraceSpan
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


async def exec_single_chunk_universal(
    analyzer,
    compressed_path: Path,
    raw_path: Path,
    start_time: float,
    end_time: float | None,
    actual_start: float,
    prompt_with_context: str,
    specific_query: str,
    force_fallback: bool = False,
) -> str:
    """Executes sub-agent chunk analysis via keyframe extraction and LangChain ChatModel."""
    keyframes = _gather_universal_frames(analyzer, raw_path, start_time, end_time, actual_start)

    user_blocks, audio_block = await _build_universal_user_blocks(
        analyzer, keyframes, actual_start, prompt_with_context, raw_path
    )

    messages = [
        SystemMessage(content=analyzer.sub_system_prompt),
        HumanMessage(content=user_blocks),
    ]

    trace_id = CURRENT_TRACE_ID.get()
    if analyzer.ctx.data_engine and trace_id:
        analyzer.ctx.data_engine.record_trace(
            type="llm_call",
            name="video_sub_agent_universal",
            payload={
                "num_keyframes": len(keyframes),
                "prompt": prompt_with_context,
                "system_instruction": analyzer.sub_system_prompt,
            },
            parent_trace_id=trace_id,
        )

    response = await _invoke_universal_with_visual_retry(
        analyzer, messages, user_blocks, audio_block, force_fallback
    )

    final_summary, final_analysis, timeline_events = _extract_submit_payload(response)

    await _persist_universal_events(
        analyzer,
        timeline_events,
        start_time,
        end_time,
        actual_start,
        raw_path,
        specific_query,
        final_summary,
    )

    end_str = f"{end_time:.1f}s" if end_time is not None else "unknown"
    return (
        f"[from {start_time:.1f}s to {end_str}] Summary: {final_summary} Analysis: {final_analysis}"
    )


def _gather_universal_frames(
    analyzer,
    raw_path: Path,
    start_time: float,
    end_time: float | None,
    actual_start: float,
) -> list:
    """Extracts uniform keyframes plus dense action-window frames, merged by timestamp."""
    # Universal providers consume stills rather than the uploaded clip. Use
    # the raw extracted segment so slowdown/transcode PTS never skew labels.
    keyframes = _va.extract_keyframes_from_video(
        raw_path,
        fps=1.0,
        max_frames=30,
        max_dimension=1080,
    )
    dense_offsets = analyzer._dense_action_offsets(
        start_time,
        end_time if end_time is not None else start_time,
        actual_start,
    )
    targeted_frames = _va.extract_frames_at_timestamps(
        raw_path,
        dense_offsets,
        max_frames=analyzer.max_dense_action_frames,
        max_dimension=1080,
    )
    merged_frames = {round(timestamp, 3): data for timestamp, data in keyframes}
    merged_frames.update({round(timestamp, 3): data for timestamp, data in targeted_frames})
    return sorted(merged_frames.items())


async def _build_universal_user_blocks(
    analyzer,
    keyframes: list,
    actual_start: float,
    prompt_with_context: str,
    raw_path: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Builds the multimodal user content blocks (text, frames, optional audio)."""
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

    audio_block = None
    try:
        async with _va.TRANSCODE_SEMAPHORE:
            audio_path = await _va.extract_audio_from_video(raw_path)
        analyzer.local_files_to_cleanup.add(audio_path)
        analyzer.local_dirs_to_cleanup.add(audio_path.parent)
        audio_block = analyzer._audio_content_block(audio_path)
        user_blocks.append(audio_block)
    except Exception as audio_error:
        logger.info(f"No usable audio track for universal video chunk: {audio_error}")

    return user_blocks, audio_block


async def _invoke_universal_with_visual_retry(
    analyzer,
    messages: list[BaseMessage],
    user_blocks: list[dict[str, Any]],
    audio_block: dict[str, Any] | None,
    force_fallback: bool,
):
    """Invokes the universal model, retrying visually when audio input is rejected."""
    with TraceSpan(name="universal_sub_agent_call") as span:
        try:
            response = await analyzer._invoke_universal_model(
                messages,
                [_va.UNIVERSAL_SUBMIT_ANSWER_TOOL],
                timeout=analyzer.model_call_timeout_seconds,
                label="Universal video sub-agent",
                force_fallback=force_fallback,
            )
        except Exception as multimodal_error:
            failure = classify_video_failure(multimodal_error)
            if audio_block is None or failure.category.value not in {
                "bad_request",
                "media_processing",
            }:
                raise
            logger.warning(
                "Configured video model rejected audio input; retrying this chunk visually"
            )
            visual_blocks = [block for block in user_blocks if block is not audio_block]
            messages = [
                SystemMessage(content=analyzer.sub_system_prompt),
                HumanMessage(content=visual_blocks),
            ]
            response = await analyzer._invoke_universal_model(
                messages,
                [_va.UNIVERSAL_SUBMIT_ANSWER_TOOL],
                timeout=analyzer.model_call_timeout_seconds,
                label="Universal visual-only sub-agent",
                force_fallback=force_fallback,
            )
        span.result = f"Received response (tool_calls={len(getattr(response, 'tool_calls', []))})"
    return response


def _extract_submit_payload(response) -> tuple[str, str, list]:
    """Pulls summary/analysis/timeline events from a universal model response."""
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

    return final_summary, final_analysis, timeline_events


async def _persist_universal_events(
    analyzer,
    timeline_events: list,
    start_time: float,
    end_time: float | None,
    actual_start: float,
    raw_path: Path,
    specific_query: str,
    final_summary: str,
) -> None:
    """Processes timeline events & saves screenshots for the blackboard ledger."""
    for event in timeline_events:
        if not isinstance(event, dict):
            continue
        try:
            v_ts = float(event.get("verification_timestamp_secs", start_time))
            conf = float(event.get("confidence_score", 0.8))
            event_start = float(event.get("start_time", start_time))
            event_end = float(
                event.get(
                    "end_time",
                    end_time if end_time is not None else event_start,
                )
            )
        except (TypeError, ValueError):
            logger.warning(f"Ignoring malformed universal video event: {event}")
            continue
        interval_end = end_time if end_time is not None else max(event_end, v_ts)
        if (
            not 0.0 <= conf <= 1.0
            or event_end < event_start
            or event_start < start_time
            or event_end > interval_end
            or v_ts < start_time
            or v_ts > interval_end
        ):
            logger.warning(f"Ignoring out-of-range universal video event: {event}")
            continue

        screenshot_path_str = None
        try:
            rel_offset = max(0.0, v_ts - actual_start)
            out_dir = _va.get_temp_dir("video_analyzer")
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
                analyzer.local_files_to_cleanup.add(shot_path)
                screenshot_path_str = str(shot_path)
        except Exception as fe:
            logger.warning(f"Failed extracting keyframe for event {v_ts}s: {fe}")

        entry = {
            "start": event_start,
            "end": event_end,
            "target": specific_query,
            "summary": event.get("transcription", final_summary),
            "confidence_score": conf,
        }
        if screenshot_path_str:
            entry["screenshot"] = screenshot_path_str
        analyzer._record_blackboard_entry(entry)


async def exec_analyze_audio_universal(
    analyzer,
    audio_path: Path,
    start_time: float,
    end_time: float | None,
    actual_start: float,
    prompt_with_context: str,
    specific_query: str,
    force_fallback: bool = False,
) -> str:
    """Executes audio analysis via Universal LangChain ChatModel."""
    messages = [
        SystemMessage(content=analyzer.audio_system_prompt),
        HumanMessage(
            content=[
                {"type": "text", "text": prompt_with_context},
                analyzer._audio_content_block(audio_path),
            ]
        ),
    ]
    response = await analyzer._invoke_universal_model(
        messages,
        [_va.UNIVERSAL_SUBMIT_ANSWER_TOOL],
        timeout=analyzer.model_call_timeout_seconds,
        label="Universal audio sub-agent",
        force_fallback=force_fallback,
    )
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
    analyzer._record_blackboard_entry(
        {
            "start": start_time,
            "end": end_val,
            "target": specific_query,
            "summary": final_summary,
            "confidence_score": 0.8,
        },
        modality="audio",
    )

    end_str = f"{end_time:.1f}s" if end_time is not None else "unknown"
    return (
        f"[from {start_time:.1f}s to {end_str}] Audio Summary: {final_summary}"
        f" Analysis: {final_analysis}"
    )


async def run_universal(
    analyzer,
    time_description: str,
    purpose: str,
    system_prompt: str,
    *,
    force_fallback: bool = False,
) -> tuple[str, str]:
    """Runs VideoAnalyzer main reasoning loop using Universal LangChain BaseChatModel."""
    messages: list[BaseMessage] = [
        SystemMessage(content=system_prompt),
        HumanMessage(
            content=(
                f"Time description: {time_description}\n"
                f"Purpose: {purpose}\n"
                f"Persistent video blackboard: {analyzer.blackboard_ledger}\n"
                "Reuse sufficient prior evidence and do not request an identical "
                "interval/query analysis again.\n"
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
            response = await _va._invoke_with_retry(
                lambda: analyzer._invoke_universal_model(
                    messages,
                    _va.UNIVERSAL_MAIN_TOOLS,
                    timeout=300,
                    label="Universal video coordinator",
                    force_fallback=force_fallback,
                ),
                "Universal video coordinator",
            )
            messages.append(response)

            if not getattr(response, "tool_calls", None):
                agent_outcome = str(response.content)
                break

            for tc in response.tool_calls:
                t_name = tc["name"]
                t_args = tc.get("args", {})
                t_id = tc.get("id", f"call_{iterations}")
                logger.info(f"Executing universal tool '{t_name}' with args {t_args}")

                res = await _execute_universal_tool(analyzer, t_name, t_args)

                messages.append(ToolMessage(tool_call_id=t_id, name=t_name, content=str(res)))

        if not agent_outcome:
            agent_outcome = "Video analyzer completed all turns without error."

    except Exception as e:
        logger.error(f"Universal video analyzer failed: {e}")
        agent_outcome = f"Video analysis failed: {e}"
        status = "error"

    analyzer._record_reliability_metrics()
    return agent_outcome, status


async def _execute_universal_tool(analyzer, t_name: str, t_args: dict) -> str:
    """Dispatches one universal coordinator tool call, converting errors to text."""
    try:
        if t_name == "extract_segment_metadata":
            res = await analyzer.exec_extract_segment_metadata(
                t_args.get("start_time"), t_args.get("end_time")
            )
        elif t_name == "spawn_sub_agent":
            res = await analyzer.exec_spawn_sub_agent(
                t_args.get("start_time"),
                t_args.get("end_time"),
                t_args.get("specific_query"),
            )
        elif t_name == "analyze_audio_only":
            res = await analyzer.exec_analyze_audio_only(
                t_args.get("start_time"),
                t_args.get("end_time"),
                t_args.get("specific_query"),
            )
        else:
            res = f"Error: Tool '{t_name}' is not recognized."
    except Exception as tool_err:
        logger.error(f"Error executing tool '{t_name}': {tool_err}")
        res = f"Error executing tool '{t_name}': {tool_err}"
    return res
