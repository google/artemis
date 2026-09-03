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

import asyncio
import subprocess
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import cv2

from artemis.context import ArtemisContext
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.drivers.mock.mock_driver import MockDeviceDriver
from artemis.utils.video import (
    RecordingSession,
    build_scrcpy_record_command,
    extract_audio_from_video,
    extract_frames_at_timestamps,
    get_ffmpeg_path,
    get_active_session,
    normalize_recording_to_mp4,
    render_timeline_clip,
    remove_active_session,
    set_active_session,
)


@pytest.mark.asyncio
async def test_audio_extraction_rejects_silent_video_without_running_ffmpeg(tmp_path):
    silent_video = tmp_path / "silent.mp4"
    silent_video.write_bytes(b"video")
    probe = MagicMock(returncode=0)
    probe.communicate = AsyncMock(return_value=(b"", b""))

    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=probe)) as spawn:
        with pytest.raises(ValueError, match="no audio stream"):
            await extract_audio_from_video(silent_video)

    assert spawn.await_count == 1


def test_targeted_frame_extraction_uses_requested_timestamps():
    fixture = Path(__file__).parents[1] / "tools" / "inputs" / "recording.mp4"
    frames = extract_frames_at_timestamps(
        fixture,
        [0.0, 0.2, 0.4, 0.2, -1.0],
        max_frames=3,
        max_dimension=320,
    )

    assert [timestamp for timestamp, _ in frames] == [0.0, 0.2, 0.4]
    assert all(data.startswith(b"\xff\xd8") for _, data in frames)


def test_scrcpy_recording_locks_each_segment_orientation(tmp_path):
    output_path = tmp_path / "recording.mkv"

    command = build_scrcpy_record_command("scrcpy", "device-1", output_path)

    assert "--capture-orientation=@" in command
    assert command[command.index("--record") + 1] == str(output_path)


@pytest.mark.asyncio
async def test_normalize_recording_handles_resolution_change(tmp_path):
    """A portrait/landscape H.264 track must become one fixed-size MP4."""
    ffmpeg = get_ffmpeg_path()
    portrait = tmp_path / "portrait.mkv"
    landscape = tmp_path / "landscape.mkv"
    dynamic = tmp_path / "dynamic.mkv"
    output = tmp_path / "recording.mp4"

    for size, path in (("108x242", portrait), ("242x108", landscape)):
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"testsrc=size={size}:rate=10:duration=0.5",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(path),
            ],
            check=True,
            capture_output=True,
        )

    concat_file = tmp_path / "segments.txt"
    concat_file.write_text(
        f"file '{portrait.as_posix()}'\nfile '{landscape.as_posix()}'\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c",
            "copy",
            str(dynamic),
        ],
        check=True,
        capture_output=True,
    )

    assert await normalize_recording_to_mp4(dynamic, output, 108, 242)

    capture = cv2.VideoCapture(str(output))
    dimensions = set()
    frame_count = 0
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        frame_count += 1
        dimensions.add((frame.shape[1], frame.shape[0]))
    capture.release()

    assert frame_count >= 8
    assert dimensions == {(108, 242)}


@pytest.mark.asyncio
async def test_analyzer_clip_stays_continuous_across_orientation_segments(tmp_path):
    """The analyzer must still receive one decodable MP4 across a rotation."""
    ffmpeg = get_ffmpeg_path()
    portrait = tmp_path / "recording.mkv"
    landscape = tmp_path / "recording_001.mkv"
    output = tmp_path / "agent_clip.mp4"
    for color, size, path in (
        ("red", "108x242", portrait),
        ("blue", "242x108", landscape),
    ):
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color={color}:size={size}:rate=15:duration=1",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(path),
            ],
            check=True,
            capture_output=True,
        )

    assert await render_timeline_clip(
        [
            {"path": portrait, "start": 0.0, "end": 1.0},
            {"path": landscape, "start": 1.0, "end": 2.0},
        ],
        0.25,
        1.75,
        output,
        canvas_width=180,
        canvas_height=320,
    )

    capture = cv2.VideoCapture(str(output))
    dimensions = set()
    frames = 0
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        dimensions.add((frame.shape[1], frame.shape[0]))
        frames += 1
    capture.release()
    assert dimensions == {(180, 320)}
    assert frames >= 20


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
        with patch(
            "artemis.controllers.unified_controller.get_android_display_state",
            AsyncMock(return_value=(0, 1080, 2424)),
        ):
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

    with (
        patch(
            "artemis.controllers.unified_controller.remux_recording_to_mp4",
            AsyncMock(side_effect=lambda src, dst: (dst.write_bytes(b"mp4 content"), True)[1]),
        ),
        patch(
            "artemis.controllers.unified_controller.write_recording_manifest",
            AsyncMock(return_value=tmp_path / "recording.json"),
        ),
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
        "artemis.controllers.unified_controller.render_timeline_clip",
        AsyncMock(side_effect=lambda src, s, e, dst: dst.write_bytes(b"segment mp4") or True),
    ):
        res = await controller.extract_segment_metadata(start_time=2.0, end_time=8.0)

        assert res.success is True
        assert res.video_path is not None
        assert res.video_path.exists()
        # Ensure session is still active (not stopped by extraction!)
        assert get_active_session("emulator-5554") is not None
    remove_active_session("emulator-5554")


@pytest.mark.asyncio
async def test_segment_cache_is_scoped_to_recording_generation(mock_ctx, tmp_path):
    controller = UnifiedMobileController(mock_ctx)
    remove_active_session("emulator-5554")
    recording = tmp_path / "recording.mkv"
    recording.write_bytes(b"recording")
    session = RecordingSession(
        video_id=uuid4(),
        device_id="emulator-5554",
        start_time=time.time() - 20.0,
        data_engine_start_time=time.time() - 20.0,
        local_video_path=recording,
        process=MagicMock(returncode=None),
    )
    set_active_session("emulator-5554", session)

    render = AsyncMock(
        side_effect=lambda source, start, end, output: output.write_bytes(b"clip") or True
    )
    with patch("artemis.controllers.unified_controller.render_timeline_clip", render):
        first = await controller.extract_segment_metadata(2.0, 8.0)
        cached = await controller.extract_segment_metadata(2.0, 8.0)
        session.generation = 1
        regenerated = await controller.extract_segment_metadata(2.0, 8.0)

    assert first is cached
    assert regenerated is not cached
    assert regenerated.generation == 1
    assert render.await_count == 2
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
        android_segment_records=[
            {
                "path": seg1,
                "output_path": tmp_path / "recording.mp4",
                "start": 0.0,
                "end": 15.0,
            }
        ],
        android_segment_index=1,
        process=mock_proc,
    )
    set_active_session("emulator-5554", session)

    with (
        patch(
            "artemis.controllers.unified_controller.remux_recording_to_mp4",
            AsyncMock(side_effect=lambda src, dst: (dst.write_bytes(b"final mp4"), True)[1]),
        ),
        patch(
            "artemis.controllers.unified_controller.write_recording_manifest",
            AsyncMock(return_value=tmp_path / "recording.json"),
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
        "artemis.controllers.unified_controller.render_timeline_clip",
        AsyncMock(side_effect=mock_trim),
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


class _FakeScrcpyProcess:
    """Minimal stand-in for an asyncio subprocess with a real stdout reader."""

    def __init__(self, lines: list[bytes], *, close: bool = False):
        self.stdout = asyncio.StreamReader()
        for line in lines:
            self.stdout.feed_data(line)
        if close:
            self.stdout.feed_eof()
        self.stderr = MagicMock()
        self.stderr.read = AsyncMock(return_value=b"")
        self.returncode = None
        self.waited = False

    async def wait(self):
        self.waited = True
        self.returncode = 1
        return self.returncode


@pytest.mark.asyncio
async def test_await_scrcpy_first_frame_uses_recording_started_marker():
    from artemis.utils.video import await_scrcpy_first_frame

    proc = _FakeScrcpyProcess(
        [
            b"scrcpy 4.1 <https://github.com/Genymobile/scrcpy>\n",
            b"INFO: ADB device found:\n",
            b"INFO: Recording started to matroska file: C:\\rec\\recording.mkv\n",
        ]
    )
    spawned_at = time.time() - 2.0
    before = time.time()
    estimate = await await_scrcpy_first_frame(proc, spawned_at)
    after = time.time()

    assert before <= estimate <= after
    assert after - before < 0.5


@pytest.mark.asyncio
async def test_await_scrcpy_first_frame_never_precedes_spawn():
    from artemis.utils.video import await_scrcpy_first_frame

    proc = _FakeScrcpyProcess([b"INFO: Recording started to matroska file: x.mkv\n"])
    spawned_at = time.time()
    estimate = await await_scrcpy_first_frame(proc, spawned_at)
    assert estimate >= spawned_at


@pytest.mark.asyncio
async def test_await_scrcpy_first_frame_falls_back_when_stdout_closes():
    from artemis.utils.video import SCRCPY_STARTUP_FALLBACK_SECONDS, await_scrcpy_first_frame

    proc = _FakeScrcpyProcess([b"scrcpy 4.1\n"], close=True)
    spawned_at = time.time()
    estimate = await await_scrcpy_first_frame(proc, spawned_at)

    assert estimate == pytest.approx(spawned_at + SCRCPY_STARTUP_FALLBACK_SECONDS)
    assert proc.waited is True
    assert proc.returncode == 1


@pytest.mark.asyncio
async def test_await_scrcpy_first_frame_falls_back_on_timeout():
    from artemis.utils.video import SCRCPY_STARTUP_FALLBACK_SECONDS, await_scrcpy_first_frame

    proc = _FakeScrcpyProcess([b"scrcpy 4.1\n"])  # marker never arrives, stdout stays open
    spawned_at = time.time()
    estimate = await await_scrcpy_first_frame(proc, spawned_at, timeout=0.1)
    assert estimate == pytest.approx(spawned_at + SCRCPY_STARTUP_FALLBACK_SECONDS)


@pytest.mark.asyncio
async def test_start_recording_anchors_timeline_to_first_frame(mock_ctx, tmp_path):
    controller = UnifiedMobileController(mock_ctx)
    remove_active_session("emulator-5554")

    proc = _FakeScrcpyProcess([b"INFO: Recording started to matroska file: x.mkv\n"])
    before = time.time()
    with (
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
        patch(
            "artemis.controllers.unified_controller.get_android_display_state",
            AsyncMock(return_value=(0, 1080, 2424)),
        ),
    ):
        res = await controller.start_video_recording(output_dir=tmp_path)

    try:
        assert res.success is True
        session = get_active_session("emulator-5554")
        assert session is not None
        assert session.start_time >= before
        assert session.android_segment_started_at == session.start_time
        kwargs = mock_ctx.data_engine.record_video_start.call_args.kwargs
        assert kwargs["start_time"] == session.start_time
    finally:
        session = get_active_session("emulator-5554")
        if session and session.watchdog_task:
            session.watchdog_task.cancel()
        remove_active_session("emulator-5554")


@pytest.mark.asyncio
async def test_next_segment_anchors_at_first_frame(mock_ctx, tmp_path):
    controller = UnifiedMobileController(mock_ctx)
    session = RecordingSession(
        video_id=uuid4(),
        device_id="emulator-5554",
        start_time=time.time() - 30.0,
        data_engine_start_time=time.time() - 30.0,
        local_video_path=tmp_path / "recording.mkv",
    )
    proc = _FakeScrcpyProcess([b"INFO: Recording started to matroska file: y.mkv\n"])
    before = time.time()
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        ok = await controller._start_next_recording_segment(session, (1, 2424, 1080))

    assert ok is True
    assert session.android_segment_started_at is not None
    assert session.android_segment_started_at >= before
    assert session.android_rotation == 1
    assert session.local_video_path == tmp_path / "recording_001.mkv"
