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

"""Unit tests for UnifiedMobileController video recording and playback features."""

import time
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from artemis.context import ArtemisContext
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.drivers.mock.mock_driver import MockDeviceDriver
from artemis.utils.video import (
    RecordingSession,
    get_active_session,
    remove_active_session,
    set_active_session,
)


@pytest.fixture
def mock_ctx(tmp_path):
    ctx = MagicMock(spec=ArtemisContext)
    ctx.device = MagicMock()
    ctx.device.device_id = "emulator-5554"
    ctx.device.mobile_platform = "android"

    # DataEngine mock
    ctx.data_engine = MagicMock()
    ctx.data_engine.current_session_id = uuid4()
    ctx.data_engine.session_start_time = time.time()
    ctx.data_engine.storage = MagicMock()

    # Mock driver
    mock_driver = MockDeviceDriver(device_id="emulator-5554")
    ctx._active_driver = mock_driver
    return ctx


@pytest.mark.asyncio
async def test_unified_controller_start_recording(mock_ctx, tmp_path):
    controller = UnifiedMobileController(mock_ctx)
    remove_active_session("emulator-5554")

    # Mock scrcpy subprocess
    mock_proc = MagicMock()
    mock_proc.returncode = None

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
        with patch("asyncio.sleep", AsyncMock()):
            res = await controller.start_video_recording(output_dir=tmp_path)

            assert res.success is True
            assert get_active_session("emulator-5554") is not None
            assert mock_ctx.data_engine.record_video_start.called

            # Calling start again should report already in progress
            res2 = await controller.start_video_recording(output_dir=tmp_path)
            assert res2.success is False
            assert "already in progress" in res2.message

    remove_active_session("emulator-5554")


@pytest.mark.asyncio
async def test_unified_controller_stop_recording(mock_ctx, tmp_path):
    controller = UnifiedMobileController(mock_ctx)
    remove_active_session("emulator-5554")

    # Create dummy recording.mkv
    mkv_path = tmp_path / "recording.mkv"
    mkv_path.write_bytes(b"dummy mkv content")

    mock_proc = MagicMock()
    mock_proc.returncode = None
    mock_proc.terminate = MagicMock()
    mock_proc.wait = AsyncMock()

    session = RecordingSession(
        video_id=uuid4(),
        device_id="emulator-5554",
        start_time=time.time() - 10.0,
        data_engine_start_time=time.time() - 10.0,
        local_video_path=mkv_path,
        process=mock_proc,
    )
    set_active_session("emulator-5554", session)

    with patch.object(
        controller,
        "_convert_mkv_to_mp4",
        AsyncMock(side_effect=lambda src, dst: dst.write_bytes(b"mp4 content") or True),
    ):
        res = await controller.stop_video_recording()

        assert res.success is True
        assert res.video_path is not None
        assert str(res.video_path).endswith("recording.mp4")
        assert get_active_session("emulator-5554") is None
        assert mock_ctx.data_engine.record_video_stop.called


@pytest.mark.asyncio
async def test_unified_controller_extract_segment_metadata(mock_ctx, tmp_path):
    controller = UnifiedMobileController(mock_ctx)
    remove_active_session("emulator-5554")

    mkv_path = tmp_path / "recording.mkv"
    mkv_path.write_bytes(b"dummy mkv content")

    mock_proc = MagicMock()
    mock_proc.returncode = None

    session = RecordingSession(
        video_id=uuid4(),
        device_id="emulator-5554",
        start_time=time.time() - 20.0,
        data_engine_start_time=time.time() - 20.0,
        local_video_path=mkv_path,
        process=mock_proc,
    )
    set_active_session("emulator-5554", session)

    with patch(
        "artemis.controllers.unified_controller.trim_video",
        AsyncMock(side_effect=lambda src, s, e, dst: dst.write_bytes(b"segment mp4") or True),
    ):
        res = await controller.extract_segment_metadata(start_time=2.0, end_time=8.0)

        assert res.success is True
        assert res.video_path is not None
        assert res.video_path.exists()
        # Ensure session is still active (not stopped by extraction!)
        assert get_active_session("emulator-5554") is not None
    remove_active_session("emulator-5554")


def test_data_engine_video_lifecycle(tmp_path):
    from artemis.data_engine.engine import DataEngine
    from artemis.context import ArtemisContext

    mock_c = MagicMock(spec=ArtemisContext)
    mock_c.execution_setup = MagicMock(traces_path=str(tmp_path / "traces"))
    mock_c.device = None
    engine = DataEngine(mock_c)
    session_id = engine.start_session(goal="Test Video Engine")

    vid = uuid4()
    vpath = tmp_path / "recording.mp4"
    vpath.write_bytes(b"video bytes")

    # Start video recording
    engine.record_video_start(vid, "device-1", vpath)
    rec = engine.storage.get_video_recording(vid)
    assert rec is not None
    assert rec.device_id == "device-1"

    # Stop video recording
    engine.record_video_stop(vid, "device-1", vpath, time.time() - 10.0, time.time())
    sess = engine.storage.get_session(session_id)
    assert sess.video_filepath == str(vpath)

    # Move video path on finalize
    new_vpath = tmp_path / "archived" / "recording.mp4"
    new_vpath.parent.mkdir(parents=True, exist_ok=True)
    new_vpath.write_bytes(b"video bytes")
    engine.update_video_path(new_vpath)

    sess_updated = engine.storage.get_session(session_id)
    assert sess_updated.video_filepath == str(new_vpath)


@pytest.mark.asyncio
async def test_unified_controller_crash_recovery_and_multi_segment(mock_ctx, tmp_path):
    """Verify that if recording is interrupted, segments are auto-recovered and concatenated seamlessly."""
    controller = UnifiedMobileController(mock_ctx)
    remove_active_session("emulator-5554")

    seg1 = tmp_path / "recording_0.mkv"
    seg1.write_bytes(b"segment 0")
    seg2 = tmp_path / "recording_1.mkv"
    seg2.write_bytes(b"segment 1")

    mock_proc = MagicMock()
    mock_proc.returncode = 1  # Simulated crash

    session = RecordingSession(
        video_id=uuid4(),
        device_id="emulator-5554",
        start_time=time.time() - 30.0,
        data_engine_start_time=time.time() - 30.0,
        local_video_path=seg2,
        android_video_segments=[seg1],
        android_segment_index=1,
        process=mock_proc,
    )
    set_active_session("emulator-5554", session)

    with (
        patch(
            "artemis.controllers.unified_controller.concatenate_videos",
            AsyncMock(side_effect=lambda segs, out: out.write_bytes(b"combined mkv") or True),
        ),
        patch.object(
            controller,
            "_convert_mkv_to_mp4",
            AsyncMock(side_effect=lambda src, dst: dst.write_bytes(b"final mp4") or True),
        ),
    ):
        res = await controller.stop_video_recording()

        assert res.success is True
        assert res.video_path is not None
        assert res.video_path.exists()
        assert str(res.video_path).endswith("recording.mp4")
        assert get_active_session("emulator-5554") is None


@pytest.mark.asyncio
async def test_unified_controller_timeline_alignment(mock_ctx, tmp_path):
    """Verify that relative time offset between DataEngine T0 and video start is precisely calculated."""
    controller = UnifiedMobileController(mock_ctx)
    remove_active_session("emulator-5554")

    mkv_path = tmp_path / "recording.mkv"
    mkv_path.write_bytes(b"dummy mkv content")

    now = time.time()
    # Assume session started 15s ago, and video started 12s ago (offset = 3.0s)
    session_start_t0 = now - 15.0
    video_start_t = now - 12.0

    session = RecordingSession(
        video_id=uuid4(),
        device_id="emulator-5554",
        start_time=video_start_t,
        data_engine_start_time=session_start_t0,
        local_video_path=mkv_path,
        process=MagicMock(returncode=None),
    )
    set_active_session("emulator-5554", session)

    trimmed_ranges = []

    async def mock_trim(src, s_time, e_time, dst):
        trimmed_ranges.append((s_time, e_time))
        dst.write_bytes(b"trimmed")
        return True

    with patch(
        "artemis.controllers.unified_controller.trim_video", AsyncMock(side_effect=mock_trim)
    ):
        # Agent asks for system time range [5.0s, 10.0s]
        res = await controller.extract_segment_metadata(start_time=5.0, end_time=10.0)

        assert res.success is True
        # Since offset = 3.0s, video_start_relative_time should be 5.0 - 3.0 = 2.0s
        # and video_end_relative_time should be 10.0 - 3.0 = 7.0s
        assert len(trimmed_ranges) == 1
        s_time, e_time = trimmed_ranges[0]
        assert abs(s_time - 2.0) < 0.1
        assert abs(e_time - 7.0) < 0.1
        # actual_start_relative_time reported back to agent must align with system T0 (5.0s)
        assert abs(res.actual_start_relative_time - 5.0) < 0.1

    remove_active_session("emulator-5554")
