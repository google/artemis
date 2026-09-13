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

"""OfflineHistoryReader: the MCP inspector's read-only twin of the live engine.

The offline reader must produce exactly the records the live ``DataEngine``
produces (same ``friendly_step``), open the database read-only, and satisfy
the ``HistoryReader`` protocol the history tools are written against.
"""

import io
import sqlite3
from unittest.mock import MagicMock

import pytest
from PIL import Image

from artemis.context import ArtemisContext
from artemis.data_engine.engine import DataEngine
from artemis.data_engine.history_reader import HistoryReader, OfflineHistoryReader


def _jpeg(color: str) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 128), color).save(buf, format="JPEG")
    return buf.getvalue()


def _engine(tmp_path):
    ctx = MagicMock(spec=ArtemisContext)
    ctx.execution_setup = MagicMock()
    ctx.execution_setup.traces_path = str(tmp_path)
    ctx.device = None
    engine = DataEngine(ctx)
    engine.start_session("offline reader test")
    return engine


def _flush(engine):
    for t in list(engine._pending_threads):
        t.join()


@pytest.fixture
def recorded(tmp_path):
    engine = _engine(tmp_path)
    engine.record_step(
        pre_screenshot_bytes=_jpeg("red"),
        post_screenshot_bytes=_jpeg("blue"),
        summary="Tapped Save after asking the explorer.",
        action_taken={"action": "click", "target": [540, 1200], "target_text": "Save"},
        last_execution_result={"status": "success"},
        operator_raw_thinking="Confirm the toggle first.",
        extra_metadata={"width": 1080, "height": 2400},
    )
    engine.record_trace(
        type="tool",
        name="ask_explorer",
        payload={"args": {"question": "toggle?"}, "result": "The toggle is ON."},
        step_id=engine.last_recorded_step_id,
    )
    engine.record_step(
        summary="Went back.",
        action_taken={"action": "press_key", "key": "back"},
        last_execution_result={"status": "success"},
    )
    engine.record_history_chunk(
        start_step_number=1,
        end_step_number=2,
        version=1,
        status="ready",
        band2="  - Steps 1–2: saved the toggle",
        band3="- Step 1 (T+00:00): click -> executed",
        rendered_text="[Chunk 1 | Steps 1–2]",
    )
    _flush(engine)
    return engine


def _reader(engine) -> OfflineHistoryReader:
    return OfflineHistoryReader(engine.db_path, engine.global_base_dir, engine.current_session_id)


def test_offline_reader_matches_live_friendly_steps(recorded):
    reader = _reader(recorded)
    assert isinstance(reader, HistoryReader)
    assert reader.session_start_time == recorded.session_start_time
    assert reader.base_dir == recorded.base_dir

    live = recorded.get_agent_friendly_steps()
    offline = reader.get_agent_friendly_steps()
    assert offline == live
    assert offline[0]["tool_calls"][0]["name"] == "ask_explorer"
    # Coordinates normalized exactly like the live engine.
    assert offline[0]["action_taken"]["target"] == live[0]["action_taken"]["target"]
    assert offline[0]["relative_time"] == live[0]["relative_time"]

    assert reader.get_agent_friendly_steps_in_range(2, 1) == live
    assert reader.get_agent_friendly_step(2) == live[1]
    assert reader.get_agent_friendly_step(9) is None


def test_offline_reader_step_records_images_and_chunks(recorded):
    reader = _reader(recorded)
    record = reader.get_step_record(1)
    assert record is not None and record.step_number == 1
    assert reader.get_step_record(7) is None

    pre = reader.get_step_image_path(1, "pre")
    post = reader.get_step_image_path(1, "post")
    assert pre is not None and pre.exists()
    assert post is not None and post.exists()
    assert pre == recorded.get_image_path(record.pre_image_name)
    assert reader.get_step_image_path(2, "pre") is None

    chunks = reader.get_history_chunks()
    assert len(chunks) == 1
    assert chunks[0].start_step_number == 1


def test_offline_reader_is_read_only(recorded):
    reader = _reader(recorded)
    with reader.storage._get_connection() as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM steps")
    # No directories or tables are created for a missing database.
    with pytest.raises(FileNotFoundError):
        OfflineHistoryReader(recorded.global_base_dir / "nope.db", recorded.global_base_dir, "x")
