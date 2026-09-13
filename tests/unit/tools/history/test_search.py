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
from artemis.core.tool_failure import is_tool_failure
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
    # A usage error is a request the tool did not serve: reported structurally.
    assert is_tool_failure(out)


@pytest.mark.asyncio
async def test_execute_reports_history_load_error_structurally():
    reader = MagicMock()
    reader.get_agent_friendly_steps.side_effect = RuntimeError("db locked")
    out = await search_history.execute(ctx=SimpleNamespace(data_engine=reader), query="login")
    assert "search_history failed to load history: db locked" in out
    assert is_tool_failure(out)


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


def test_excerpts_never_show_raw_json(engine):
    """The raw action / result JSON only scores; the ``Match:`` excerpt is the
    ledger action phrase plus the result's status/error words."""
    engine.record_step(
        summary="Started playback.",
        action_taken={
            "action": "click",
            "coordinates": [500, 520],
            "coordinate_space": "normalized",
            "target_description": "Wi-Fi jsonneedle row",
            "args": {"target": [500, 520], "target_description": "Wi-Fi jsonneedle row"},
        },
        last_execution_result={"status": "failed", "error": "Error: tap rejected"},
    )
    _flush(engine)

    out = search_history_text(engine, query="jsonneedle", recall_config=_cfg())
    match = next(line for line in out.splitlines() if line.strip().startswith("Match:"))
    assert "Tapped 'Wi-Fi jsonneedle row' (self-described) at [500, 520]" in match
    assert "status failed" in match
    assert "Error: tap rejected" in match
    for token in ('{"action"', '"coordinates"', '"coordinate_space"', '"args"', "{", "}"):
        assert token not in match, match


def test_raw_only_fields_still_score(engine):
    """A hit that only exists in the raw record (a resource id / package name
    under ``args``) is still found; its excerpt is the readable action line."""
    engine.record_step(
        summary="Opened the drawer.",
        action_taken={
            "action": "click",
            "coordinates": [10, 20],
            "coordinate_space": "normalized",
            "args": {"target": [10, 20], "resource_id": "com.example:id/rawonlyneedle"},
        },
        last_execution_result={"status": "dispatched"},
    )
    _flush(engine)
    out = search_history_text(engine, query="rawonlyneedle", recall_config=_cfg())
    assert "[Step 4 " in out
    assert "Match: Tapped element at [10, 20] | status dispatched" in out
    assert '"resource_id"' not in out


def test_summary_hit_is_not_repeated_in_the_match_line(engine):
    out = search_history_text(engine, query="promo popup", recall_config=_cfg())
    block = out[out.index("[Step 3 ") :]
    assert "Screen: A promo popup appeared and was dismissed." in block
    # The summary heads the result once; the Match line (if any) shows the
    # other readable fields, never the summary again.
    assert block.count("A promo popup appeared") == 1


def test_screen_hits_render_readable_values_not_ui_tree_json(tmp_path):
    engine = _make_engine(tmp_path)
    engine.record_step(
        pre_screenshot_bytes=_jpeg("red"),
        summary="On the network settings screen.",
        action_taken={"action": "click", "target_text": "Network"},
        ui_tree=[
            {
                "text": "",
                "content-desc": "Navigate up",
                "resource-id": "com.android.settings:id/toolbar_up",
                "bounds": "[0,0][100,100]",
                "class": "android.widget.ImageButton",
                "clickable": "true",
                "package": "com.android.settings",
            },
            {
                "text": "Wi-Fi treeneedle",
                "content-desc": "",
                "resource-id": "android:id/title",
                "bounds": "[40,560][600,640]",
                "class": "android.widget.TextView",
                "clickable": "false",
                "children": [{"text": "Connected", "bounds": "[40,640][600,700]"}],
            },
        ],
        ocr_result=[{"text": "Bluetooth ocrword", "bounds": [0, 0, 10, 10]}],
    )
    _flush(engine)

    out = search_history_text(engine, query="treeneedle", recall_config=_cfg())
    match = next(line for line in out.splitlines() if line.strip().startswith("Match:"))
    assert "Wi-Fi treeneedle" in match
    assert "Navigate up | com.android.settings:id/toolbar_up | Wi-Fi treeneedle" in match
    assert "android:id/title | Connected | Bluetooth ocrword" in match
    for token in ('"bounds"', '"class"', '"clickable"', "{", "}"):
        assert token not in match, match
    # Bounds / class names still score (they stay in the raw haystack).
    assert "[Step 1 " in search_history_text(engine, query="ImageButton", recall_config=_cfg())


def test_action_matches_keep_target_provenance(engine):
    """The action haystack is the shared ledger rendering, so an excerpt shows a
    self-described coordinate target with its marker and an observed index
    target without one."""
    from artemis.utils.task_tree import SELF_DESCRIBED_MARKER

    engine.record_step(
        summary="Started playback.",
        action_taken={
            "action": "click",
            "coordinates": [500, 600],
            "target_description": "play button",
        },
        last_execution_result={"status": "dispatched"},
    )
    _flush(engine)

    described = search_history_text(engine, query="play button", recall_config=_cfg())
    assert "[Step 4 " in described
    assert f"'play button' {SELF_DESCRIBED_MARKER}" in described

    observed = search_history_text(engine, query="Login entry", recall_config=_cfg())
    assert "[Step 1 " in observed
    assert "'Login entry'" in observed
    assert SELF_DESCRIBED_MARKER not in observed
