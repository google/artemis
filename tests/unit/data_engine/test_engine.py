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

import asyncio
import json
from unittest.mock import MagicMock, patch

from artemis.context import ArtemisContext
from artemis.data_engine.engine import DataEngine


def test_ipc_send_reconnects_and_retries_current_event(tmp_path):
    """A stale Windows TCP socket must not make the triggering SSE event disappear."""
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_execution_setup = MagicMock()
    mock_execution_setup.traces_path = str(tmp_path)
    mock_ctx.execution_setup = mock_execution_setup
    mock_ctx.device = None

    stale_socket = MagicMock()
    stale_socket.sendall.side_effect = OSError("connection reset")
    replacement_socket = MagicMock()

    with (
        patch("artemis.data_engine.engine.read_ipc_port", return_value=49152),
        patch(
            "artemis.data_engine.engine.socket.create_connection",
            side_effect=[stale_socket, replacement_socket],
        ) as create_connection,
    ):
        engine = DataEngine(mock_ctx)
        engine._publish("llm_stream", {"session_id": "s1", "chunk": "hello"})

    assert create_connection.call_count == 2
    stale_socket.close.assert_called_once()
    replacement_socket.sendall.assert_called_once()
    frame = replacement_socket.sendall.call_args.args[0]
    assert frame.endswith(b"\n")
    assert json.loads(frame.decode("utf-8"))["data"]["chunk"] == "hello"


def test_ipc_connect_falls_back_to_refreshed_port_file(tmp_path):
    """A UI restart must supersede the worker's stale inherited Windows port."""
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_execution_setup = MagicMock()
    mock_execution_setup.traces_path = str(tmp_path / "traces")
    mock_ctx.execution_setup = mock_execution_setup
    mock_ctx.device = None

    port_file = tmp_path / ".artemis_ipc_port"
    port_file.write_text("51629", encoding="utf-8")
    live_socket = MagicMock()

    with (
        patch("artemis.data_engine.engine.read_ipc_port", return_value=49555),
        patch("artemis.data_engine.engine.get_ipc_port_file", return_value=port_file),
        patch(
            "artemis.data_engine.engine.socket.create_connection",
            side_effect=[OSError("stale port"), live_socket],
        ) as create_connection,
    ):
        engine = DataEngine(mock_ctx)

    assert engine.ipc_socket is live_socket
    assert create_connection.call_args_list[0].args[0] == ("127.0.0.1", 49555)
    assert create_connection.call_args_list[1].args[0] == ("127.0.0.1", 51629)


def test_ipc_does_not_reconnect_after_engine_shutdown(tmp_path):
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_execution_setup = MagicMock()
    mock_execution_setup.traces_path = str(tmp_path)
    mock_ctx.execution_setup = mock_execution_setup
    mock_ctx.device = None
    connected_socket = MagicMock()

    with (
        patch("artemis.data_engine.engine.read_ipc_port", return_value=49152),
        patch("artemis.data_engine.engine.get_ipc_port_file", return_value=tmp_path / "none"),
        patch(
            "artemis.data_engine.engine.socket.create_connection",
            return_value=connected_socket,
        ) as create_connection,
    ):
        engine = DataEngine(mock_ctx)
        asyncio.run(engine.shutdown())
        engine._publish("llm_stream", {"chunk": "after shutdown"})

    assert create_connection.call_count == 1
    connected_socket.close.assert_called_once()


def test_ipc_connection_failure_is_backed_off(tmp_path):
    """A stale desktop port must not block every emitted trace event."""
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_execution_setup = MagicMock()
    mock_execution_setup.traces_path = str(tmp_path)
    mock_ctx.execution_setup = mock_execution_setup
    mock_ctx.device = None

    with (
        patch("artemis.data_engine.engine.read_ipc_port", return_value=49152),
        patch("artemis.data_engine.engine.get_ipc_port_file", return_value=tmp_path / "none"),
        patch(
            "artemis.data_engine.engine.socket.create_connection",
            side_effect=TimeoutError("stale port"),
        ) as create_connection,
    ):
        engine = DataEngine(mock_ctx)
        for index in range(20):
            engine._publish("llm_stream", {"chunk": str(index)})

    assert create_connection.call_count == 1


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
    assert persisted.status == "recording"

    # Test update
    record.end_time = 150.0
    record.local_video_path = "/tmp/recording_final.mp4"
    record.status = "ready"
    storage.update_video_recording(record)

    updated = storage.get_video_recording(video_id)
    assert updated is not None
    assert updated.end_time == 150.0
    assert updated.local_video_path == "/tmp/recording_final.mp4"
    assert updated.status == "ready"


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
    for t in list(engine._pending_threads):
        t.join()

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


def test_resumed_session_monotonic_step_numbering(tmp_path):
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_execution_setup = MagicMock()
    mock_execution_setup.traces_path = str(tmp_path)
    mock_ctx.execution_setup = mock_execution_setup
    mock_ctx.device = None

    # First runner records Step 1 and Step 2
    engine1 = DataEngine(mock_ctx)
    sid = engine1.start_session("Monotonic test")
    engine1.record_step(summary="Step 1 action")
    engine1.record_step(summary="Step 2 action")
    for t in list(engine1._pending_threads):
        t.join()

    # Second runner starts or resumes the same session
    engine2 = DataEngine(mock_ctx)
    engine2.start_session("Resumed goal", session_id=sid)
    assert engine2.current_step_number == 2

    # Recording a new step must be Step 3, strictly monotonic
    engine2.record_step(summary="Step 3 action")
    for t in list(engine2._pending_threads):
        t.join()

    steps = engine2.storage.get_steps(sid)
    assert len(steps) == 3
    assert [s.step_number for s in steps] == [1, 2, 3]


def test_end_session_normalizes_legacy_success_status(tmp_path):
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_execution_setup = MagicMock()
    mock_execution_setup.traces_path = str(tmp_path)
    mock_ctx.execution_setup = mock_execution_setup
    mock_ctx.device = None

    engine = DataEngine(mock_ctx)
    sid = engine.start_session("Terminal status normalization test")
    engine.end_session("success")

    session = engine.storage.get_session(sid)
    assert session is not None
    assert session.status == "completed"
    assert session.end_time is not None


def test_update_step_summary_includes_step_number_in_sse(tmp_path):
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_execution_setup = MagicMock()
    mock_execution_setup.traces_path = str(tmp_path)
    mock_ctx.execution_setup = mock_execution_setup
    mock_ctx.device = None

    published_events = []

    def mock_subscriber(event_type, data):
        published_events.append((event_type, data))

    engine = DataEngine(mock_ctx)
    engine.subscribe(mock_subscriber)
    sid = engine.start_session("SSE step_number verification")

    step_id = engine.record_step(summary="Initial step 1")
    for t in list(engine._pending_threads):
        t.join()

    assert engine.get_step_number(step_id) == 1

    # Call update_step_summary
    engine.update_step_summary(step_id, "Updated concise summary for Step 1")
    for t in list(engine._pending_threads):
        t.join()

    # Find the published step_updated event
    step_updated_events = [e for e in published_events if e[0] == "step_updated"]
    assert len(step_updated_events) >= 1
    last_update = step_updated_events[-1][1]
    assert last_update["step_id"] == str(step_id)
    assert last_update["summary"] == "Updated concise summary for Step 1"
    assert last_update["step_number"] == 1


def _foreground_engine(tmp_path):
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_execution_setup = MagicMock()
    mock_execution_setup.traces_path = str(tmp_path)
    mock_ctx.execution_setup = mock_execution_setup
    mock_ctx.device = None
    engine = DataEngine(mock_ctx)
    engine.start_session("foreground_app persistence")
    return engine


def test_record_step_persists_explicit_foreground_app(tmp_path):
    """M5: the historically dropped foreground_app parameter is persisted."""
    engine = _foreground_engine(tmp_path)
    engine.record_step(
        foreground_app="com.android.settings",
        ui_tree=[{"package": "com.other.app", "bounds": "[0,0][10,10]"}],
        summary="Explicit parameter wins",
    )
    for t in list(engine._pending_threads):
        t.join()

    step = engine.storage.get_steps(engine.current_session_id)[0]
    assert step.extra_metadata["foreground_app"] == "com.android.settings"


def test_record_step_derives_foreground_app_from_ui_tree(tmp_path):
    """Without the parameter, the dominant non-overlay package is stamped."""
    engine = _foreground_engine(tmp_path)
    engine.record_step(
        ui_tree=[
            {"package": "com.android.systemui", "bounds": "[0,0][10,10]"},
            {"package": "com.google.android.deskclock"},
            {
                "packageName": "com.google.android.deskclock",
                "children": [{"package": "com.google.android.deskclock"}],
            },
        ],
        summary="Derived from UI tree",
    )
    for t in list(engine._pending_threads):
        t.join()

    step = engine.storage.get_steps(engine.current_session_id)[0]
    assert step.extra_metadata["foreground_app"] == "com.google.android.deskclock"


def test_record_step_without_app_data_stamps_nothing(tmp_path):
    engine = _foreground_engine(tmp_path)
    engine.record_step(
        ui_tree=[{"package": "com.android.systemui"}],
        summary="Only overlay packages",
    )
    for t in list(engine._pending_threads):
        t.join()

    step = engine.storage.get_steps(engine.current_session_id)[0]
    assert "foreground_app" not in (step.extra_metadata or {})
