# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage, HumanMessage
import pytest

from artemis.agents.video_analyzer.reliability import (
    AgenticVideoDegraded,
    VideoCircuitBreaker,
    VideoFailureCategory,
    classify_video_failure,
)
from artemis.agents.video_analyzer.video_analyzer import VideoAnalyzer
from artemis.context import ArtemisContext


def _context() -> MagicMock:
    ctx = MagicMock(spec=ArtemisContext)
    ctx._video_blackboard = None
    ctx._video_circuit_breaker = None
    ctx._mobile_controller = None
    ctx.execution_setup = None
    ctx.data_engine = None
    ctx.agent_config = SimpleNamespace(
        video_analyzer=SimpleNamespace(
            enable_ledger=True,
            chunk_size_seconds=60.0,
            min_chunk_seconds=4.0,
            max_split_depth=4,
            circuit_breaker_threshold=2,
            circuit_breaker_cooldown_seconds=30.0,
            action_window_seconds=2.0,
            dense_action_fps=4.0,
            max_dense_action_frames=24,
        )
    )
    ctx.device = SimpleNamespace(device_id="reliability-device")
    ctx.llm_config = MagicMock()
    ctx.llm_config.utils.video_analyzer.model = "primary-video-model"
    ctx.llm_config.utils.video_analyzer.temperature = 0.2
    return ctx


def test_failure_classifier_chooses_safe_recovery():
    outage = classify_video_failure(RuntimeError("503 service unavailable"))
    assert outage.category == VideoFailureCategory.PROVIDER_UNAVAILABLE
    assert outage.retryable and outage.should_fallback
    assert not outage.should_split

    timeout = classify_video_failure(TimeoutError("clip timed out"))
    assert timeout.category == VideoFailureCategory.TIMEOUT
    assert timeout.should_split and timeout.should_fallback

    invalid = SimpleNamespace(code=400)
    invalid_error = RuntimeError("bad request")
    invalid_error.code = invalid.code
    classified = classify_video_failure(invalid_error)
    assert classified.category == VideoFailureCategory.BAD_REQUEST
    assert not classified.retryable


def test_circuit_breaker_opens_and_half_opens():
    breaker = VideoCircuitBreaker(threshold=2, cooldown_seconds=10.0)
    failure = classify_video_failure(TimeoutError("timeout"))
    breaker.record_failure("model", failure, now=10.0)
    assert breaker.allow("model", now=10.1)
    breaker.record_failure("model", failure, now=11.0)
    assert not breaker.allow("model", now=15.0)
    assert breaker.allow("model", now=21.1)
    assert not breaker.allow("model", now=21.2)
    breaker.record_success("model")
    assert breaker.snapshot() == {}


@pytest.mark.asyncio
async def test_timeout_chunk_is_bisected_and_successful_leaves_survive():
    with patch("artemis.agents.video_analyzer.video_analyzer.settings.GOOGLE_API_KEY", None):
        analyzer = VideoAnalyzer(_context())

    async def analyze(start: float, end: float, query: str) -> str:
        if end - start > 4.0:
            raise TimeoutError("large clip timed out")
        return f"[from {start:.1f}s to {end:.1f}s] Summary: recovered {query}"

    with patch.object(analyzer, "_exec_single_chunk", side_effect=analyze) as child:
        result = await analyzer.exec_spawn_sub_agent(0.0, 16.0, "find transition")

    assert child.await_count == 7
    assert result.count("recovered find transition") == 4
    assert "PARTIAL" not in result


@pytest.mark.asyncio
async def test_universal_primary_failure_uses_configured_fallback():
    with patch("artemis.agents.video_analyzer.video_analyzer.settings.GOOGLE_API_KEY", None):
        analyzer = VideoAnalyzer(_context())

    primary = MagicMock()
    primary_bound = MagicMock()
    primary_bound.ainvoke = AsyncMock(side_effect=TimeoutError("primary timeout"))
    primary.bind_tools.return_value = primary_bound
    fallback = MagicMock()
    fallback_bound = MagicMock()
    fallback_bound.ainvoke = AsyncMock(return_value=AIMessage(content="fallback answer"))
    fallback.bind_tools.return_value = fallback_bound

    with patch(
        "artemis.agents.video_analyzer.video_analyzer.get_llm",
        side_effect=[primary, fallback],
    ) as get_model:
        response = await analyzer._invoke_universal_model(
            [HumanMessage(content="inspect")],
            [],
            timeout=1.0,
            label="test",
        )

    assert response.content == "fallback answer"
    assert get_model.call_args_list[0].kwargs["is_utils"] is True
    assert get_model.call_args_list[1].kwargs["use_fallback"] is True


@pytest.mark.asyncio
async def test_native_chunk_exhaustion_commits_universal_fallback(tmp_path):
    with patch("artemis.agents.video_analyzer.video_analyzer.settings.GOOGLE_API_KEY", None):
        analyzer = VideoAnalyzer(_context())
    analyzer.use_native_gemini = True
    raw_video = tmp_path / "segment.mp4"
    raw_video.write_bytes(b"video")
    extracted = SimpleNamespace(
        success=True,
        video_path=raw_video,
        actual_start_relative_time=0.0,
        duration_seconds=5.0,
        warning=None,
    )
    controller = SimpleNamespace(extract_segment_metadata=AsyncMock(return_value=extracted))
    fallback_text = "[from 0.0s to 5.0s] Summary: fallback recovered Analysis: grounded"

    with (
        patch(
            "artemis.agents.video_analyzer.video_analyzer.get_controller",
            return_value=controller,
        ),
        patch(
            "artemis.agents.video_analyzer.video_analyzer.compress_video_for_api",
            new=AsyncMock(return_value=raw_video),
        ),
        patch.object(
            analyzer,
            "upload_and_poll_file",
            new=AsyncMock(side_effect=RuntimeError("503 provider unavailable")),
        ) as upload,
        patch.object(
            analyzer,
            "_exec_single_chunk_universal",
            new=AsyncMock(return_value=fallback_text),
        ) as fallback,
        patch(
            "artemis.agents.video_analyzer.video_analyzer.asyncio.sleep",
            new=AsyncMock(),
        ),
    ):
        result = await analyzer._exec_single_chunk(0.0, 5.0, "verify transition")

    assert result == fallback_text
    assert upload.await_count == 2
    fallback.assert_awaited_once()
    assert analyzer.blackboard.missing_intervals(0.0, 5.0, "verify transition") == []


def test_action_timestamps_drive_dense_sampling():
    with patch("artemis.agents.video_analyzer.video_analyzer.settings.GOOGLE_API_KEY", None):
        analyzer = VideoAnalyzer(_context())
    step = SimpleNamespace(timestamp=104.0, action_taken={"name": "tap"})
    storage = SimpleNamespace(get_steps=lambda _session: [step])
    analyzer.ctx.data_engine = SimpleNamespace(
        current_session_id="session-1",
        session_start_time=100.0,
        storage=storage,
    )

    offsets = analyzer._dense_action_offsets(0.0, 10.0, actual_start=0.0)

    assert offsets[0] == 2.0
    assert 4.0 in offsets
    assert offsets[-1] == 6.0


class _FakeGenaiError(Exception):
    """Mimics google.genai.errors.APIError's shape (a ``code`` attribute)."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


@pytest.mark.asyncio
async def test_invoke_with_retry_raises_non_retryable_immediately():
    from artemis.agents.video_analyzer.video_analyzer import _invoke_with_retry

    calls = 0

    async def op():
        nonlocal calls
        calls += 1
        raise _FakeGenaiError("invalid api key", code=401)

    with pytest.raises(_FakeGenaiError):
        await _invoke_with_retry(op, "test-op")
    assert calls == 1


@pytest.mark.asyncio
async def test_invoke_with_retry_bad_request_never_retried():
    from artemis.agents.video_analyzer.video_analyzer import _invoke_with_retry

    calls = 0

    async def op():
        nonlocal calls
        calls += 1
        raise _FakeGenaiError("invalid request payload", code=400)

    with pytest.raises(_FakeGenaiError):
        await _invoke_with_retry(op, "test-op")
    assert calls == 1


@pytest.mark.asyncio
async def test_invoke_with_retry_restarts_from_scratch_on_transient_failure():
    from artemis.agents.video_analyzer import video_analyzer as va

    calls = 0

    async def op():
        nonlocal calls
        calls += 1
        accumulated = "partial-"  # per-attempt accumulator, like a stream loop
        if calls < 2:
            raise _FakeGenaiError("503 service unavailable", code=503)
        return accumulated + "complete"

    with patch.object(va.asyncio, "sleep", new=AsyncMock()) as sleep_mock:
        result = await va._invoke_with_retry(op, "test-op")

    # The successful attempt produced the whole output; the failed attempt's
    # partial accumulation never leaked into the result.
    assert result == "partial-complete"
    assert calls == 2
    assert sleep_mock.await_count == 1


@pytest.mark.asyncio
async def test_invoke_with_retry_exhaustion_reraises_original_error():
    from artemis.agents.video_analyzer import video_analyzer as va

    calls = 0

    async def op():
        nonlocal calls
        calls += 1
        raise _FakeGenaiError("503 service unavailable", code=503)

    with patch.object(va.asyncio, "sleep", new=AsyncMock()):
        with pytest.raises(_FakeGenaiError):
            await va._invoke_with_retry(op, "test-op", max_attempts=2)
    assert calls == 2


@pytest.mark.asyncio
async def test_invoke_with_retry_caps_unknown_failures_by_policy():
    from artemis.llm.reliability import FailureCategory, retry_policy_for

    from artemis.agents.video_analyzer import video_analyzer as va

    calls = 0

    async def op():
        nonlocal calls
        calls += 1
        raise RuntimeError("something odd")

    with patch.object(va.asyncio, "sleep", new=AsyncMock()):
        with pytest.raises(RuntimeError):
            await va._invoke_with_retry(op, "test-op", max_attempts=5)
    # UNKNOWN failures follow the shared policy, not the caller's outer cap.
    assert calls == retry_policy_for(FailureCategory.UNKNOWN).max_attempts


# ------------------------------------------------ mid-run agentic degrade


class _AgenticRejected(Exception):
    status_code = 400


@pytest.mark.asyncio
async def test_pending_chunk_sized_for_agentic_ceiling_is_not_run_statically(tmp_path):
    """A chunk planned at 600s whose run degraded before it started is handed back."""
    with patch("artemis.agents.video_analyzer.video_analyzer.settings.GOOGLE_API_KEY", None):
        analyzer = VideoAnalyzer(_context())
    analyzer.use_native_gemini = True
    # A sibling chunk already flipped the run to static (60s ceiling).
    analyzer.video_processing = "static"
    analyzer.chunk_size_seconds = analyzer.static_chunk_size_seconds
    controller = SimpleNamespace(extract_segment_metadata=AsyncMock())

    with (
        patch(
            "artemis.agents.video_analyzer.video_analyzer.get_controller",
            return_value=controller,
        ),
        patch(
            "artemis.agents.video_analyzer.chunk_native._run_native_chunk_conversation",
            new=AsyncMock(),
        ) as static_path,
        patch.object(analyzer, "_exec_single_chunk_universal", new=AsyncMock()) as universal,
        pytest.raises(AgenticVideoDegraded) as raised,
    ):
        await analyzer._exec_single_chunk(0.0, 150.0, "verify toggle")

    # Refused before any extraction/upload; no static or fallback analysis.
    controller.extract_segment_metadata.assert_not_awaited()
    static_path.assert_not_awaited()
    universal.assert_not_awaited()
    assert classify_video_failure(raised.value).should_split
    # The lease is released so the re-planned pieces can be claimed.
    assert analyzer.blackboard.missing_intervals(0.0, 150.0, "verify toggle") == [(0.0, 150.0)]


@pytest.mark.asyncio
async def test_sibling_chunk_degraded_mid_run_is_replanned_into_static_pieces(tmp_path):
    """Two 600s chunks: the first is rejected while the second is still in media prep."""
    with patch("artemis.agents.video_analyzer.video_analyzer.settings.GOOGLE_API_KEY", None):
        analyzer = VideoAnalyzer(_context())
    analyzer.use_native_gemini = True
    analyzer.video_processing = "agentic"
    analyzer.chunk_size_seconds = analyzer.agentic_chunk_size_seconds
    assert analyzer.chunk_size_seconds == 600.0

    raw_video = tmp_path / "segment.mp4"
    raw_video.write_bytes(b"video")
    first_rejected = asyncio.Event()

    async def extract(start: float, end: float):
        if start >= 600.0 and analyzer.video_processing == "agentic":
            # The second 600s chunk is mid-prep when the first one degrades.
            await first_rejected.wait()
        return SimpleNamespace(
            success=True,
            video_path=raw_video,
            actual_start_relative_time=start,
            duration_seconds=end - start,
            warning=None,
        )

    controller = SimpleNamespace(extract_segment_metadata=AsyncMock(side_effect=extract))
    agentic_calls: list[tuple[float, float]] = []

    async def agentic(analyzer_, media, cs, ce, *rest):
        agentic_calls.append((cs, ce))
        first_rejected.set()
        raise _AgenticRejected("400 INVALID_ARGUMENT: processing 'agentic' is not supported")

    async def static_answer(analyzer_, media, cs, ce, *rest):
        return f"[from {cs:.1f}s to {ce:.1f}s] Summary: static Analysis: ok"

    with (
        patch(
            "artemis.agents.video_analyzer.video_analyzer.get_controller",
            return_value=controller,
        ),
        patch(
            "artemis.agents.video_analyzer.video_analyzer.compress_video_for_api",
            new=AsyncMock(return_value=raw_video),
        ),
        patch(
            "artemis.agents.video_analyzer.chunk_native.run_agentic_chunk_conversation",
            new=AsyncMock(side_effect=agentic),
        ),
        patch(
            "artemis.agents.video_analyzer.chunk_native._run_native_chunk_conversation",
            new=AsyncMock(side_effect=static_answer),
        ) as static_path,
        patch.object(analyzer, "_exec_single_chunk_universal", new=AsyncMock()) as universal,
        patch("artemis.agents.video_analyzer.video_analyzer._record_llm_event"),
    ):
        result = await analyzer.exec_spawn_sub_agent(0.0, 1200.0, "verify toggle")

    # Only the first chunk ever reached the agentic API; the second never ran
    # its 600s clip statically and neither went to the universal fallback.
    assert agentic_calls == [(0.0, 600.0)]
    universal.assert_not_awaited()
    segments = sorted((call.args[2], call.args[3]) for call in static_path.await_args_list)
    assert len(segments) == 20
    assert segments[0] == (0.0, 60.0)
    assert segments[-1] == (1140.0, 1200.0)
    assert all(ce - cs <= 60.0 for cs, ce in segments)
    assert "PARTIAL" not in result
    assert result.count("Summary: static") == 20
    assert analyzer.video_processing == "static"


@pytest.mark.asyncio
async def test_short_degraded_chunk_is_retried_statically_once():
    """A 5s chunk (below min_chunk*2) is re-run as-is instead of failing."""
    with patch("artemis.agents.video_analyzer.video_analyzer.settings.GOOGLE_API_KEY", None):
        analyzer = VideoAnalyzer(_context())
    calls: list[tuple[float, float]] = []

    async def analyze(start: float, end: float, query: str) -> str:
        calls.append((start, end))
        if len(calls) == 1:
            analyzer.video_processing = "static"
            analyzer.chunk_size_seconds = analyzer.static_chunk_size_seconds
            raise AgenticVideoDegraded("agentic rejected")
        return f"[from {start:.1f}s to {end:.1f}s] Summary: static {query}"

    with patch.object(analyzer, "_exec_single_chunk", side_effect=analyze) as child:
        result = await analyzer.exec_spawn_sub_agent(0.0, 5.0, "find toggle")

    assert child.await_count == 2
    assert calls == [(0.0, 5.0), (0.0, 5.0)]
    assert "PARTIAL" not in result
    assert "failed" not in result.lower()
    assert result.count("Summary: static find toggle") == 1


@pytest.mark.asyncio
async def test_static_retry_of_degraded_chunk_is_bounded():
    """A second degrade on the same interval is terminal: no loop, no bisect."""
    with patch("artemis.agents.video_analyzer.video_analyzer.settings.GOOGLE_API_KEY", None):
        analyzer = VideoAnalyzer(_context())
    calls: list[tuple[float, float]] = []

    async def analyze(start: float, end: float, query: str) -> str:
        calls.append((start, end))
        raise AgenticVideoDegraded("agentic rejected again")

    with patch.object(analyzer, "_exec_single_chunk", side_effect=analyze) as child:
        # 30s is above min_chunk*2, so a bisect would otherwise be legal.
        result = await analyzer.exec_spawn_sub_agent(0.0, 30.0, "find toggle")

    assert child.await_count == 2
    assert calls == [(0.0, 30.0), (0.0, 30.0)]
    assert result.startswith("All sub-agent chunks failed")
    assert "0.0s-30.0s [bad_request]" in result
