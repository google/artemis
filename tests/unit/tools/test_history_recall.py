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

"""recall_history boundaries and search correctness (M4, design §10).

Hard boundaries under test: bounded result count, bounded response tokens,
image recall limited to the real stored screenshots of a single step, and
every result carrying a step number. Plus: keyword/step-range/notes filter
correctness and the era-marker re-entry (a step_range returns the full-width
``build_action_ledger`` rows for that range).
"""

import io
import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image

from artemis.context import ArtemisContext
from artemis.data_engine.engine import DataEngine
from artemis.tools.history_recall import (
    RecallHistoryTool,
    recall_history,
    search_history,
)
from artemis.utils.notes import save_note_content


def _jpeg(color: str) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 128), color).save(buf, format="JPEG")
    return buf.getvalue()


def _make_engine(tmp_path):
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_execution_setup = MagicMock()
    mock_execution_setup.traces_path = str(tmp_path)
    mock_ctx.execution_setup = mock_execution_setup
    mock_ctx.device = None
    engine = DataEngine(mock_ctx)
    engine.start_session("recall test session")
    return engine


def _flush(engine):
    for t in list(engine._pending_threads):
        t.join()


def _cfg(**overrides):
    values = {"enabled": True, "max_results": 5, "max_text_tokens": 2000, "max_image_steps": 1}
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def engine(tmp_path):
    engine = _make_engine(tmp_path)
    engine.record_step(
        pre_screenshot_bytes=_jpeg("red"),
        summary="Opened the login page.",
        action_taken={"action": "click", "target_text": "Login entry"},
        last_execution_result={"status": "success"},
    )
    engine.record_step(
        pre_screenshot_bytes=_jpeg("green"),
        post_screenshot_bytes=_jpeg("blue"),
        summary="Typed the verification code 4711 into the input.",
        action_taken={"action": "input_text", "text": "4711"},
        last_execution_result={"status": "success"},
        operator_raw_thinking="The code arrived via SMS; entering 4711 now.",
    )
    engine.record_step(
        summary="A promo popup appeared and was dismissed.",
        action_taken={"action": "click", "target_text": "Close popup"},
        last_execution_result={"status": "failed", "error": "element vanished"},
    )
    _flush(engine)
    return engine


def _result_blocks(text: str) -> list[str]:
    return [b for b in text.split("\n") if b.startswith("[")]


def test_keyword_search_finds_step_and_carries_step_number(engine):
    out = search_history(engine, query="verification code", recall_config=_cfg())
    assert isinstance(out, str)
    assert "Step 2" in out
    assert "4711" in out
    # Every result header names a step / step range / step anchor.
    for header in _result_blocks(out):
        assert re.search(r"Step[s]? \d", header), header


def test_step_range_filters_matches(engine):
    out = search_history(engine, query="popup", step_range=[1, 2], recall_config=_cfg())
    # Step 3 (the popup step) is outside the range: only the range ledger and
    # a no-match line may appear.
    assert "A promo popup appeared" not in out
    assert "No matches" in out


def test_step_range_returns_full_width_ledger_rows(engine):
    """The recall-only era period paragraph's '(Step-level ledger via
    recall_history for steps a–b)' re-entry point."""
    out = search_history(engine, query="", step_range=[1, 3], recall_config=_cfg())
    assert "Action ledger for Steps 1–3" in out
    # Full-width build_action_ledger rows: step number + T+mm:ss offset +
    # semantic action + result phrase.
    assert re.search(r"- Step 1 \(T\+\d{2}:\d{2}\): .*Login entry", out)
    assert re.search(r"- Step 2 \(T\+\d{2}:\d{2}\): ", out)
    assert re.search(r"- Step 3 \(T\+\d{2}:\d{2}\): ", out)


def test_result_count_is_clamped_by_config(engine):
    for i in range(8):
        engine.record_step(
            summary=f"Scrolled the anchorterm feed page {i}.",
            action_taken={"action": "swipe"},
            last_execution_result={"status": "success"},
        )
    _flush(engine)
    out = search_history(
        engine, query="anchorterm", max_results=50, recall_config=_cfg(max_results=3)
    )
    assert out.count("[Step ") == 3
    assert "more not shown" in out


def test_response_is_truncated_at_token_budget(engine):
    engine.record_step(
        summary="needleword " + "filler content " * 400,
        action_taken={"action": "click"},
        last_execution_result={"status": "success"},
    )
    _flush(engine)
    out = search_history(
        engine,
        query="needleword",
        include_details=True,
        recall_config=_cfg(max_text_tokens=150),
    )
    assert len(out) <= 150 * 4 + 200  # budget + truncation notice
    assert "truncated at the recall token budget" in out


def test_notes_are_searchable_and_anchored_to_a_step(engine):
    save_note_content(engine.base_dir, "login_flow", "The OTP entry lives behind the SMS tab.")
    out = search_history(engine, query="OTP entry", recall_config=_cfg())
    assert "[Note 'login_flow'" in out
    assert re.search(r"as of Step \d|last written at Step \d", out)


def test_chunk_rows_are_searchable(engine):
    engine.record_history_chunk(
        start_step_number=1,
        end_step_number=2,
        version=1,
        status="ready",
        band2="  - Steps 1–2: walked the chunkneedle flow",
        band3="- Step 1 (T+00:01): click -> executed",
        rendered_text=(
            "[Chunk 1 | Steps 1–2]\n② Compressed step summary\n"
            "  - Steps 1–2: walked the chunkneedle flow\n"
            "③ Step action ledger\n- Step 1 (T+00:01): click -> executed"
        ),
    )
    _flush(engine)
    out = search_history(engine, query="chunkneedle", recall_config=_cfg())
    assert "[History chunk | Steps 1–2" in out


def test_images_only_for_a_single_step_and_only_real_files(engine):
    out = search_history(
        engine,
        query="verification code",
        include_images=True,
        recall_config=_cfg(),
    )
    assert isinstance(out, list)
    images = [b for b in out if b.get("type") == "image_url"]
    # Step 2 has a real pre and post screenshot on disk — but never more than
    # one step's worth of images.
    assert 1 <= len(images) <= 2
    labels = [
        b["text"]
        for b in out
        if b.get("type") == "text" and "screenshot of Step" in b.get("text", "")
    ]
    assert all("Step 2" in label for label in labels)
    for block in images:
        assert block["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_images_disabled_by_config_cap(engine):
    out = search_history(
        engine,
        query="verification code",
        include_images=True,
        recall_config=_cfg(max_image_steps=0),
    )
    assert isinstance(out, str)


def test_missing_image_files_yield_note_not_blocks(engine):
    # Step 3 recorded no screenshots at all.
    out = search_history(
        engine,
        query="promo popup",
        include_images=True,
        recall_config=_cfg(),
    )
    assert isinstance(out, list)
    assert not [b for b in out if b.get("type") == "image_url"]
    assert any(
        "no stored screenshots exist" in b.get("text", "") for b in out if b.get("type") == "text"
    )


@pytest.mark.asyncio
async def test_execute_requires_query_or_range(engine):
    tool = RecallHistoryTool()
    ctx = SimpleNamespace(data_engine=engine)
    out = await tool.execute(ctx=ctx, query="", step_range=None)
    assert "needs a query and/or a step_range" in out


@pytest.mark.asyncio
async def test_execute_without_engine_degrades():
    tool = RecallHistoryTool()
    out = await tool.execute(ctx=SimpleNamespace(data_engine=None), query="x")
    assert "no active DataEngine session" in out


def test_availability_requires_engine_and_config():
    assert recall_history.is_available(None) is False
    assert recall_history.is_available(SimpleNamespace(data_engine=None)) is False
    assert recall_history.is_available(SimpleNamespace(data_engine=object())) is True


def test_foreground_app_stamp_joins_search_surface(tmp_path):
    """M5: a package-name query hits the record_step foreground_app stamp."""
    engine = _make_engine(tmp_path)
    engine.record_step(
        summary="Opened the alarms list.",
        action_taken={"action": "click", "target_text": "Alarm"},
        ui_tree=[{"package": "com.google.android.deskclock", "bounds": "[0,0][9,9]"}],
    )
    _flush(engine)

    out = search_history(engine, query="deskclock", recall_config=_cfg())
    assert "Step 1" in out
    assert "No matches" not in out
