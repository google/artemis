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

"""Tests for the versioned, status-carrying step summary write (M0 §6.1)."""

from unittest.mock import MagicMock

from artemis.context import ArtemisContext
from artemis.data_engine.engine import DataEngine


def _make_engine(tmp_path):
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_execution_setup = MagicMock()
    mock_execution_setup.traces_path = str(tmp_path)
    mock_ctx.execution_setup = mock_execution_setup
    mock_ctx.device = None
    engine = DataEngine(mock_ctx)
    engine.start_session("summary versioning test")
    return engine


def _flush(engine):
    for t in list(engine._pending_threads):
        t.join()


def _get_step(engine, step_id):
    steps = engine.storage.get_steps(engine.current_session_id)
    for step in steps:
        if str(step.step_id) == str(step_id):
            return step
    return None


def test_ready_write_persists_summary_and_metadata(tmp_path):
    engine = _make_engine(tmp_path)
    step_id = engine.record_step(summary="initial")
    _flush(engine)

    applied = engine.storage.update_step_summary(
        step_id,
        "Tapped the login button.",
        source="visual_transition",
        model="gemini-2.5-flash-lite",
        status="ready",
    )
    assert applied is True

    step = _get_step(engine, step_id)
    assert step.summary == "Tapped the login button."
    assert step.extra_metadata["summary_status"] == "ready"
    assert step.extra_metadata["summary_source"] == "visual_transition"
    assert step.extra_metadata["summary_version"] == 1
    assert step.extra_metadata["summary_model"] == "gemini-2.5-flash-lite"


def test_auto_version_increments_per_write(tmp_path):
    engine = _make_engine(tmp_path)
    step_id = engine.record_step(summary="initial")
    _flush(engine)

    engine.storage.update_step_summary(step_id, "v1", status="ready")
    engine.storage.update_step_summary(step_id, "v2", status="ready")

    step = _get_step(engine, step_id)
    assert step.summary == "v2"
    assert step.extra_metadata["summary_version"] == 2


def test_stale_explicit_version_is_dropped(tmp_path):
    engine = _make_engine(tmp_path)
    step_id = engine.record_step(summary="initial")
    _flush(engine)

    assert engine.storage.update_step_summary(step_id, "newer", version=5, status="ready")
    applied = engine.storage.update_step_summary(step_id, "older", version=3, status="ready")
    assert applied is False

    step = _get_step(engine, step_id)
    assert step.summary == "newer"
    assert step.extra_metadata["summary_version"] == 5


def test_pending_does_not_downgrade_ready(tmp_path):
    engine = _make_engine(tmp_path)
    step_id = engine.record_step(summary="initial")
    _flush(engine)

    engine.storage.update_step_summary(step_id, "done", status="ready")
    applied = engine.storage.update_step_summary(step_id, None, status="pending")
    assert applied is False

    step = _get_step(engine, step_id)
    assert step.summary == "done"
    assert step.extra_metadata["summary_status"] == "ready"


def test_failed_marks_status_without_touching_summary(tmp_path):
    engine = _make_engine(tmp_path)
    step_id = engine.record_step(summary="original text")
    _flush(engine)

    engine.storage.update_step_summary(step_id, None, status="pending", source="visual_transition")
    applied = engine.storage.update_step_summary(
        step_id, None, status="failed", source="visual_transition"
    )
    assert applied is True

    step = _get_step(engine, step_id)
    assert step.summary == "original text"
    assert step.extra_metadata["summary_status"] == "failed"

    # A failed write must never clobber an already-ready summary.
    engine.storage.update_step_summary(step_id, "recovered", status="ready")
    applied = engine.storage.update_step_summary(step_id, None, status="failed")
    assert applied is False
    step = _get_step(engine, step_id)
    assert step.extra_metadata["summary_status"] == "ready"


def test_missing_step_returns_false(tmp_path):
    from uuid import uuid4

    engine = _make_engine(tmp_path)
    assert engine.storage.update_step_summary(uuid4(), "text", status="ready") is False


def test_engine_level_write_carries_metadata_and_publishes_ready_only(tmp_path):
    engine = _make_engine(tmp_path)
    published = []
    engine.subscribe(lambda event_type, data: published.append((event_type, data)))

    step_id = engine.record_step(summary="initial")
    _flush(engine)
    published.clear()

    # Non-ready statuses persist metadata but publish no SSE.
    engine.update_step_summary(step_id, None, status="pending", source="visual_transition")
    _flush(engine)
    assert not [e for e in published if e[0] == "step_updated"]
    step = _get_step(engine, step_id)
    assert step.extra_metadata["summary_status"] == "pending"

    engine.update_step_summary(
        step_id,
        "Ready text.",
        status="ready",
        source="visual_transition",
        model="test-model",
    )
    _flush(engine)
    ready_events = [e for e in published if e[0] == "step_updated"]
    assert len(ready_events) == 1
    assert ready_events[0][1]["summary"] == "Ready text."

    step = _get_step(engine, step_id)
    assert step.summary == "Ready text."
    assert step.extra_metadata["summary_status"] == "ready"
    assert step.extra_metadata["summary_model"] == "test-model"
