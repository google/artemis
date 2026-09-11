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

"""Unit tests for native screenrecord fallback when scrcpy is unavailable."""

import asyncio
from pathlib import Path
import time
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from artemis.context import ArtemisContext
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.drivers.mock.mock_driver import MockDeviceDriver
from artemis.utils.video import (
    NATIVE_RECORDING_DEVICE_DIR,
    RecordingSession,
    detect_video_tools_enabled,
    get_active_session,
    is_adb_available,
    is_adb_installed,
    is_scrcpy_installed,
    probe_video_segment,
    remove_active_session,
    set_active_session,
)


@pytest.fixture
def mock_ctx():
    ctx = MagicMock(spec=ArtemisContext)
    ctx.device = MagicMock()
    ctx.device.device_id = "real-device-1234"
    ctx.device.mobile_platform = "android"
    ctx.device.device_width = 1080
    ctx.device.device_height = 2400

    # DataEngine mock
    ctx.data_engine = MagicMock()
    ctx.data_engine.current_session_id = uuid4()
    ctx.data_engine.session_start_time = time.time()
    ctx.data_engine.storage = MagicMock()

    mock_driver = MockDeviceDriver(device_id="real-device-1234")
    ctx._active_driver = mock_driver
    return ctx


class _FakeProcess:
    """Mock process mimicking asyncio.subprocess.Process."""

    def __init__(self, returncode: int | None = None, stderr: bytes = b""):
        self.returncode = returncode
        self._stderr = stderr
        self.stderr = AsyncMock()
        self.stderr.read = AsyncMock(return_value=self._stderr)
        self.stdout = AsyncMock()
        self.stdout.read = AsyncMock(return_value=b"")

    async def wait(self) -> int:
        return self.returncode if self.returncode is not None else 0

    def send_signal(self, sig: int) -> None:
        self.returncode = 0

    def terminate(self) -> None:
        self.returncode = -15


# --- Detection Tests ---


def test_detect_video_tools_scrcpy_and_ffmpeg():
    with (
        patch("artemis.utils.video.is_ffmpeg_installed", return_value=True),
        patch("artemis.utils.video.is_scrcpy_installed", return_value=True),
        patch("artemis.utils.video.is_adb_installed", return_value=False),
    ):
        assert detect_video_tools_enabled() is True


def test_detect_video_tools_adb_only():
    with (
        patch("artemis.utils.video.is_ffmpeg_installed", return_value=False),
        patch("artemis.utils.video.is_scrcpy_installed", return_value=False),
        patch("artemis.utils.video.is_adb_installed", return_value=True),
    ):
        assert detect_video_tools_enabled() is True
        assert is_adb_available() is True


def test_detect_video_tools_nothing():
    with (
        patch("artemis.utils.video.is_ffmpeg_installed", return_value=False),
        patch("artemis.utils.video.is_scrcpy_installed", return_value=False),
        patch("artemis.utils.video.is_adb_installed", return_value=False),
    ):
        assert detect_video_tools_enabled() is False


# --- RecordingSession Model Tests ---


def test_recording_session_defaults_and_backend():
    session = RecordingSession(
        video_id=uuid4(),
        device_id="dev-1",
        start_time=100.0,
    )
    assert session.recording_backend == "scrcpy"
    assert session.native_device_segments == []

    native_session = RecordingSession(
        video_id=uuid4(),
        device_id="dev-2",
        start_time=100.0,
        recording_backend="native",
        native_device_segments=["/data/local/tmp/artemis/rec_000.mp4"],
    )
    assert native_session.recording_backend == "native"
    assert len(native_session.native_device_segments) == 1


# --- Controller Fallback Tests ---


@pytest.mark.asyncio
async def test_start_native_fallback_on_scrcpy_failure(mock_ctx, tmp_path):
    device_id = "real-device-1234"
    remove_active_session(device_id)
    controller = UnifiedMobileController(mock_ctx)

    # Scrcpy process exits immediately with error
    scrcpy_proc = _FakeProcess(returncode=1, stderr=b"ERROR: Encoder rejected format")
    # Native process runs successfully
    native_proc = _FakeProcess(returncode=None)

    async def fake_spawn(command):
        if "scrcpy" in command[0]:
            return scrcpy_proc
        return native_proc

    with (
        patch("artemis.controllers.unified_controller.is_scrcpy_installed", return_value=True),
        patch.object(controller, "_spawn_scrcpy", side_effect=fake_spawn),
        patch(
            "artemis.controllers.unified_controller.get_android_display_state",
            AsyncMock(return_value=(0, 1080, 2400)),
        ),
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=_FakeProcess(returncode=0))),
        patch("asyncio.sleep", AsyncMock()),
    ):
        res = await controller.start_video_recording(output_dir=tmp_path)

        assert res.success is True
        assert "native fallback" in res.message
        session = get_active_session(device_id)
        assert session is not None
        assert session.recording_backend == "native"
        assert len(session.native_device_segments) == 1
        assert session.native_device_segments[0].endswith("rec_000.mp4")
        assert session.watchdog_task is not None

        # Clean up
        if session.watchdog_task and not session.watchdog_task.done():
            session.watchdog_task.cancel()
    remove_active_session(device_id)


@pytest.mark.asyncio
async def test_start_native_fallback_when_scrcpy_not_installed(mock_ctx, tmp_path):
    device_id = "real-device-1234"
    remove_active_session(device_id)
    controller = UnifiedMobileController(mock_ctx)

    native_proc = _FakeProcess(returncode=None)

    async def fake_spawn(command):
        if "scrcpy" in command[0]:
            raise FileNotFoundError("scrcpy not found")
        return native_proc

    with (
        patch("artemis.controllers.unified_controller.is_scrcpy_installed", return_value=False),
        patch.object(controller, "_spawn_scrcpy", side_effect=fake_spawn),
        patch(
            "artemis.controllers.unified_controller.get_android_display_state",
            AsyncMock(return_value=None),
        ),
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=_FakeProcess(returncode=0))),
        patch("asyncio.sleep", AsyncMock()),
    ):
        res = await controller.start_video_recording(output_dir=tmp_path)

        assert res.success is True
        assert "native fallback" in res.message
        session = get_active_session(device_id)
        assert session is not None
        assert session.recording_backend == "native"

        # Clean up
        if session.watchdog_task and not session.watchdog_task.done():
            session.watchdog_task.cancel()
    remove_active_session(device_id)


@pytest.mark.asyncio
async def test_start_native_fallback_both_fail(mock_ctx, tmp_path):
    device_id = "real-device-1234"
    remove_active_session(device_id)
    controller = UnifiedMobileController(mock_ctx)

    scrcpy_proc = _FakeProcess(returncode=1, stderr=b"scrcpy error")
    native_proc = _FakeProcess(returncode=2, stderr=b"screenrecord: failed to create encoder")

    async def fake_spawn(command):
        if "scrcpy" in command[0]:
            return scrcpy_proc
        return native_proc

    with (
        patch("artemis.controllers.unified_controller.is_scrcpy_installed", return_value=True),
        patch.object(controller, "_spawn_scrcpy", side_effect=fake_spawn),
        patch(
            "artemis.controllers.unified_controller.get_android_display_state",
            AsyncMock(return_value=None),
        ),
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=_FakeProcess(returncode=0))),
        patch("asyncio.sleep", AsyncMock()),
    ):
        res = await controller.start_video_recording(output_dir=tmp_path)

        assert res.success is False
        assert "Both scrcpy and native screenrecord failed" in res.message
        assert get_active_session(device_id) is None
    remove_active_session(device_id)


@pytest.mark.asyncio
async def test_stop_native_recording_pulls_segment_and_writes_manifest(mock_ctx, tmp_path):
    device_id = "real-device-1234"
    remove_active_session(device_id)
    controller = UnifiedMobileController(mock_ctx)

    mock_proc = _FakeProcess(returncode=None)
    local_mp4 = tmp_path / "recording.mp4"
    remote_path = f"{NATIVE_RECORDING_DEVICE_DIR}/rec_000.mp4"

    session = RecordingSession(
        video_id=uuid4(),
        device_id=device_id,
        start_time=time.time() - 10.0,
        data_engine_start_time=time.time() - 10.0,
        local_video_path=local_mp4,
        process=mock_proc,
        recording_backend="native",
        native_device_segments=[remote_path],
    )
    set_active_session(device_id, session)

    async def fake_pull(dev_id, remote, local):
        local.write_bytes(b"native mp4 content")
        return True

    with (
        patch.object(controller, "_pull_native_segment", side_effect=fake_pull),
        patch(
            "artemis.controllers.unified_controller.write_recording_manifest",
            AsyncMock(return_value=tmp_path / "recording.json"),
        ),
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=_FakeProcess(returncode=0))),
    ):
        res = await controller.stop_video_recording()

        assert res.success is True
        assert res.video_path == local_mp4
        assert local_mp4.exists()
        assert get_active_session(device_id) is None
        assert mock_ctx.data_engine.record_video_stop.called
    remove_active_session(device_id)


@pytest.mark.asyncio
async def test_pull_native_segment_success(mock_ctx, tmp_path):
    controller = UnifiedMobileController(mock_ctx)
    local_path = tmp_path / "recording.mp4"
    remote_path = f"{NATIVE_RECORDING_DEVICE_DIR}/rec_000.mp4"

    async def fake_subproc(*args, **kwargs):
        if "pull" in args:
            local_path.write_bytes(b"video bytes")
            return _FakeProcess(returncode=0)
        return _FakeProcess(returncode=0)

    with patch("asyncio.create_subprocess_exec", side_effect=fake_subproc):
        result = await controller._pull_native_segment("device-1", remote_path, local_path)
        assert result is True
        assert local_path.exists()


@pytest.mark.asyncio
async def test_pull_native_segment_failure_on_nonzero_exit(mock_ctx, tmp_path):
    controller = UnifiedMobileController(mock_ctx)
    local_path = tmp_path / "recording.mp4"
    remote_path = f"{NATIVE_RECORDING_DEVICE_DIR}/rec_000.mp4"

    with patch(
        "asyncio.create_subprocess_exec",
        AsyncMock(return_value=_FakeProcess(returncode=1)),
    ):
        result = await controller._pull_native_segment("device-1", remote_path, local_path)
        assert result is False


@pytest.mark.asyncio
async def test_native_recording_watchdog_rolls_segment(mock_ctx, tmp_path):
    device_id = "real-device-1234"
    remove_active_session(device_id)
    controller = UnifiedMobileController(mock_ctx)

    local_mp4 = tmp_path / "recording.mp4"
    proc_segment_0 = _FakeProcess(returncode=0)  # Exited at 180s limit
    proc_segment_1 = _FakeProcess(returncode=None)  # New active segment

    session = RecordingSession(
        video_id=uuid4(),
        device_id=device_id,
        start_time=time.time() - 180.0,
        local_video_path=local_mp4,
        process=proc_segment_0,
        recording_backend="native",
        native_device_segments=[f"{NATIVE_RECORDING_DEVICE_DIR}/rec_000.mp4"],
    )
    set_active_session(device_id, session)

    async def fake_pull(dev_id, remote, local):
        local.write_bytes(b"segment 0 content")
        return True

    async def fake_start_next(sess, dev_id, out_dir):
        sess.process = proc_segment_1
        sess.native_device_segments.append(f"{NATIVE_RECORDING_DEVICE_DIR}/rec_001.mp4")
        sess.is_active = False  # Stop watchdog after one roll
        return True

    with (
        patch.object(controller, "_pull_native_segment", side_effect=fake_pull),
        patch.object(controller, "_start_native_recording_segment", side_effect=fake_start_next),
        patch("asyncio.sleep", AsyncMock()),
    ):
        await controller._native_recording_watchdog(device_id)

        assert session.android_segment_index == 1
        assert len(session.android_segment_records) == 1
        assert session.android_segment_records[0]["path"] == local_mp4

    remove_active_session(device_id)


@pytest.mark.asyncio
async def test_probe_video_segment_fallback_to_cv2(tmp_path):
    import cv2
    import numpy as np

    video_path = tmp_path / "probe_test.mp4"
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out = cv2.VideoWriter(str(video_path), fourcc, 30.0, (640, 480))
    for _ in range(30):
        out.write(np.zeros((480, 640, 3), dtype=np.uint8))
    out.release()

    with patch(
        "asyncio.create_subprocess_exec", side_effect=FileNotFoundError("ffprobe not found")
    ):
        metadata = await probe_video_segment(video_path)

        assert metadata["width"] == 640
        assert metadata["height"] == 480
        assert 0.9 <= metadata["duration"] <= 1.1


@pytest.mark.asyncio
async def test_probe_video_segment_nonexistent_file(tmp_path):
    video_path = tmp_path / "nonexistent.mp4"
    with patch(
        "asyncio.create_subprocess_exec", side_effect=FileNotFoundError("ffprobe not found")
    ):
        metadata = await probe_video_segment(video_path)
        assert metadata == {}
