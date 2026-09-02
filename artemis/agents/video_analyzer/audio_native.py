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

"""Native Gemini execution path for audio-only segment analysis.

Extracted from ``video_analyzer.py`` as a pure structural split.  Patchable
collaborators are looked up late through the facade module so
``mock.patch("artemis.agents.video_analyzer.video_analyzer.<name>")`` targets
keep working.
"""

import asyncio
from pathlib import Path

from google.genai import types

from artemis.agents.video_analyzer import video_analyzer as _va
from artemis.agents.video_analyzer.reliability import classify_video_failure
from artemis.constants import SAFETY_SETTINGS_BLOCK_NONE
from artemis.data_engine.trace import TraceSpan
from artemis.llm.reliability import retry_policy_for
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


class _AudioMedia:
    """Mutable holder for per-attempt audio artifacts shared with failure handling."""

    __slots__ = ("audio_path", "actual_start", "prompt_with_context")

    def __init__(self):
        self.audio_path: Path | None = None
        self.actual_start: float | None = None
        self.prompt_with_context: str | None = None


async def exec_analyze_audio_only(
    analyzer, start_time: float, end_time: float | None, specific_query: str
) -> str:
    """Analyzes only the audio track of a segment, with claim/lease and retries."""
    if not isinstance(start_time, (int, float)):
        return "Error: start_time must be a numeric recording-relative timestamp."
    if not str(specific_query or "").strip():
        return "Error: specific_query must describe the audio evidence to find."
    max_retries = analyzer.native_max_retries
    current_start = round(start_time, 1) if isinstance(start_time, (int, float)) else start_time
    current_end = round(end_time, 1) if isinstance(end_time, (int, float)) else end_time

    if current_end is None:
        controller = _va.get_controller(analyzer.ctx)
        async with _va.TRANSCODE_SEMAPHORE:
            metadata = await controller.extract_segment_metadata(current_start, None)
        duration = getattr(metadata, "duration_seconds", None)
        actual_start = getattr(metadata, "actual_start_relative_time", current_start)
        if not metadata.success or not isinstance(duration, (int, float)) or duration <= 0:
            return f"Error fetching audio segment metadata: {metadata.message}"
        current_start = round(float(actual_start), 1)
        current_end = round(current_start + float(duration), 1)
        if metadata.video_path:
            metadata_path = Path(metadata.video_path)
            analyzer.local_files_to_cleanup.add(metadata_path)
            analyzer.local_dirs_to_cleanup.add(metadata_path.parent)
    if current_end <= current_start:
        return "Error: end_time must be greater than start_time."

    device_id = getattr(getattr(analyzer.ctx, "device", None), "device_id", None)
    active_session = _va.get_active_session(device_id) if device_id else None
    claim = analyzer.blackboard.claim_segment(
        current_start,
        current_end,
        specific_query,
        modality="audio",
        model_name=analyzer.model_name,
        source_generation=(
            active_session.generation if active_session is not None else None
        ),
    )
    if claim.state == "cached":
        result = (
            f"[from {current_start:.1f}s to {current_end:.1f}s] Audio Summary: "
            f"{claim.summary or 'No audio summary provided.'}"
        )
        if claim.analysis:
            result += f" Analysis: {claim.analysis}"
        return "CACHED AUDIO ANALYSIS: " + result
    if claim.state == "in_progress":
        return (
            f"[from {current_start:.1f}s to {current_end:.1f}s] "
            "Audio analysis is already in progress; do not duplicate it."
        )
    lease_owner = claim.lease_owner
    if lease_owner is None:
        raise RuntimeError("Video blackboard returned an audio claim without a lease owner")

    media = _AudioMedia()

    for attempt in range(max_retries + 1):
        try:
            logger.info(
                f"Attempt {attempt + 1}/{max_retries + 1} for"
                f" analyze_audio_only: start={current_start},"
                f" end={current_end}"
            )
            result = await _prepare_audio_media(analyzer, media, current_start, current_end)
            media.prompt_with_context = _build_audio_prompt(
                analyzer, result, media.actual_start, start_time, end_time, specific_query
            )

            if not analyzer.use_native_gemini:
                audio_result = await analyzer._exec_analyze_audio_universal(
                    audio_path=media.audio_path,
                    start_time=current_start,
                    end_time=current_end,
                    actual_start=media.actual_start,
                    prompt_with_context=media.prompt_with_context,
                    specific_query=specific_query,
                )
                summary, analysis = analyzer._parse_chunk_result(audio_result)
                analyzer.blackboard.complete_segment(
                    current_start,
                    current_end,
                    specific_query,
                    lease_owner,
                    summary,
                    analysis,
                    modality="audio",
                )
                return audio_result

            return await _run_native_audio_conversation(
                analyzer,
                media,
                current_start,
                current_end,
                start_time,
                end_time,
                specific_query,
                lease_owner,
            )

        except Exception as e:
            # Same contract as the video sub-agent loop: classification
            # owns the retry decision and every retry restarts the attempt
            # from scratch.
            failure = classify_video_failure(e)
            attempts_so_far = attempt + 1
            if not failure.retryable:
                logger.warning(
                    f"Audio non-retryable failure [{failure.category.value}]"
                    f" on attempt {attempts_so_far}: {e}"
                )
                return await _finish_failed_audio(
                    analyzer,
                    media,
                    current_start,
                    current_end,
                    specific_query,
                    lease_owner,
                    e,
                    retryable=False,
                )
            policy = retry_policy_for(failure.category)
            allowed_attempts = min(max_retries + 1, policy.max_attempts)
            if attempts_so_far >= allowed_attempts:
                logger.warning(
                    f"Audio analysis exhausted {attempts_so_far} attempt(s)"
                    f" [{failure.category.value}]: {e}"
                )
                return await _finish_failed_audio(
                    analyzer,
                    media,
                    current_start,
                    current_end,
                    specific_query,
                    lease_owner,
                    e,
                )
            delay = policy.delay_for(attempts_so_far)
            logger.warning(
                f"Audio retryable failure [{failure.category.value}]"
                f" ({attempts_so_far}/{allowed_attempts}): {e}; retrying"
                f" from scratch in {delay:.2f}s"
            )
            _va._record_llm_retry(
                str(e),
                delay,
                attempt=attempts_so_far,
                max_retries=allowed_attempts,
                source="video_audio_agent",
            )
            await asyncio.sleep(delay)


async def _finish_failed_audio(
    analyzer,
    media: _AudioMedia,
    current_start: float,
    current_end: float,
    specific_query: str,
    lease_owner: str,
    error: Exception,
    *,
    retryable: bool | None = None,
):
    """Records terminal audio failure, attempting the universal fallback first."""
    failure = classify_video_failure(error)
    if (
        analyzer.use_native_gemini
        and failure.should_fallback
        and media.audio_path is not None
        and media.actual_start is not None
        and media.prompt_with_context is not None
    ):
        try:
            logger.warning(
                "Native audio analysis failed"
                f" [{failure.category.value}]; using universal fallback"
            )
            _va._record_llm_event(
                "llm_fallback",
                {
                    "reason": "native_audio_analysis_failed",
                    "category": failure.category.value,
                    "error": str(error)[:500],
                },
            )
            fallback_result = await analyzer._exec_analyze_audio_universal(
                audio_path=media.audio_path,
                start_time=current_start,
                end_time=current_end,
                actual_start=media.actual_start,
                prompt_with_context=media.prompt_with_context,
                specific_query=specific_query,
                force_fallback=True,
            )
            summary, analysis = analyzer._parse_chunk_result(fallback_result)
            analyzer.blackboard.complete_segment(
                current_start,
                current_end,
                specific_query,
                lease_owner,
                summary,
                analysis,
                modality="audio",
            )
            return fallback_result
        except Exception as fallback_error:
            error = fallback_error
            failure = classify_video_failure(error)
            retryable = None
    analyzer.blackboard.fail_segment(
        current_start,
        current_end,
        specific_query,
        lease_owner,
        str(error),
        modality="audio",
        retryable=failure.retryable if retryable is None else retryable,
        error_category=failure.category.value,
    )
    raise error


async def _prepare_audio_media(
    analyzer, media: _AudioMedia, current_start: float, current_end: float
):
    """Extracts the segment and its audio track, tracking artifacts for cleanup."""
    controller = _va.get_controller(analyzer.ctx)
    async with _va.TRANSCODE_SEMAPHORE:
        result = await controller.extract_segment_metadata(current_start, current_end)
    if not result.success or not result.video_path:
        raise Exception(f"Failed to get video segment: {result.message}")

    path = Path(result.video_path)
    analyzer.local_files_to_cleanup.add(path)
    analyzer.local_dirs_to_cleanup.add(path.parent)

    async with _va.TRANSCODE_SEMAPHORE:
        audio_path = await _va.extract_audio_from_video(path)
    analyzer.local_files_to_cleanup.add(audio_path)
    analyzer.local_dirs_to_cleanup.add(audio_path.parent)
    media.audio_path = audio_path
    actual_start = getattr(result, "actual_start_relative_time", current_start)
    if not isinstance(actual_start, (int, float)):
        actual_start = current_start
    media.actual_start = actual_start
    return result


def _build_audio_prompt(
    analyzer,
    result,
    actual_start: float,
    start_time: float,
    end_time: float | None,
    specific_query: str,
) -> str:
    """Assembles the audio sub-agent prompt with timing context, ledger, and warnings."""
    warnings = analyzer.get_overlapping_warnings(start_time, end_time)
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
    if analyzer.enable_ledger:
        prompt_with_context += (
            "BLACKBOARD"
            f" LEDGER:\n{analyzer.blackboard_ledger}\nDirective:"
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
    return prompt_with_context


async def _run_native_audio_conversation(
    analyzer,
    media: _AudioMedia,
    current_start: float,
    current_end: float,
    start_time: float,
    end_time: float | None,
    specific_query: str,
    lease_owner: str,
) -> str:
    """Uploads the audio and drives the native Gemini audio agent to a committed answer."""
    file = None
    try:
        with TraceSpan(name="upload_audio_to_gemini") as span:
            file = await analyzer.upload_and_poll_file(media.audio_path)
            span.result = f"Uploaded {file.name}"
        logger.info(
            f"Invoking Gemini for audio-only task with model {analyzer.model_name}..."
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

        (
            final_summary,
            final_analysis,
            final_confidence_score,
        ) = await _drive_audio_agent_loop(analyzer, sub_agent_contents)

        end_val = end_time if end_time is not None else "unknown"
        analyzer._record_blackboard_entry(
            {
                "start": start_time,
                "end": end_val,
                "target": specific_query,
                "summary": final_summary,
                "confidence_score": final_confidence_score,
            },
            modality="audio",
        )

        end_str = f"{end_time:.1f}s" if end_time is not None else "unknown"
        audio_result = (
            f"[from {start_time:.1f}s to {end_str}] Summary:"
            f" {final_summary} Analysis: {final_analysis}"
        )
        analyzer.blackboard.complete_segment(
            current_start,
            current_end,
            specific_query,
            lease_owner,
            final_summary,
            final_analysis,
            modality="audio",
        )
        return audio_result
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


async def _drive_audio_agent_loop(
    analyzer, sub_agent_contents: list
) -> tuple[str, str, float]:
    """Iterates the audio agent conversation until submit_answer is accepted."""
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
                analyzer.client.aio.models.generate_content(
                    model=analyzer.model_name,
                    contents=sub_agent_contents,
                    config=types.GenerateContentConfig(
                        system_instruction=analyzer.audio_system_prompt,
                        tools=[
                            types.Tool(
                                function_declarations=[analyzer.submit_answer_tool]
                            )
                        ],
                        safety_settings=SAFETY_SETTINGS_BLOCK_NONE,
                    ),
                ),
                timeout=analyzer.model_call_timeout_seconds,
            )

        full_text = response.text or ""
        final_full_text += full_text + "\n"
        function_calls = response.function_calls or []

        if function_calls:
            (
                answered,
                final_summary,
                final_analysis,
                final_confidence_score,
            ) = _handle_audio_function_calls(
                sub_agent_contents,
                response,
                function_calls,
                final_summary,
                final_analysis,
                final_confidence_score,
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

    return final_summary, final_analysis, final_confidence_score


def _handle_audio_function_calls(
    sub_agent_contents: list,
    response,
    function_calls: list,
    final_summary: str,
    final_analysis: str,
    final_confidence_score: float,
):
    """Processes audio-agent function calls; returns (answered, summary, analysis, score)."""
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

    if answered is True or answered == "error":
        return answered, final_summary, final_analysis, final_confidence_score

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
    return answered, final_summary, final_analysis, final_confidence_score
