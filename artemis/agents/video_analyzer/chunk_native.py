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

"""Native Gemini execution path for a single video chunk sub-agent.

Extracted from ``video_analyzer.py`` as a pure structural split.  Patchable
collaborators (``get_controller``, ``compress_video_for_api``, semaphores,
LLM event recorders, ...) are looked up late through the facade module so
``mock.patch("artemis.agents.video_analyzer.video_analyzer.<name>")`` targets
keep working.  The sub-agent conversation itself lives in
``chunk_conversation``.
"""

import asyncio
from pathlib import Path

from artemis.agents.video_analyzer import video_analyzer as _va
from artemis.agents.video_analyzer.chunk_conversation import (
    _ChunkMedia,
    _run_native_chunk_conversation,
)
from artemis.agents.video_analyzer.reliability import classify_video_failure
from artemis.llm.reliability import retry_policy_for
from artemis.utils.logger import get_logger

logger = get_logger(__name__)


async def exec_single_chunk(
    analyzer, start_time: float, end_time: float | None, specific_query: str
) -> str:
    """Analyzes one closed chunk interval, with claim/lease and retry handling."""
    max_retries = analyzer.native_max_retries
    current_start = round(start_time, 1) if isinstance(start_time, (int, float)) else start_time
    current_end = round(end_time, 1) if isinstance(end_time, (int, float)) else end_time

    if not isinstance(current_start, (int, float)) or not isinstance(current_end, (int, float)):
        raise ValueError("Child video analysis requires a closed numeric interval")

    device_id = getattr(getattr(analyzer.ctx, "device", None), "device_id", None)
    active_session = _va.get_active_session(device_id) if device_id else None
    claim = analyzer.blackboard.claim_segment(
        current_start,
        current_end,
        specific_query,
        model_name=analyzer.model_name,
        source_generation=(active_session.generation if active_session is not None else None),
    )
    if claim.state == "cached":
        cached = (
            f"[from {current_start:.1f}s to {current_end:.1f}s] Summary: "
            f"{claim.summary or 'No summary provided.'}"
        )
        if claim.analysis:
            cached += f" Analysis: {claim.analysis}"
        return cached
    if claim.state == "in_progress":
        return (
            f"[from {current_start:.1f}s to {current_end:.1f}s] "
            "Analysis is already in progress in another video agent; do not duplicate it."
        )
    lease_owner = claim.lease_owner
    if lease_owner is None:
        raise RuntimeError("Video blackboard returned a claim without a lease owner")

    media = _ChunkMedia()

    for attempt in range(max_retries + 1):
        try:
            logger.info(
                f"Attempt {attempt + 1}/{max_retries + 1} for sub-agent:"
                f" start={current_start}, end={current_end}"
            )
            result, slowdown_factor = await _prepare_chunk_media(
                analyzer, media, current_start, current_end
            )
            media.prompt_with_context = _build_chunk_prompt(
                analyzer,
                result,
                media.actual_start,
                slowdown_factor,
                start_time,
                end_time,
                specific_query,
            )

            if not analyzer.use_native_gemini:
                chunk_result = await analyzer._exec_single_chunk_universal(
                    compressed_path=media.compressed_path,
                    raw_path=media.path,
                    start_time=current_start,
                    end_time=current_end,
                    actual_start=media.actual_start,
                    prompt_with_context=media.prompt_with_context,
                    specific_query=specific_query,
                )
                summary, analysis = analyzer._parse_chunk_result(chunk_result)
                analyzer.blackboard.complete_segment(
                    current_start,
                    current_end,
                    specific_query,
                    lease_owner,
                    summary,
                    analysis,
                )
                return chunk_result

            return await _run_native_chunk_conversation(
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
            # Retry decisions are owned by the shared classification layer.
            # Every attempt restarts from scratch (segment re-extracted,
            # file re-uploaded, stream re-consumed); partially accumulated
            # stream output from the failed attempt is discarded, never
            # surfaced as a complete answer.
            failure = classify_video_failure(e)
            attempts_so_far = attempt + 1
            if not failure.retryable:
                logger.warning(
                    f"Sub-agent non-retryable failure [{failure.category.value}]"
                    f" on attempt {attempts_so_far}: {e}"
                )
                return await _finish_failed_chunk(
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
                    f"Sub-agent exhausted {attempts_so_far} attempt(s)"
                    f" [{failure.category.value}]: {e}"
                )
                return await _finish_failed_chunk(
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
                f"Sub-agent retryable failure [{failure.category.value}]"
                f" ({attempts_so_far}/{allowed_attempts}): {e}; retrying"
                f" from scratch in {delay:.2f}s (partial stream output"
                " from the failed attempt is discarded)"
            )
            _va._record_llm_retry(
                str(e),
                delay,
                attempt=attempts_so_far,
                max_retries=allowed_attempts,
                source="video_sub_agent",
            )
            await asyncio.sleep(delay)

    raise RuntimeError(
        f"Video sub-agent exhausted retries for {current_start:.1f}s-{current_end:.1f}s"
    )


async def _finish_failed_chunk(
    analyzer,
    media: _ChunkMedia,
    current_start: float,
    current_end: float,
    specific_query: str,
    lease_owner: str,
    error: Exception,
    *,
    retryable: bool | None = None,
):
    """Records terminal failure, attempting the universal fallback first when eligible."""
    failure = classify_video_failure(error)
    if (
        analyzer.use_native_gemini
        and failure.should_fallback
        and media.path is not None
        and media.compressed_path is not None
        and media.actual_start is not None
        and media.prompt_with_context is not None
    ):
        try:
            logger.warning("Native video chunk exhausted retries; using universal fallback")
            _va._record_llm_event(
                "llm_fallback",
                {
                    "reason": "native_video_chunk_failed",
                    "category": failure.category.value,
                    "error": str(error)[:500],
                },
            )
            fallback_result = await analyzer._exec_single_chunk_universal(
                compressed_path=media.compressed_path,
                raw_path=media.path,
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
        retryable=failure.retryable if retryable is None else retryable,
        error_category=failure.category.value,
    )
    raise error


async def _prepare_chunk_media(
    analyzer, media: _ChunkMedia, current_start: float, current_end: float
):
    """Extracts and compresses the chunk segment, tracking artifacts for cleanup."""
    controller = _va.get_controller(analyzer.ctx)
    async with _va.TRANSCODE_SEMAPHORE:
        result = await controller.extract_segment_metadata(current_start, current_end)
    if not result.success or not result.video_path:
        raise Exception(f"Failed to get video segment: {result.message}")

    path = Path(result.video_path)
    analyzer.local_files_to_cleanup.add(path)
    analyzer.local_dirs_to_cleanup.add(path.parent)
    media.path = path

    actual_start = getattr(result, "actual_start_relative_time", current_start)
    if not isinstance(actual_start, (int, float)):
        actual_start = current_start
    media.actual_start = actual_start

    slowdown_factor = 1.0
    if current_end is not None and (current_end - current_start) <= _va.SLOWDOWN_THRESHOLD_SECONDS:
        slowdown_factor = _va.SLOWDOWN_FACTOR

    async with _va.TRANSCODE_SEMAPHORE:
        compressed_path = await _va.compress_video_for_api(
            path,
            force_compress=True,
            start_offset_seconds=actual_start,
            slowdown_factor=slowdown_factor,
        )
    if compressed_path != path:
        analyzer.local_files_to_cleanup.add(compressed_path)
        analyzer.local_dirs_to_cleanup.add(compressed_path.parent)
    media.compressed_path = compressed_path

    actual_start = getattr(result, "actual_start_relative_time", current_start)
    if not isinstance(actual_start, (int, float)):
        actual_start = current_start
    media.actual_start = actual_start
    return result, slowdown_factor


def _build_chunk_prompt(
    analyzer,
    result,
    actual_start: float,
    slowdown_factor: float,
    start_time: float,
    end_time: float | None,
    specific_query: str,
) -> str:
    """Assembles the sub-agent prompt with timing context, ledger, and warnings."""
    warnings = analyzer.get_overlapping_warnings(start_time, end_time)
    warning_block = ""
    if warnings:
        lines = ["WARNING: The following queries were already tried in this timeframe:\n"]
        for w in warnings:
            lines.append(f"Searched for: {w['target']} -> {w['summary']}")
        warning_block = "\n".join(lines) + "\n\n"

    duration_secs = getattr(result, "duration_seconds", None)
    actual_end = actual_start + duration_secs if isinstance(duration_secs, (int, float)) else None
    end_str = f" to {actual_end:.1f}s" if actual_end is not None else ""
    warning_val = getattr(result, "warning", None)
    truncation_note = (
        f" (NOTE: {warning_val})" if isinstance(warning_val, str) and warning_val else ""
    )

    slowdown_note = ""
    if slowdown_factor != 1.0:
        slowdown_note = "WARNING: This video is slowed down to capture fast micro-actions.\n\n"

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
            f"{warning_block}Directive: Avoid repeating"
            " searches that have already failed. Try to"
            " approach the problem from a different angle or"
            " use a different search strategy.\n\n"
        )
    prompt_with_context += f"{specific_query}"
    return prompt_with_context
