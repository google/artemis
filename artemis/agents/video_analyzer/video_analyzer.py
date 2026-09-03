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

"""VideoAnalyzer facade: entry point and shared runtime state.

The execution engines live in sibling modules (``chunk_native``,
``audio_native``, ``universal_engine``, ``native_coordinator``,
``gemini_files``, ``universal_tools``) and resolve their patchable
collaborators through THIS module's namespace, so existing
``mock.patch("artemis.agents.video_analyzer.video_analyzer.<name>")``
targets keep working.  Keep the imports below in place even when the facade
body no longer references them directly.
"""

import asyncio
import base64
from datetime import datetime
import glob
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from typing import Any

from google import genai
from google.genai import types
from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from artemis.agents.video_analyzer.conflict_resolution import (
    ConflictResolutionService,
)
from artemis.agents.video_analyzer.blackboard import get_video_blackboard
from artemis.agents.video_analyzer.gemini_files import (
    cleanup_abandoned_gemini_files,
)
from artemis.agents.video_analyzer.reliability import (
    VideoCircuitBreaker,
    classify_video_failure,
)
from artemis.agents.video_analyzer.universal_tools import (
    UNIVERSAL_MAIN_TOOLS,
    UNIVERSAL_SUBMIT_ANSWER_TOOL,
    build_native_tools_declaration,
    build_submit_answer_declaration,
)
from artemis.config import get_temp_dir, settings
from artemis.constants import SAFETY_SETTINGS_BLOCK_NONE
from artemis.context import ArtemisContext
from artemis.controllers.controller_factory import get_controller
from artemis.data_engine.trace import CURRENT_TRACE_ID, TraceSpan, trace
from artemis.llm.reliability import retry_policy_for
from artemis.services.llm import _record_llm_event, _record_llm_retry, get_llm
from artemis.utils.logger import get_logger
from artemis.utils.video import (
    compress_video_for_api,
    extract_audio_from_video,
    extract_frames_at_timestamps,
    extract_keyframes_from_video,
    get_active_session,
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


async def _invoke_with_retry(operation, label: str, max_attempts: int = 3):
    """Retry side-effect-free model coordination calls using shared classification.

    Retry decisions come from ``classify_video_failure`` plus the shared
    per-category policies in :mod:`artemis.llm.reliability` instead of ad-hoc
    status-code sets.  ``operation`` must be restartable from scratch: each
    retry re-invokes it and any partially accumulated result (e.g. consumed
    stream chunks) from the failed attempt is discarded, never surfaced as a
    complete output.  The original exception is re-raised on giving up so
    downstream ``classify_video_failure`` consumers keep seeing the provider's
    native error shape.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return await operation()
        except Exception as exc:
            failure = classify_video_failure(exc)
            if not failure.retryable:
                logger.warning(
                    f"{label} non-retryable failure [{failure.category.value}] "
                    f"on attempt {attempt}: {exc}"
                )
                raise
            policy = retry_policy_for(failure.category)
            allowed_attempts = min(max_attempts, policy.max_attempts)
            if attempt >= allowed_attempts:
                logger.warning(
                    f"{label} exhausted {attempt} attempt(s) [{failure.category.value}]: {exc}"
                )
                raise
            delay = policy.delay_for(attempt)
            logger.warning(
                f"{label} retryable failure [{failure.category.value}] "
                f"({attempt}/{allowed_attempts}): {exc}; retrying from scratch "
                f"in {delay:.2f}s (partial output from the failed attempt is discarded)"
            )
            _record_llm_retry(
                str(exc),
                delay,
                attempt=attempt,
                max_retries=allowed_attempts,
                source=label,
            )
            await asyncio.sleep(delay)


class VideoAnalyzer:
    def __init__(self, ctx: ArtemisContext):
        self.ctx = ctx
        self.blackboard = get_video_blackboard(ctx)
        self.blackboard_entries = self.blackboard.list_ledger_entries()
        agent_config = getattr(ctx, "agent_config", None)
        self.video_config = getattr(agent_config, "video_analyzer", None)
        self.enable_ledger = getattr(
            self.video_config,
            "enable_ledger",
            getattr(agent_config, "enable_video_ledger", True),
        )
        self.chunk_size_seconds = float(getattr(self.video_config, "chunk_size_seconds", 60.0))
        self.min_chunk_seconds = float(getattr(self.video_config, "min_chunk_seconds", 4.0))
        self.max_split_depth = int(getattr(self.video_config, "max_split_depth", 4))
        self.action_window_seconds = float(getattr(self.video_config, "action_window_seconds", 2.0))
        self.dense_action_fps = float(getattr(self.video_config, "dense_action_fps", 4.0))
        self.max_dense_action_frames = int(
            getattr(self.video_config, "max_dense_action_frames", 24)
        )
        self.native_max_retries = int(getattr(self.video_config, "native_max_retries", 1))
        self.model_call_timeout_seconds = float(
            getattr(self.video_config, "model_call_timeout_seconds", 120.0)
        )
        breaker = getattr(ctx, "_video_circuit_breaker", None)
        if breaker is None:
            breaker = VideoCircuitBreaker(
                threshold=int(getattr(self.video_config, "circuit_breaker_threshold", 3)),
                cooldown_seconds=float(
                    getattr(
                        self.video_config,
                        "circuit_breaker_cooldown_seconds",
                        60.0,
                    )
                ),
            )
            ctx._video_circuit_breaker = breaker
        self.circuit_breaker = breaker
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

    def _get_universal_llm(self, *, use_fallback: bool = False):
        """Resolve the utility-scoped video model, including its configured fallback."""

        return get_llm(
            self.ctx,
            name="video_analyzer",
            is_utils=True,
            use_fallback=use_fallback,
        )

    async def _invoke_universal_model(
        self,
        messages: list[BaseMessage],
        tools: list[dict[str, Any]],
        *,
        timeout: float,
        label: str,
        force_fallback: bool = False,
    ):
        """Invoke primary then fallback model under a context-scoped circuit breaker."""

        primary_key = f"video_analyzer:{self.model_name}:primary"
        attempts = [True] if force_fallback else [False, True]
        last_error: BaseException | None = None
        for use_fallback in attempts:
            if not use_fallback and not self.circuit_breaker.allow(primary_key):
                logger.warning(f"{label}: primary video-model circuit is open; using fallback")
                continue
            try:
                llm = self._get_universal_llm(use_fallback=use_fallback)
            except (ValueError, AttributeError) as config_error:
                if use_fallback:
                    if last_error is not None:
                        raise last_error
                    raise config_error
                raise
            try:
                bound_llm = llm.bind_tools(tools)
                response = await asyncio.wait_for(bound_llm.ainvoke(messages), timeout=timeout)
                if not use_fallback:
                    self.circuit_breaker.record_success(primary_key)
                return response
            except BaseException as error:
                failure = classify_video_failure(error)
                if not use_fallback:
                    self.circuit_breaker.record_failure(primary_key, failure)
                last_error = error
                if use_fallback or not failure.should_fallback:
                    raise
                logger.warning(
                    f"{label}: {failure.category.value} from primary; trying configured fallback"
                )
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"{label}: no video model is available")

    def _record_reliability_metrics(self) -> None:
        """Persist a compact blackboard/circuit snapshot into the active trace."""

        trace_id = CURRENT_TRACE_ID.get()
        if not self.ctx.data_engine or not trace_id:
            return
        try:
            self.ctx.data_engine.record_trace(
                type="video_analysis",
                name="video_analysis_reliability",
                payload={
                    "blackboard": self.blackboard.metrics(),
                    "circuits": self.circuit_breaker.snapshot(),
                },
                parent_trace_id=trace_id,
            )
        except Exception as error:
            logger.warning(f"Failed to record video reliability metrics: {error}")

    async def _cleanup_universal_resources(self) -> None:
        """Release universal-path temporary media without touching durable evidence."""

        if os.environ.get("KEEP_VIDEOS") or os.environ.get("ARTEMIS_DEBUG"):
            return
        for path in list(self.local_files_to_cleanup):
            try:
                if path.exists():
                    path.unlink()
            except OSError as error:
                logger.warning(f"Failed to delete temporary video artifact {path}: {error}")
        protected = {
            Path("/").resolve(),
            Path("/tmp").resolve(),
            Path("/var/tmp").resolve(),
            Path.home().resolve(),
            Path(tempfile.gettempdir()).resolve(),
        }
        for directory in sorted(
            self.local_dirs_to_cleanup,
            key=lambda value: len(value.parts),
            reverse=True,
        ):
            try:
                resolved = directory.resolve()
                if resolved not in protected and directory.exists():
                    shutil.rmtree(directory, ignore_errors=True)
            except OSError as error:
                logger.warning(f"Failed to delete temporary video directory {directory}: {error}")

    def _action_timestamps(self, start: float, end: float) -> list[float]:
        """Return test-relative timestamps for recorded actions in an interval."""

        engine = self.ctx.data_engine
        session_id = getattr(engine, "current_session_id", None) if engine else None
        storage = getattr(engine, "storage", None) if engine else None
        session_start = getattr(engine, "session_start_time", None) if engine else None
        if not storage or not session_id or not isinstance(session_start, (int, float)):
            return []
        try:
            return sorted(
                {
                    round(float(step.timestamp) - float(session_start), 3)
                    for step in storage.get_steps(session_id)
                    if step.action_taken
                    and start <= float(step.timestamp) - float(session_start) <= end
                }
            )
        except Exception as error:
            logger.warning(f"Failed to load action timestamps for video sampling: {error}")
            return []

    def _dense_action_offsets(self, start: float, end: float, actual_start: float) -> list[float]:
        """Build bounded, video-relative sampling offsets around mobile actions."""

        if self.max_dense_action_frames <= 0:
            return []
        interval = 1.0 / max(1.0, self.dense_action_fps)
        offsets: list[float] = []
        for action_time in self._action_timestamps(start, end):
            window_start = max(start, action_time - self.action_window_seconds)
            window_end = min(end, action_time + self.action_window_seconds)
            cursor = window_start
            while cursor <= window_end + 1e-6:
                offsets.append(max(0.0, cursor - actual_start))
                if len(offsets) >= self.max_dense_action_frames:
                    return offsets
                cursor += interval
        return offsets

    @staticmethod
    def _audio_content_block(audio_path: Path) -> dict[str, Any]:
        suffix = audio_path.suffix.lower()
        mime_type = {
            ".aac": "audio/aac",
            ".m4a": "audio/mp4",
            ".mp3": "audio/mpeg",
            ".wav": "audio/wav",
            ".ogg": "audio/ogg",
        }.get(suffix, "application/octet-stream")
        return {
            "type": "audio",
            "base64": base64.b64encode(audio_path.read_bytes()).decode("utf-8"),
            "mime_type": mime_type,
        }

    def _refresh_blackboard(self) -> None:
        """Refresh the presentation ledger from persistent video memory."""
        self.blackboard_entries = self.blackboard.list_ledger_entries()

    def _clean_blackboard(self) -> None:
        """Clean the local view without deleting another agent's durable records."""
        self.blackboard_entries = ConflictResolutionService.clean(self.blackboard_entries)

    def _record_blackboard_entry(
        self, entry: dict[str, Any], *, modality: str = "video"
    ) -> dict[str, Any]:
        """Persist an observation immediately, then update this agent's ledger."""
        stored = self.blackboard.add_observation(entry, modality=modality)
        observation_id = stored.get("observation_id")
        if not any(
            existing.get("observation_id") == observation_id
            for existing in self.blackboard_entries
            if isinstance(existing, dict)
        ):
            self.blackboard_entries.append(stored)
        return stored

    @staticmethod
    def _parse_chunk_result(result: str) -> tuple[str, str | None]:
        """Extract summary/analysis from the stable child response envelope."""
        match = re.search(r"Summary:\s*(.*?)(?:\s+Analysis:\s*(.*))?$", result, re.DOTALL)
        if not match:
            return result.strip(), None
        summary = match.group(1).strip()
        analysis = match.group(2).strip() if match.group(2) else None
        return summary, analysis

    def _init_tools(self) -> None:
        """Initializes native and sub-agent tool declarations."""
        self.tools_declaration = build_native_tools_declaration()
        self.submit_answer_tool = build_submit_answer_declaration()

    def _init_engine(self) -> None:
        """Initializes model engine and decides whether to use native Gemini or Universal path."""
        ctx = self.ctx
        llm_config = getattr(ctx, "llm_config", None)
        utils_cfg = getattr(llm_config, "utils", None) if llm_config else None
        llm_cfg = getattr(utils_cfg, "video_analyzer", None) if utils_cfg else None
        model_str = (
            llm_cfg.model if (llm_cfg and hasattr(llm_cfg, "model")) else "gemini-3.8-flash"
        ).lower()
        self.model_name = (
            llm_cfg.model if (llm_cfg and hasattr(llm_cfg, "model")) else "gemini-3.8-flash"
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
        self._clean_blackboard()
        if not self.blackboard_entries:
            return "No video segments analyzed yet."
        lines = []
        for e in self.blackboard_entries:
            lines.append(f"{e['start']}s - {e['end']}s: {e['summary']}")
        return " | ".join(lines)

    async def upload_and_poll_file(self, compressed_path: Path) -> any:
        from artemis.agents.video_analyzer import gemini_files

        return await gemini_files.upload_and_poll_file(
            self.client, compressed_path, self.cloud_files_to_cleanup
        )

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

    async def _resolve_open_ended_interval(
        self, requested_start: float
    ) -> tuple[float, float | None, str | None]:
        """Resolve an open-ended request to a stable numeric interval (or an error)."""
        controller = get_controller(self.ctx)
        async with TRANSCODE_SEMAPHORE:
            metadata_result = await controller.extract_segment_metadata(requested_start, None)
        if not metadata_result.success:
            return (
                requested_start,
                None,
                f"Error fetching segment metadata: {metadata_result.message}",
            )
        duration = getattr(metadata_result, "duration_seconds", None)
        actual_start = getattr(metadata_result, "actual_start_relative_time", requested_start)
        if not isinstance(duration, (int, float)) or duration <= 0:
            return (
                requested_start,
                None,
                "Error fetching segment metadata: resolved duration is unavailable.",
            )
        if not isinstance(actual_start, (int, float)):
            actual_start = requested_start
        requested_start = round(float(actual_start), 3)
        requested_end = round(requested_start + float(duration), 3)
        if metadata_result.video_path:
            metadata_path = Path(metadata_result.video_path)
            self.local_files_to_cleanup.add(metadata_path)
            self.local_dirs_to_cleanup.add(metadata_path.parent)
        return requested_start, requested_end, None

    @trace(type="tool", name="spawn_sub_agent")
    async def exec_spawn_sub_agent(
        self, start_time: float, end_time: float | None, specific_query: str
    ) -> str:
        if not isinstance(start_time, (int, float)):
            return "Error: start_time must be a numeric recording-relative timestamp."
        if not str(specific_query or "").strip():
            return "Error: specific_query must describe the evidence to find."

        requested_start = round(float(start_time), 3)
        requested_end = round(float(end_time), 3) if isinstance(end_time, (int, float)) else None
        if requested_end is not None and requested_end <= requested_start:
            return "Error: end_time must be greater than start_time."

        # A closed interval does not need a throw-away full-range trim merely
        # to discover its duration. Open-ended requests are resolved once to a
        # stable numeric end before they enter persistent coverage.
        if requested_end is None:
            requested_start, requested_end, error = await self._resolve_open_ended_interval(
                requested_start
            )
            if error is not None:
                return error

        cached_results = self.blackboard.format_cached_segments(
            requested_start, requested_end, specific_query
        )
        missing = self.blackboard.missing_intervals(requested_start, requested_end, specific_query)
        if not missing:
            logger.info(
                "Reusing complete video-blackboard coverage for "
                f"{requested_start:.1f}s-{requested_end:.1f}s"
            )
            return "CACHED VIDEO ANALYSIS: " + " ".join(cached_results)

        target_chunk_size = self.chunk_size_seconds
        chunks: list[tuple[float, float]] = []
        for gap_start, gap_end in missing:
            cursor = gap_start
            while cursor < gap_end:
                chunk_end = min(gap_end, cursor + target_chunk_size)
                chunks.append((round(cursor, 3), round(chunk_end, 3)))
                cursor = chunk_end
        logger.info(
            f"Video blackboard planned {len(chunks)} uncovered chunk(s) for "
            f"{requested_start:.1f}s-{requested_end:.1f}s"
        )

        async def analyze_with_recovery(
            cs: float, ce: float, depth: int = 0
        ) -> tuple[list[str], list[str]]:
            try:
                async with API_SEMAPHORE:
                    value = await self._exec_single_chunk(cs, ce, specific_query)
                return [value], []
            except Exception as error:
                failure = classify_video_failure(error)
                duration = ce - cs
                can_split = (
                    failure.should_split
                    and depth < self.max_split_depth
                    and duration >= self.min_chunk_seconds * 2
                )
                if can_split:
                    midpoint = round(cs + duration / 2.0, 3)
                    logger.warning(
                        f"Splitting failed {cs:.1f}s-{ce:.1f}s chunk at {midpoint:.1f}s "
                        f"after {failure.category.value}"
                    )
                    left, right = await asyncio.gather(
                        analyze_with_recovery(cs, midpoint, depth + 1),
                        analyze_with_recovery(midpoint, ce, depth + 1),
                    )
                    return left[0] + right[0], left[1] + right[1]
                failure_text = f"{cs:.1f}s-{ce:.1f}s [{failure.category.value}]: {error}"
                logger.error(f"Video chunk terminal failure: {failure_text}")
                return [], [failure_text]

        recovered = await asyncio.gather(*(analyze_with_recovery(cs, ce) for cs, ce in chunks))
        fresh_results = [item for values, _ in recovered for item in values]
        failed_chunks = [item for _, failures in recovered for item in failures]

        # Reload committed chunk summaries and event evidence from the shared
        # store before applying presentation-only conflict filtering.
        self._refresh_blackboard()
        self._clean_blackboard()

        valid_results = cached_results + fresh_results
        if not valid_results:
            return "All sub-agent chunks failed. Failed intervals: " + "; ".join(failed_chunks)
        combined = " ".join(valid_results)
        if failed_chunks:
            return (
                "PARTIAL VIDEO ANALYSIS (successful chunks were persisted). "
                f"Failed intervals: {'; '.join(failed_chunks)}. Results: {combined}"
            )
        self._record_reliability_metrics()
        return combined

    async def _exec_single_chunk(
        self, start_time: float, end_time: float | None, specific_query: str
    ) -> str:
        from artemis.agents.video_analyzer import chunk_native

        return await chunk_native.exec_single_chunk(self, start_time, end_time, specific_query)

    @trace(type="tool", name="analyze_audio_only")
    async def exec_analyze_audio_only(
        self, start_time: float, end_time: float | None, specific_query: str
    ) -> str:
        from artemis.agents.video_analyzer import audio_native

        return await audio_native.exec_analyze_audio_only(
            self, start_time, end_time, specific_query
        )

    async def _exec_single_chunk_universal(
        self,
        compressed_path: Path,
        raw_path: Path,
        start_time: float,
        end_time: float | None,
        actual_start: float,
        prompt_with_context: str,
        specific_query: str,
        force_fallback: bool = False,
    ) -> str:
        from artemis.agents.video_analyzer import universal_engine

        return await universal_engine.exec_single_chunk_universal(
            self,
            compressed_path=compressed_path,
            raw_path=raw_path,
            start_time=start_time,
            end_time=end_time,
            actual_start=actual_start,
            prompt_with_context=prompt_with_context,
            specific_query=specific_query,
            force_fallback=force_fallback,
        )

    async def _exec_analyze_audio_universal(
        self,
        audio_path: Path,
        start_time: float,
        end_time: float | None,
        actual_start: float,
        prompt_with_context: str,
        specific_query: str,
        force_fallback: bool = False,
    ) -> str:
        from artemis.agents.video_analyzer import universal_engine

        return await universal_engine.exec_analyze_audio_universal(
            self,
            audio_path=audio_path,
            start_time=start_time,
            end_time=end_time,
            actual_start=actual_start,
            prompt_with_context=prompt_with_context,
            specific_query=specific_query,
            force_fallback=force_fallback,
        )

    async def _run_universal(
        self,
        time_description: str,
        purpose: str,
        system_prompt: str,
        *,
        force_fallback: bool = False,
    ) -> tuple[str, str]:
        from artemis.agents.video_analyzer import universal_engine

        return await universal_engine.run_universal(
            self,
            time_description,
            purpose,
            system_prompt,
            force_fallback=force_fallback,
        )

    @trace(type="agent", name="video_analyzer")
    async def run(self, time_description: str, purpose: str) -> tuple[str, str]:
        ctx = self.ctx
        async with MAIN_AGENT_SEMAPHORE:
            logger.info(
                f"Video analyzer invoked with time: '{time_description}', purpose: '{purpose}'"
            )

            self._refresh_blackboard()

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
                self.model_name = "gemini-3.8-flash"

            # Track files for cleanup
            self.local_files_to_cleanup = set()
            self.local_dirs_to_cleanup = set()
            self.cloud_files_to_cleanup = set()

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
            try:
                return await self._run_universal(
                    time_description=time_description,
                    purpose=purpose,
                    system_prompt=system_prompt,
                )
            finally:
                await self._cleanup_universal_resources()

        from artemis.agents.video_analyzer import native_coordinator

        return await native_coordinator.run_native(
            self,
            ctx,
            time_description,
            purpose,
            system_prompt,
            temperature,
            thinking_level,
        )
