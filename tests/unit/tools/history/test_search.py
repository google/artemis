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

"""search_history boundaries and search correctness.

Hard boundaries under test: bounded result count, bounded response tokens,
every result carrying a step number. Plus: keyword/step-range/notes/chunk
filter correctness, the step-range ledger re-entry (full-width
``build_action_ledger`` rows), and the screen-text surface: the step's own
pre AND post screenshots plus screenshots its tool results embedded.
"""

import hashlib
import io
import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image

from artemis.context import ArtemisContext
from artemis.data_engine.engine import DataEngine
from artemis.tools.history import (
    SearchHistoryTool,
    search_history,
    search_history_available,
    search_history_text,
)
from artemis.tools.history.search import result_search_text
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
    engine.start_session("search test session")
    return engine


def _flush(engine):
    for t in list(engine._pending_threads):
        t.join()


def _cfg(**overrides):
    values = {"enabled": True, "max_results": 5, "max_text_tokens": 2000, "screen_scan_steps": 150}
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
    out = search_history_text(engine, query="verification code", recall_config=_cfg())
    assert isinstance(out, str)
    assert "Step 2" in out
    assert "4711" in out
    # Every result header names a step / step range / step anchor.
    for header in _result_blocks(out):
        assert re.search(r"Step[s]? \d", header), header


def test_step_range_filters_matches(engine):
    out = search_history_text(engine, query="popup", step_range=[1, 2], recall_config=_cfg())
    # Step 3 (the popup step) is outside the range: only the range ledger and
    # a no-match line may appear.
    assert "A promo popup appeared" not in out
    assert "No matches" in out


def test_step_range_returns_full_width_ledger_rows(engine):
    """The compressed-history marker line's re-entry point: a step range
    returns that range's full per-step action ledger."""
    out = search_history_text(engine, query="", step_range=[3, 1], recall_config=_cfg())
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
    out = search_history_text(
        engine, query="anchorterm", max_results=50, recall_config=_cfg(max_results=3)
    )
    assert out.count("[Step ") == 3
    assert "more not shown" in out


def test_response_is_truncated_at_token_budget(engine):
    for i in range(6):
        engine.record_step(
            summary=f"needleword page {i} " + "filler content " * 60,
            action_taken={"action": "click"},
            last_execution_result={"status": "success"},
        )
    _flush(engine)
    out = search_history_text(
        engine, query="needleword", max_results=6, recall_config=_cfg(max_text_tokens=150)
    )
    assert len(out) <= 150 * 4 + 200  # budget + truncation notice
    assert "truncated at the search token budget" in out


def test_notes_are_searchable_and_anchored_to_a_step(engine):
    save_note_content(engine.base_dir, "login_flow", "The OTP entry lives behind the SMS tab.")
    out = search_history_text(engine, query="OTP entry", recall_config=_cfg())
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
    out = search_history_text(engine, query="chunkneedle", recall_config=_cfg())
    assert "[History chunk | Steps 1–2" in out


def test_foreground_app_stamp_joins_search_surface(tmp_path):
    """A package-name query hits the record_step foreground_app stamp."""
    engine = _make_engine(tmp_path)
    engine.record_step(
        summary="Opened the alarms list.",
        action_taken={"action": "click", "target_text": "Alarm"},
        ui_tree=[{"package": "com.google.android.deskclock", "bounds": "[0,0][9,9]"}],
    )
    _flush(engine)

    out = search_history_text(engine, query="deskclock", recall_config=_cfg())
    assert "Step 1" in out
    assert "No matches" not in out


def test_tool_calls_of_a_step_are_searchable(engine):
    engine.record_step(
        summary="Asked the explorer.",
        action_taken={"action": "click", "target_text": "Save"},
        last_execution_result={"status": "success"},
    )
    engine.record_trace(
        type="tool",
        name="ask_explorer",
        payload={"args": {"question": "toolneedle?"}, "result": "The resultneedle is ON."},
        step_id=engine.last_recorded_step_id,
    )
    _flush(engine)
    assert "[Step 4 " in search_history_text(engine, query="toolneedle", recall_config=_cfg())
    assert "[Step 4 " in search_history_text(engine, query="resultneedle", recall_config=_cfg())


# --- Screen text: pre + post screenshots and referenced screenshots ----------------------


def test_post_screenshot_text_joins_the_search_surface(engine):
    """OCR text that only exists on the step's post-action screenshot is found."""
    record = engine.get_step_record(2)
    engine.storage.update_image_data(
        record.post_image_name,
        ocr_result=[{"text": "Welcome back postneedle", "bounds": [0, 0, 10, 10]}],
        ui_tree=None,
    )
    out = search_history_text(engine, query="postneedle", recall_config=_cfg())
    assert "[Step 2 " in out


def test_screen_scan_cap_limits_the_screenshot_sweep(engine):
    record = engine.get_step_record(1)
    engine.storage.update_image_data(
        record.pre_image_name,
        ocr_result=[{"text": "capneedle", "bounds": [0, 0, 10, 10]}],
        ui_tree=None,
    )
    # Only the most recent step is scanned: Step 1's OCR is out of reach ...
    out = search_history_text(engine, query="capneedle", recall_config=_cfg(screen_scan_steps=1))
    assert "No matches" in out
    # ... unless the range narrows the sweep onto it.
    out = search_history_text(
        engine, query="capneedle", step_range=[1, 1], recall_config=_cfg(screen_scan_steps=1)
    )
    assert "[Step 1 " in out


def test_embedded_screenshot_description_joins_the_search_surface(tmp_path):
    """A step whose tool result embedded an earlier screenshot is found by
    that screenshot's OCR text: the image is described, not stripped."""
    engine = _make_engine(tmp_path)
    pre_bytes = _jpeg("red")
    engine.record_step(
        pre_screenshot_bytes=pre_bytes,
        summary="Opened the alarms list.",
        action_taken={"action": "click", "target_text": "Alarm"},
        ocr_result=[{"text": "Alarm 07:30 ocrneedle", "bounds": [0, 0, 10, 10]}],
    )
    engine.record_step(
        summary="Looked back at the alarm screen.",
        action_taken={"action": "click", "target_text": "Save"},
    )
    engine.record_trace(
        type="tool",
        name="get_step_screenshot",
        payload={
            "args": {"step_number": 1, "which": "pre"},
            "result": [
                {"type": "text", "text": "Screenshot of step 1 (pre-action) is attached."},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"<ImageRef: sha256={hashlib.sha256(pre_bytes).hexdigest()} length=1>"
                    },
                },
            ],
        },
        step_id=engine.last_recorded_step_id,
    )
    _flush(engine)

    out = search_history_text(engine, query="ocrneedle", recall_config=_cfg())
    assert "[Step 1 " in out
    assert "[Step 2 " in out
    assert "base64" not in out


def test_result_search_text_strips_image_blocks():
    blocks = [
        {"type": "text", "text": "hello needle"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJDRA=="}},
    ]
    text = result_search_text(blocks)
    assert "hello needle" in text
    assert "base64" not in text and "QUJDRA" not in text
    assert result_search_text({"answer": "yes"}) == '{"answer": "yes"}'
    assert result_search_text(None) == ""


# --- Tool surface ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_requires_query_or_range(engine):
    tool = SearchHistoryTool()
    out = await tool.execute(ctx=SimpleNamespace(data_engine=engine), query="", step_range=None)
    assert "needs a query and/or a step_range" in out


@pytest.mark.asyncio
async def test_execute_searches_and_never_returns_images(engine):
    out = await search_history.execute(
        ctx=SimpleNamespace(data_engine=engine), query="verification code"
    )
    assert isinstance(out, str)
    assert "Step 2" in out


@pytest.mark.asyncio
async def test_execute_without_engine_degrades():
    out = await search_history.execute(ctx=SimpleNamespace(data_engine=None), query="x")
    assert "no active execution history" in out


def test_availability_requires_engine_and_config():
    assert search_history.is_available(None) is False
    assert search_history.is_available(SimpleNamespace(data_engine=None)) is False
    assert search_history.is_available(SimpleNamespace(data_engine=object())) is True
    assert search_history_available(SimpleNamespace(data_engine=object())) is True
