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

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from artemis.agents.video_analyzer.blackboard import (
    VideoBlackboard,
    get_video_blackboard,
)
from artemis.agents.video_analyzer.video_analyzer import VideoAnalyzer, _invoke_with_retry
from artemis.context import ArtemisContext


def test_blackboard_persists_segments_observations_and_evidence(tmp_path):
    db_path = tmp_path / "trace.db"
    evidence_dir = tmp_path / "video_blackboard" / "evidence"
    source_image = tmp_path / "temporary.jpg"
    source_image.write_bytes(b"jpeg evidence")

    board = VideoBlackboard("video:recording-1", db_path=db_path, evidence_dir=evidence_dir)
    stored = board.add_observation(
        {
            "start": 1.0,
            "end": 1.5,
            "target": "verify login",
            "summary": "Login screen became visible.",
            "confidence_score": 0.95,
            "screenshot": str(source_image),
        }
    )
    # A duplicate commit is idempotent.
    board.add_observation(stored)

    claim = board.claim_segment(0.0, 5.0, "verify login")
    assert claim.state == "claimed"
    board.complete_segment(
        0.0,
        5.0,
        "verify login",
        claim.lease_owner,
        "Login succeeded.",
        "The authenticated home screen appeared.",
    )

    reopened = VideoBlackboard("video:recording-1", db_path=db_path, evidence_dir=evidence_dir)
    observations = reopened.list_observations()
    assert len(observations) == 1
    persisted_evidence = observations[0]["screenshot"]
    assert persisted_evidence != str(source_image)
    assert (evidence_dir / f"{stored['observation_id']}.jpg").read_bytes() == b"jpeg evidence"

    assert reopened.missing_intervals(0.0, 5.0, "verify login") == []
    cached = reopened.claim_segment(0.0, 5.0, "verify login")
    assert cached.state == "cached"
    assert cached.summary == "Login succeeded."
    assert any(entry.get("kind") == "segment_result" for entry in reopened.list_ledger_entries())


def test_blackboard_calculates_only_uncovered_intervals(tmp_path):
    board = VideoBlackboard("video:coverage", db_path=tmp_path / "coverage.db")
    for start, end in ((0.0, 10.0), (8.0, 20.0), (30.0, 40.0)):
        claim = board.claim_segment(start, end, "find spinner")
        board.complete_segment(
            start, end, "find spinner", claim.lease_owner, f"Analyzed {start}-{end}"
        )

    assert board.missing_intervals(0.0, 50.0, "find spinner") == [
        (20.0, 30.0),
        (40.0, 50.0),
    ]
    # Different questions can use the observations as context but do not share
    # query-specific completion coverage.
    assert board.missing_intervals(0.0, 10.0, "count spinner frames") == [(0.0, 10.0)]


def test_blackboard_lease_suppresses_duplicates_and_is_reclaimable():
    board = VideoBlackboard("video:lease", lease_seconds=0.02)
    first = board.claim_segment(0.0, 5.0, "verify tap")
    assert first.state == "claimed"
    assert board.claim_segment(0.0, 5.0, "verify tap").state == "in_progress"

    time.sleep(0.03)
    reclaimed = board.claim_segment(0.0, 5.0, "verify tap")
    assert reclaimed.state == "claimed"
    assert reclaimed.lease_owner != first.lease_owner

    board.fail_segment(0.0, 5.0, "verify tap", reclaimed.lease_owner, "temporary provider outage")
    assert board.claim_segment(0.0, 5.0, "verify tap").state == "claimed"


def test_get_video_blackboard_is_shared_by_context():
    ctx = MagicMock(spec=ArtemisContext)
    ctx._video_blackboard = None
    ctx.data_engine = None
    ctx.execution_setup = None
    ctx.device = MagicMock()
    ctx.device.device_id = "device-1"

    first = get_video_blackboard(ctx)
    first.add_observation(
        {
            "start": 0.0,
            "end": 1.0,
            "target": "shared",
            "summary": "Persisted in context.",
        }
    )
    second = get_video_blackboard(ctx)

    assert first is second
    assert second.list_observations()[0]["summary"] == "Persisted in context."


@pytest.fixture
def analyzer_context():
    ctx = MagicMock(spec=ArtemisContext)
    ctx._video_blackboard = None
    ctx._mobile_controller = None
    ctx.data_engine = None
    ctx.execution_setup = None
    ctx.agent_config = None
    ctx.device = MagicMock()
    ctx.device.device_id = "device-analyzer"
    ctx.llm_config = MagicMock()
    ctx.llm_config.utils.video_analyzer.model = "universal-test-model"
    ctx.llm_config.utils.video_analyzer.temperature = 0.2
    return ctx


@pytest.mark.asyncio
async def test_spawn_sub_agent_reuses_complete_persistent_coverage(analyzer_context):
    with patch("artemis.agents.video_analyzer.video_analyzer.settings.GOOGLE_API_KEY", None):
        analyzer = VideoAnalyzer(analyzer_context)
    claim = analyzer.blackboard.claim_segment(10.0, 20.0, "verify login")
    analyzer.blackboard.complete_segment(
        10.0,
        20.0,
        "verify login",
        claim.lease_owner,
        "Login succeeded.",
        "Home screen appeared.",
    )

    with patch.object(analyzer, "_exec_single_chunk", new_callable=AsyncMock) as child:
        result = await analyzer.exec_spawn_sub_agent(10.0, 20.0, "verify login")

    child.assert_not_awaited()
    assert result.startswith("CACHED VIDEO ANALYSIS:")
    assert "Login succeeded" in result


@pytest.mark.asyncio
async def test_spawn_sub_agent_returns_partial_and_keeps_prior_success(analyzer_context):
    with patch("artemis.agents.video_analyzer.video_analyzer.settings.GOOGLE_API_KEY", None):
        analyzer = VideoAnalyzer(analyzer_context)
    claim = analyzer.blackboard.claim_segment(0.0, 60.0, "summarize flow")
    analyzer.blackboard.complete_segment(
        0.0,
        60.0,
        "summarize flow",
        claim.lease_owner,
        "First minute completed.",
    )

    with patch.object(
        analyzer,
        "_exec_single_chunk",
        new_callable=AsyncMock,
        side_effect=RuntimeError("provider unavailable"),
    ) as child:
        result = await analyzer.exec_spawn_sub_agent(0.0, 120.0, "summarize flow")

    child.assert_awaited_once_with(60.0, 120.0, "summarize flow")
    assert result.startswith("PARTIAL VIDEO ANALYSIS")
    assert "First minute completed" in result
    assert analyzer.blackboard.missing_intervals(0.0, 120.0, "summarize flow") == [(60.0, 120.0)]


@pytest.mark.asyncio
async def test_video_coordinator_retries_transient_failures():
    operation = AsyncMock(
        side_effect=[TimeoutError("timeout one"), ConnectionError("reset"), "completed"]
    )
    with patch(
        "artemis.agents.video_analyzer.video_analyzer.asyncio.sleep", new_callable=AsyncMock
    ) as sleep:
        result = await _invoke_with_retry(operation, "test coordinator")

    assert result == "completed"
    assert operation.await_count == 3
    assert sleep.await_count == 2


@pytest.mark.asyncio
async def test_audio_analysis_reuses_exact_completed_coverage(analyzer_context):
    with patch("artemis.agents.video_analyzer.video_analyzer.settings.GOOGLE_API_KEY", None):
        analyzer = VideoAnalyzer(analyzer_context)
    claim = analyzer.blackboard.claim_segment(2.0, 6.0, "hear notification", modality="audio")
    analyzer.blackboard.complete_segment(
        2.0,
        6.0,
        "hear notification",
        claim.lease_owner,
        "A notification sounded.",
        "One short bell was audible.",
        modality="audio",
    )

    with patch("artemis.agents.video_analyzer.video_analyzer.get_controller") as controller:
        result = await analyzer.exec_analyze_audio_only(2.0, 6.0, "hear notification")

    controller.assert_not_called()
    assert result.startswith("CACHED AUDIO ANALYSIS:")
    assert "notification sounded" in result


def test_blackboard_metrics_include_structured_failures(tmp_path):
    board = VideoBlackboard("video:metrics", db_path=tmp_path / "metrics.db")
    claim = board.claim_segment(
        0.0,
        5.0,
        "inspect",
        model_name="vlm-primary",
        source_generation=2,
    )
    board.fail_segment(
        0.0,
        5.0,
        "inspect",
        claim.lease_owner,
        "timeout",
        error_category="timeout",
    )

    assert board.metrics()["segments"] == {"retryable_failed": 1}
    assert board.metrics()["failure_categories"] == {"timeout": 1}


def test_session_deletion_removes_video_blackboard_rows(tmp_path):
    from uuid import uuid4

    from artemis.data_engine.engine import DataEngine

    ctx = MagicMock(spec=ArtemisContext)
    ctx.execution_setup = MagicMock(traces_path=str(tmp_path / "traces"))
    ctx.device = None
    engine = DataEngine(ctx)
    session_id = engine.start_session(goal="blackboard cleanup")
    video_id = uuid4()
    recording = tmp_path / "recording.mp4"
    recording.write_bytes(b"video")
    engine.record_video_start(video_id, "device", recording)

    board = VideoBlackboard(f"video:{video_id}", db_path=engine.storage.db_path)
    board.add_observation({"start": 0.0, "end": 1.0, "target": "cleanup", "summary": "evidence"})
    assert board.list_observations()

    engine.storage.delete_session(session_id)

    reopened = VideoBlackboard(f"video:{video_id}", db_path=engine.storage.db_path)
    assert reopened.list_observations() == []
