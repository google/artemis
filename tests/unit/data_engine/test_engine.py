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

from unittest.mock import MagicMock
from artemis.context import ArtemisContext
from artemis.data_engine.engine import DataEngine


def test_get_or_create_image_updates_missing_data(tmp_path):
    # Setup mock context
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_execution_setup = MagicMock()
    mock_execution_setup.traces_path = str(tmp_path)
    mock_ctx.execution_setup = mock_execution_setup
    mock_ctx.device = None

    # Initialize DataEngine
    engine = DataEngine(mock_ctx)

    image_bytes = b"fake_image_bytes"

    # 1. Save image without OCR/UI tree
    image_name1 = engine.get_or_create_image(image_bytes)

    # Verify it was saved with None for OCR/UI tree
    record1 = engine.storage.get_image(image_name1)
    assert record1 is not None
    assert record1.ocr_result is None
    assert record1.ui_tree is None

    # 2. Save same image with OCR/UI tree
    fake_ocr = [{"text": "hello", "box": [0, 0, 10, 10]}]
    fake_ui = [{"resource-id": "button1", "text": "click me"}]

    image_name2 = engine.get_or_create_image(image_bytes, ui_tree=fake_ui, ocr_result=fake_ocr)

    assert image_name1 == image_name2

    # Verify it now has OCR/UI tree
    record2 = engine.storage.get_image(image_name2)
    assert record2 is not None
    assert record2.ocr_result == fake_ocr
    assert record2.ui_tree == fake_ui


def test_create_image_duplicate_handling(tmp_path):
    from artemis.data_engine.storage import StorageManager
    from artemis.data_engine.models import ImageRecord

    storage = StorageManager(tmp_path / "data_engine.db", tmp_path)
    image_record = ImageRecord(
        image_name="test_image_hash",
        timestamp=123.456,
        ocr_result=None,
        ui_tree=None,
        extra_metadata={},
    )

    # First insert
    storage.create_image(image_record)

    # Second insert with same image_name should not raise IntegrityError due to INSERT OR IGNORE
    storage.create_image(image_record)

    record = storage.get_image("test_image_hash")
    assert record is not None
    assert record.image_name == "test_image_hash"


def test_video_recording_persistence(tmp_path):
    from uuid import uuid4
    from artemis.data_engine.storage import StorageManager
    from artemis.data_engine.models import VideoRecordingRecord

    storage = StorageManager(tmp_path / "data_engine.db", tmp_path)
    video_id = uuid4()
    session_id = uuid4()

    record = VideoRecordingRecord(
        video_id=video_id,
        session_id=session_id,
        device_id="emulator-5554",
        start_time=100.0,
        end_time=None,
        local_video_path="/tmp/recording.mp4",
    )

    # Test insert
    storage.create_video_recording(record)

    persisted = storage.get_video_recording(video_id)
    assert persisted is not None
    assert persisted.video_id == video_id
    assert persisted.session_id == session_id
    assert persisted.device_id == "emulator-5554"
    assert persisted.start_time == 100.0
    assert persisted.end_time is None
    assert persisted.local_video_path == "/tmp/recording.mp4"

    # Test update
    record.end_time = 150.0
    record.local_video_path = "/tmp/recording_final.mp4"
    storage.update_video_recording(record)

    updated = storage.get_video_recording(video_id)
    assert updated is not None
    assert updated.end_time == 150.0
    assert updated.local_video_path == "/tmp/recording_final.mp4"


def test_record_step_suppresses_identical_post_screenshot(tmp_path):
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_execution_setup = MagicMock()
    mock_execution_setup.traces_path = str(tmp_path)
    mock_ctx.execution_setup = mock_execution_setup
    mock_ctx.device = None

    engine = DataEngine(mock_ctx)
    engine.start_session("test_goal")
    same_image_bytes = b"identical_screen_bytes_123"

    step_id = engine.record_step(
        pre_screenshot_bytes=same_image_bytes,
        post_screenshot_bytes=same_image_bytes,
        summary="Test step with identical screenshot",
    )
    for t in list(engine._pending_threads):
        t.join()

    step_record = engine.storage.get_steps(engine.current_session_id)[0]
    assert step_record is not None
    assert step_record.pre_image_name is not None
    # post_image_name must be None when no visual transition occurred
    assert step_record.post_image_name is None


def test_update_step_execution_result_suppresses_identical_post_screenshot(tmp_path):
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_execution_setup = MagicMock()
    mock_execution_setup.traces_path = str(tmp_path)
    mock_ctx.execution_setup = mock_execution_setup
    mock_ctx.device = None

    engine = DataEngine(mock_ctx)
    engine.start_session("test_goal")
    image_bytes = b"pre_screen_bytes_456"

    step_id = engine.record_step(
        pre_screenshot_bytes=image_bytes,
        summary="Initial step",
    )

    step_record = engine.storage.get_steps(engine.current_session_id)[0]
    assert step_record.post_image_name is None

    # Update with post_image_name matching pre_image_name
    engine.storage.update_step_execution_result(
        step_id,
        {"status": "success"},
        post_image_name=step_record.pre_image_name,
    )

    updated_step = engine.storage.get_steps(engine.current_session_id)[0]
    assert updated_step.post_image_name is None

    # Update with distinct post_image_name
    engine.storage.update_step_execution_result(
        step_id,
        {"status": "success"},
        post_image_name="distinct_new_hash_789",
    )

    updated_step_distinct = engine.storage.get_steps(engine.current_session_id)[0]
    assert updated_step_distinct.post_image_name == "distinct_new_hash_789"
