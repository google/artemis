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

"""Hermetic tests for the native iOS simctl recorder."""

import asyncio
from collections import deque
import json
from pathlib import Path
import signal
import subprocess
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import cv2
import pytest

from artemis.drivers.ios import recording as rec
from artemis.drivers.ios.recording import (
    IosRecordingSession,
    IosScreenRecorder,
    finalize_mov_to_mp4,
)
from artemis.drivers.ios.xcode_driver import XcodeSimulatorDriver
from third_party.mobile_use.utils.video import (
    get_active_session,
    remove_active_session,
    set_active_session,
)

UDID = "E1D9F1D1-04E5-4E95-801F-830B854FD3E2"


class FakeStderr:
    """Queued stderr lines; hangs like a live pipe until ``feed_eof``."""

    def __init__(self, lines=None, delay: float = 0.0, eof: bool = False):
        self._lines = deque(lines if lines is not None else [b"Recording started\n"])
        self._delay = delay
        self._eof = asyncio.Event()
        if eof:
            self._eof.set()

    def feed(self, line: bytes) -> None:
        self._lines.append(line)

    def feed_eof(self) -> None:
        self._eof.set()

    async def readline(self):
        while True:
            if self._lines:
                if self._delay:
                    await asyncio.sleep(self._delay)
                return self._lines.popleft()
            if self._eof.is_set():
                return b""
            await asyncio.sleep(0.005)


class FakeRecorderProcess:
    """Minimal asyncio.subprocess.Process stand-in for simctl recordVideo."""

    def __init__(self, stderr=None, *, ignore_signals=()):
        self.stderr = stderr if stderr is not None else FakeStderr()
        self.returncode = None
        self.signals: list[int] = []
        self.terminated = False
        self.killed = False
        self._exit = asyncio.Event()
        self._ignore = set(ignore_signals)

    def send_signal(self, sig):
        self.signals.append(sig)
        if sig not in self._ignore and self.returncode is None:
            self.returncode = 0
            self._exit.set()

    def terminate(self):
        self.terminated = True
        if self.returncode is None:
            self.returncode = -int(signal.SIGTERM)
            self._exit.set()

    def kill(self):
        self.killed = True
        if self.returncode is None:
            self.returncode = -int(signal.SIGKILL)
            self._exit.set()

    async def wait(self):
        await self._exit.wait()
        return self.returncode


def spawn_factory(procs):
    """Return (spawn, created) where spawn serves FakeRecorderProcess items."""
    created: list[tuple] = []

    async def spawn(*argv, **kwargs):
        created.append(argv)
        index = min(len(created) - 1, len(procs) - 1)
        return procs[index]

    return spawn, created


@pytest.fixture
def recorder_env(monkeypatch):
    """Patch the recorder's child-process and probe seams."""
    env = SimpleNamespace(finalized=[], spawns=None)
    monkeypatch.setattr(rec, "probe_display_dimensions", AsyncMock(return_value=(1206, 2622)))

    async def fake_finalize(source_path, output_path, width, height, span):
        env.finalized.append((Path(source_path), Path(output_path), width, height, span))
        output_path.write_bytes(b"fake mp4")
        return True

    monkeypatch.setattr(rec, "finalize_mov_to_mp4", fake_finalize)
    monkeypatch.setattr(
        "artemis.utils.video.probe_video_segment",
        AsyncMock(return_value={"duration": 1.0, "width": 100, "height": 200}),
    )
    monkeypatch.setattr(rec, "WATCHDOG_INTERVAL_SECONDS", 0.01)
    return env


def patch_spawn(monkeypatch, procs):
    spawn, created = spawn_factory(procs)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    return created


@pytest.mark.asyncio
async def test_recordvideo_argv_pins_udid_without_force_or_android_tools(
    recorder_env, monkeypatch, tmp_path
):
    procs = [FakeRecorderProcess()]
    created = patch_spawn(monkeypatch, procs)
    recorder = IosScreenRecorder(UDID)

    session = await recorder.start(output_dir=tmp_path)

    argv = created[0]
    assert argv[:4] == ("xcrun", "simctl", "io", UDID)
    assert "recordVideo" in argv and "--codec=h264" in argv
    assert str(tmp_path / "recording.mov") in argv
    assert "--force" not in argv
    assert not any("adb" in part or "scrcpy" in part for part in argv)
    assert session.is_active and session.capture_width == 1206
    await recorder.stop()


@pytest.mark.asyncio
async def test_marker_anchors_timeline_not_process_spawn(recorder_env, monkeypatch, tmp_path):
    procs = [FakeRecorderProcess(FakeStderr(delay=0.05))]
    created = patch_spawn(monkeypatch, procs)
    recorder = IosScreenRecorder(UDID)

    before = time.time()
    session = await recorder.start(output_dir=tmp_path)

    assert session.start_time >= before + 0.03  # anchored at marker, not spawn
    assert session.anchor_monotonic is not None
    assert session.segment_started_monotonic == session.anchor_monotonic
    await recorder.stop()


@pytest.mark.asyncio
async def test_missing_marker_times_out_and_reaps(recorder_env, monkeypatch, tmp_path):
    monkeypatch.setattr(rec, "STARTUP_TIMEOUT_SECONDS", 0.05)
    proc = FakeRecorderProcess(FakeStderr(lines=[]))  # hangs, never marks
    patch_spawn(monkeypatch, [proc])
    recorder = IosScreenRecorder(UDID)

    with pytest.raises(TimeoutError):
        await recorder.start(output_dir=tmp_path)

    assert signal.SIGINT in proc.signals
    assert proc.returncode == 0
    assert recorder.session is not None and not recorder.session.is_active


@pytest.mark.asyncio
async def test_stderr_eof_before_marker_fails(recorder_env, monkeypatch, tmp_path):
    proc = FakeRecorderProcess(FakeStderr(lines=[], eof=True))
    patch_spawn(monkeypatch, [proc])
    recorder = IosScreenRecorder(UDID)

    with pytest.raises(RuntimeError, match="Recording started"):
        await recorder.start(output_dir=tmp_path)
    assert proc.returncode == 0  # reaped via SIGINT


@pytest.mark.asyncio
async def test_stderr_eof_error_reports_simctl_reason(recorder_env, monkeypatch, tmp_path):
    stderr = FakeStderr(
        lines=[b"Error starting video recorder: Host recording is already in progress\n"],
        eof=True,
    )
    patch_spawn(monkeypatch, [FakeRecorderProcess(stderr)])
    recorder = IosScreenRecorder(UDID)

    with pytest.raises(RuntimeError, match="already in progress"):
        await recorder.start(output_dir=tmp_path)


@pytest.mark.asyncio
async def test_stale_recorders_are_siginted_before_spawn(recorder_env, monkeypatch, tmp_path):
    """An orphaned recordVideo on the same UDID is stopped before spawning."""
    kills: list[tuple[int, int]] = []
    exited: set[int] = set()

    def fake_kill(pid, sig):
        kills.append((pid, sig))
        if sig == signal.SIGINT:
            exited.add(pid)
        elif sig == 0 and pid in exited:
            raise ProcessLookupError

    monkeypatch.setattr(rec.os, "kill", fake_kill)
    monkeypatch.setattr(
        rec.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="4321\n", returncode=0),
    )
    created = patch_spawn(monkeypatch, [FakeRecorderProcess()])
    recorder = IosScreenRecorder(UDID)

    session = await recorder.start(output_dir=tmp_path)

    assert (4321, signal.SIGINT) in kills
    # pgrep goes through subprocess.run, so the first spawned child is still
    # the xcrun recordVideo invocation itself.
    assert created[0][:4] == ("xcrun", "simctl", "io", UDID)
    assert session.is_active
    await recorder.stop()


@pytest.mark.asyncio
async def test_busy_spawn_reaps_cross_device_orphan_and_retries(
    recorder_env, monkeypatch, tmp_path
):
    """A recorder on a sibling UDID holds the host lock: sweep all, retry once."""
    kills: list[tuple[int, int]] = []
    exited: set[int] = set()

    def fake_kill(pid, sig):
        kills.append((pid, sig))
        if sig == signal.SIGINT:
            exited.add(pid)
        elif sig == 0 and pid in exited:
            raise ProcessLookupError

    monkeypatch.setattr(rec.os, "kill", fake_kill)
    pgrep_results = iter(
        [
            "",  # pre-spawn sweep: no same-UDID recorder
            "9999\n",  # busy-retry sweep: a sibling device's orphan
        ]
    )
    pgrep_patterns: list[str] = []
    monkeypatch.setattr(
        rec.subprocess,
        "run",
        lambda argv, **kwargs: (
            pgrep_patterns.append(argv[2])
            or SimpleNamespace(stdout=next(pgrep_results, ""), returncode=0)
        ),
    )
    busy = FakeRecorderProcess(
        FakeStderr(
            lines=[b"Error starting video recorder: Host recording is already in progress\n"],
            eof=True,
        )
    )
    healthy = FakeRecorderProcess()
    created = patch_spawn(monkeypatch, [busy, healthy])
    recorder = IosScreenRecorder(UDID)

    session = await recorder.start(output_dir=tmp_path)

    assert (9999, signal.SIGINT) in kills
    assert any("recordVideo" in p and UDID not in p for p in pgrep_patterns)
    assert sum("recordVideo" in " ".join(argv) for argv in created) == 2
    assert session.is_active and session.process is healthy
    await recorder.stop()


@pytest.mark.asyncio
async def test_startup_cancellation_reaps_child(recorder_env, monkeypatch, tmp_path):
    proc = FakeRecorderProcess(FakeStderr(lines=[]))  # marker never arrives
    created = patch_spawn(monkeypatch, [proc])
    recorder = IosScreenRecorder(UDID)

    task = asyncio.create_task(recorder.start(output_dir=tmp_path))
    # The pre-spawn stale-recorder sweep adds a suspension point before the
    # child exists; cancel only after spawn so the in-flight child is reaped.
    for _ in range(200):
        if created:
            break
        await asyncio.sleep(0.005)
    assert created, "recorder child was never spawned"
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert proc.returncode is not None
    assert signal.SIGINT in proc.signals


@pytest.mark.asyncio
async def test_cancelled_probe_after_marker_reaps_child(recorder_env, monkeypatch, tmp_path):
    """Cancelling during the dimension probe must not leak the recorder child."""
    proc = FakeRecorderProcess()  # marker arrives instantly
    patch_spawn(monkeypatch, [proc])
    probe_entered = threading.Event()
    probe_gate = threading.Event()

    async def gated_probe(device_id):
        probe_entered.set()
        while not probe_gate.is_set():
            await asyncio.sleep(0.005)
        return (1206, 2622)

    monkeypatch.setattr(rec, "probe_display_dimensions", gated_probe)
    recorder = IosScreenRecorder(UDID)

    task = asyncio.create_task(recorder.start(output_dir=tmp_path))
    try:
        assert await asyncio.to_thread(probe_entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()  # still gated inside the probe await
    finally:
        probe_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    session = recorder.session
    assert session is not None and not session.is_active
    assert proc.returncode is not None
    assert signal.SIGINT in proc.signals
    assert session.stderr_task is None or session.stderr_task.done()


@pytest.mark.asyncio
async def test_probe_failure_after_marker_reaps_child(recorder_env, monkeypatch, tmp_path):
    """A dimension-probe error after the marker must reap the owned child."""
    proc = FakeRecorderProcess()
    patch_spawn(monkeypatch, [proc])

    async def boom(device_id):
        raise RuntimeError("display probe exploded")

    monkeypatch.setattr(rec, "probe_display_dimensions", boom)
    recorder = IosScreenRecorder(UDID)

    with pytest.raises(RuntimeError, match="display probe exploded"):
        await recorder.start(output_dir=tmp_path)

    session = recorder.session
    assert session is not None and not session.is_active
    assert proc.returncode is not None
    assert signal.SIGINT in proc.signals
    assert session.stderr_task is None or session.stderr_task.done()


@pytest.mark.asyncio
async def test_sigint_terminate_kill_escalation(recorder_env, monkeypatch, tmp_path):
    monkeypatch.setattr(rec, "SIGINT_FLUSH_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(rec, "TERMINATE_TIMEOUT_SECONDS", 0.05)
    term_proc = FakeRecorderProcess(ignore_signals={signal.SIGINT})
    kill_proc = FakeRecorderProcess(ignore_signals={signal.SIGINT})
    kill_proc.terminate = lambda: setattr(kill_proc, "terminated", True)

    patch_spawn(monkeypatch, [term_proc])
    recorder = IosScreenRecorder(UDID)
    await recorder.start(output_dir=tmp_path)
    await recorder.stop()
    assert term_proc.signals == [signal.SIGINT]
    assert term_proc.terminated
    assert not term_proc.killed

    patch_spawn(monkeypatch, [kill_proc])
    recorder2 = IosScreenRecorder(UDID)
    await recorder2.start(output_dir=tmp_path / "b")
    await recorder2.stop()
    assert kill_proc.terminated and kill_proc.killed


@pytest.mark.asyncio
async def test_stderr_buffer_is_bounded(recorder_env, monkeypatch, tmp_path):
    stderr = FakeStderr()
    proc = FakeRecorderProcess(stderr)
    patch_spawn(monkeypatch, [proc])
    recorder = IosScreenRecorder(UDID)
    session = await recorder.start(output_dir=tmp_path)

    for index in range(rec.STDERR_BUFFER_LINES * 2):
        stderr.feed(f"line {index}\n".encode())
    await asyncio.sleep(0.05)

    assert len(session.stderr_lines) <= rec.STDERR_BUFFER_LINES
    assert session.stderr_lines[-1].startswith("line")
    await recorder.stop()


@pytest.mark.asyncio
async def test_double_start_rejected_and_stop_without_recording(
    recorder_env, monkeypatch, tmp_path
):
    patch_spawn(monkeypatch, [FakeRecorderProcess()])
    recorder = IosScreenRecorder(UDID)

    assert await recorder.stop() is None  # no session yet

    await recorder.start(output_dir=tmp_path)
    with pytest.raises(RuntimeError, match="already active"):
        await recorder.start(output_dir=tmp_path)
    await recorder.stop()
    assert await recorder.stop() is not None  # stopped session metadata retained


@pytest.mark.asyncio
async def test_output_collision_never_overwrites(recorder_env, monkeypatch, tmp_path):
    original = tmp_path / "recording.mov"
    original.write_bytes(b"pre-existing capture")
    created = patch_spawn(monkeypatch, [FakeRecorderProcess()])
    recorder = IosScreenRecorder(UDID)

    session = await recorder.start(output_dir=tmp_path)

    assert "recording_001.mov" in str(created[0][-1])
    assert session.local_video_path.name == "recording_001.mov"
    assert original.read_bytes() == b"pre-existing capture"
    await recorder.stop()


@pytest.mark.asyncio
async def test_rotation_rolls_segment_with_raw_dimensions(recorder_env, monkeypatch, tmp_path):
    procs = [FakeRecorderProcess(), FakeRecorderProcess()]
    patch_spawn(monkeypatch, procs)
    dims_state = {"value": (1206, 2622)}
    monkeypatch.setattr(
        rec,
        "probe_display_dimensions",
        AsyncMock(side_effect=lambda *_: dims_state["value"]),
    )
    recorder = IosScreenRecorder(UDID)

    session = await recorder.start(output_dir=tmp_path)
    dims_state["value"] = (2622, 1206)  # rotate the display
    for _ in range(200):
        if session.segments and session.capture_width == 2622:
            break
        await asyncio.sleep(0.01)

    assert session.segments, "watchdog did not roll on rotation"
    first = session.segments[0]
    assert (first["width"], first["height"]) == (1206, 2622)
    assert session.capture_width == 2622 and session.capture_height == 1206
    assert session.generation >= 1
    assert signal.SIGINT in procs[0].signals
    await recorder.stop()


@pytest.mark.asyncio
async def test_duration_limit_rolls_segment(recorder_env, monkeypatch, tmp_path):
    procs = [FakeRecorderProcess(), FakeRecorderProcess()]
    patch_spawn(monkeypatch, procs)
    recorder = IosScreenRecorder(UDID)

    session = await recorder.start(output_dir=tmp_path, max_duration_seconds=1)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not session.segments:
        await asyncio.sleep(0.02)

    assert session.segments, "watchdog did not roll on duration limit"
    await recorder.stop()


@pytest.mark.asyncio
async def test_crash_recovery_is_bounded(recorder_env, monkeypatch, tmp_path):
    healthy = FakeRecorderProcess()
    dead = [FakeRecorderProcess(FakeStderr(lines=[], eof=True)) for _ in range(5)]
    created = patch_spawn(monkeypatch, [healthy] + dead)
    recorder = IosScreenRecorder(UDID)

    session = await recorder.start(output_dir=tmp_path)
    healthy.returncode = 1  # simulate recorder crash
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and session.is_active:
        await asyncio.sleep(0.02)

    assert not session.is_active
    assert len(created) <= rec.MAX_CONSECUTIVE_FAILURES + 1
    assert any("recovery limit" in error for error in session.errors)


@pytest.mark.asyncio
async def test_seal_finalizes_segment_and_continues(recorder_env, monkeypatch, tmp_path):
    procs = [FakeRecorderProcess(), FakeRecorderProcess()]
    patch_spawn(monkeypatch, procs)
    recorder = IosScreenRecorder(UDID)
    session = await recorder.start(output_dir=tmp_path)

    await asyncio.sleep(0.05)
    await recorder.seal(through_time=0.02)

    # The sealed boundary covers through_time; content runs to the SIGINT point.
    assert 0.02 <= session.segments[0]["end"] <= 0.5
    assert session.sealed_until >= 0.02
    assert session.generation == 1
    assert session.is_active and session.process is procs[1]
    await recorder.seal(through_time=0.01)  # already sealed through this value
    assert len(session.segments) == 1
    await recorder.stop()


@pytest.mark.asyncio
async def test_stop_finalizes_manifest_and_returns_mp4(recorder_env, monkeypatch, tmp_path):
    proc = FakeRecorderProcess()
    patch_spawn(monkeypatch, [proc])
    recorder = IosScreenRecorder(UDID)
    session = await recorder.start(output_dir=tmp_path)

    result = await recorder.stop()

    assert result == tmp_path / "recording.mp4"
    assert signal.SIGINT in proc.signals
    manifest = json.loads((tmp_path / "recording.json").read_text())
    assert manifest["version"] == 2
    assert manifest["segments"] and manifest["segments"][0]["file"] == "recording.mp4"
    assert not session.is_active


@pytest.mark.asyncio
async def test_stop_cancelled_caller_still_reaps_and_finalizes(recorder_env, monkeypatch, tmp_path):
    gate = asyncio.Event()

    async def slow_finalize(source_path, output_path, width, height, span):
        await gate.wait()
        output_path.write_bytes(b"fake mp4")
        return True

    monkeypatch.setattr(rec, "finalize_mov_to_mp4", slow_finalize)
    proc = FakeRecorderProcess()
    patch_spawn(monkeypatch, [proc])
    recorder = IosScreenRecorder(UDID)
    await recorder.start(output_dir=tmp_path)

    task = asyncio.create_task(recorder.stop())
    await asyncio.sleep(0.05)
    task.cancel()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert proc.returncode == 0
    assert (tmp_path / "recording.mp4").exists()


@pytest.mark.asyncio
async def test_conversion_failure_reports_partial_and_keeps_raw(
    recorder_env, monkeypatch, tmp_path
):
    async def failing(*args, **kwargs):
        return False

    monkeypatch.setattr(rec, "finalize_mov_to_mp4", failing)
    patch_spawn(monkeypatch, [FakeRecorderProcess()])
    recorder = IosScreenRecorder(UDID)
    session = await recorder.start(output_dir=tmp_path)

    assert await recorder.stop() is None
    assert any("finalization" in error for error in session.errors)


@pytest.mark.asyncio
async def test_disconnect_finalizes_recorder_and_clears_registry(
    recorder_env, monkeypatch, tmp_path
):
    proc = FakeRecorderProcess()
    patch_spawn(monkeypatch, [proc])
    driver = XcodeSimulatorDriver(device_id=UDID)
    driver._session_key = "session-key"
    bridge = SimpleNamespace(
        connected=True,
        call=AsyncMock(),
        close=AsyncMock(),
        start=AsyncMock(),
    )
    driver._bridge = bridge

    await driver.start_video_recording(output_dir=tmp_path)
    session = driver.recording_session
    set_active_session(UDID, session)

    await driver.disconnect()

    assert proc.returncode == 0
    assert get_active_session(UDID) is None
    bridge.call.assert_awaited_once()
    assert bridge.call.await_args.args[0] == "DeviceInteractionEndSession"
    bridge.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_disconnect_closes_bridge_even_when_finalize_fails(
    recorder_env, monkeypatch, tmp_path
):
    proc = FakeRecorderProcess()
    patch_spawn(monkeypatch, [proc])
    monkeypatch.setattr(rec, "finalize_mov_to_mp4", AsyncMock(return_value=False))
    driver = XcodeSimulatorDriver(device_id=UDID)
    driver._session_key = "session-key"
    bridge = SimpleNamespace(connected=True, call=AsyncMock(), close=AsyncMock(), start=AsyncMock())
    driver._bridge = bridge

    await driver.start_video_recording(output_dir=tmp_path)
    set_active_session(UDID, driver.recording_session)
    await driver.disconnect()

    assert proc.returncode == 0
    assert get_active_session(UDID) is None
    bridge.close.assert_awaited_once()
    assert driver.recording_session.errors


@pytest.mark.asyncio
async def test_disconnect_leaves_other_sessions_registry(recorder_env, monkeypatch, tmp_path):
    proc = FakeRecorderProcess()
    patch_spawn(monkeypatch, [proc])
    other = IosRecordingSession(video_id=uuid4(), device_id=UDID, start_time=0.0)
    set_active_session(UDID, other)
    driver = XcodeSimulatorDriver(device_id=UDID)
    driver._session_key = "session-key"
    driver._bridge = SimpleNamespace(
        connected=True, call=AsyncMock(), close=AsyncMock(), start=AsyncMock()
    )

    await driver.start_video_recording(output_dir=tmp_path)
    await driver.disconnect()

    assert get_active_session(UDID) is other
    remove_active_session(UDID)


# --- Real FFmpeg regression coverage (bundled binary, no Xcode/simulator) ---


def _make_vfr_mov(path: Path, width: int = 320, height: int = 240) -> Path:
    """Two VFR frames: red at t=0, blue at t=0.5 (from 30fps concat)."""
    ffmpeg = rec.get_ffmpeg_path()
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=red:s={width}x{height}:r=30:d=0.5",
            "-f",
            "lavfi",
            "-i",
            f"color=c=blue:s={width}x{height}:r=30:d=0.5",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1,select='eq(n,0)+eq(n,15)'[v]",
            "-map",
            "[v]",
            "-fps_mode",
            "vfr",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def _frame_at(video: Path, seconds: float):
    capture = cv2.VideoCapture(str(video))
    try:
        capture.set(cv2.CAP_PROP_POS_MSEC, seconds * 1000)
        ok, frame = capture.read()
        assert ok and frame is not None
        return frame
    finally:
        capture.release()


def _mean_bgr(frame):
    return frame.mean(axis=(0, 1))


@pytest.mark.asyncio
async def test_finalize_preserves_vfr_event_times_without_stretching(tmp_path):
    source = _make_vfr_mov(tmp_path / "segment.mov")
    output = tmp_path / "recording.mp4"

    assert await finalize_mov_to_mp4(source, output, 320, 240, 2.0)

    capture = cv2.VideoCapture(str(output))
    fps = capture.get(cv2.CAP_PROP_FPS)
    frame_count = capture.get(cv2.CAP_PROP_FRAME_COUNT)
    capture.release()
    assert fps == pytest.approx(30, abs=0.5)
    assert frame_count / fps == pytest.approx(2.0, abs=0.05)

    red = _mean_bgr(_frame_at(output, 0.1))
    blue_early = _mean_bgr(_frame_at(output, 0.6))
    blue_tail = _mean_bgr(_frame_at(output, 1.6))
    assert red[2] > 150 and red[0] < 60  # red frame, not stretched into tail
    assert blue_early[0] > 150 and blue_early[2] < 60
    assert blue_tail[0] > 150 and blue_tail[2] < 60  # cloned tail stays blue


@pytest.mark.asyncio
async def test_finalize_normalizes_mixed_resolution_sources(tmp_path):
    portrait = _make_vfr_mov(tmp_path / "portrait.mov", width=240, height=320)
    landscape = _make_vfr_mov(tmp_path / "landscape.mov", width=320, height=240)

    out_p = tmp_path / "portrait.mp4"
    out_l = tmp_path / "landscape.mp4"
    assert await finalize_mov_to_mp4(portrait, out_p, 240, 320, 1.0)
    assert await finalize_mov_to_mp4(landscape, out_l, 240, 320, 1.0)

    for produced in (out_p, out_l):
        capture = cv2.VideoCapture(str(produced))
        width = capture.get(cv2.CAP_PROP_FRAME_WIDTH)
        height = capture.get(cv2.CAP_PROP_FRAME_HEIGHT)
        ok, _frame = capture.read()
        capture.release()
        assert ok and (width, height) == (240, 320)


@pytest.mark.asyncio
async def test_probe_falls_back_to_cv2_only_when_ffprobe_missing(monkeypatch, tmp_path):
    from artemis.utils import video as video_utils

    mp4 = tmp_path / "segment.mp4"
    assert await finalize_mov_to_mp4(_make_vfr_mov(tmp_path / "src.mov"), mp4, 320, 240, 1.0)
    monkeypatch.setattr(video_utils, "get_ffprobe_path", lambda: str(tmp_path / "missing-ffprobe"))

    metadata = await video_utils.probe_video_segment(mp4)
    assert metadata["width"] == 320 and metadata["height"] == 240
    assert metadata["duration"] == pytest.approx(1.0, abs=0.05)

    corrupted = tmp_path / "corrupt.mp4"
    corrupted.write_bytes(b"not a movie")
    assert await video_utils.probe_video_segment(corrupted) == {}


# Mirrors real ``simctl io <udid> enumerate`` output on an iPhone simulator:
# the LCD is the only display with an IOSurface port; external scene displays
# (CarPlay wireless, resizable) report Default dims only and must be ignored.
ENUMERATE_SAMPLE = """\
Port:
    UUID: 0E2F25F7-2FF1-44B7-93A2-36AE37042D2D
    Class: Unknown
    Port Identifier: com.apple.display.captureservice
    Power state: On

Port:
    UUID: 99261FD3-48E9-4CB4-B58D-5D2AF9CB5538
    Class: Display
    Port Identifier: com.apple.framebuffer.display
    Power state: On
    Display class: 1
    Default width: 720
    Default height: 480
    Default pixel format: 'BGRA'

Port:
    UUID: 9A996636-389D-48C9-86D4-D2A252086D44
    Class: Display
    Port Identifier: com.apple.framebuffer.display
    Power state: On
    Display class: 0
    Default width: 1206
    Default height: 2622
    Default pixel format: 'BGRA'
    IOSurface port:
        width              = 1206
        height             = 2622
        bytes per row      = 4864
        size               = 12763136

Port:
    UUID: E7A14973-04D1-456A-858D-BD0EEF2FF5D5
    Class: Display
    Port Identifier: com.apple.framebuffer.display
    Power state: On
    Display class: 1
    Default width: 7680
    Default height: 4320
    Default pixel format: 'BGRA'
"""


def test_parse_display_dimensions_uses_iosurface_not_default_dims():
    assert rec._parse_display_dimensions(ENUMERATE_SAMPLE) == (1206, 2622)
    rotated = ENUMERATE_SAMPLE.replace(
        "width              = 1206", "width              = 2622"
    ).replace("height             = 2622", "height             = 1206")
    assert rec._parse_display_dimensions(rotated) == (2622, 1206)
    assert rec._parse_display_dimensions("Port:\n    Class: Unknown\n") is None
    assert rec._parse_display_dimensions("") is None


@pytest.mark.asyncio
async def test_young_crash_loop_caps_respawns_and_stops(recorder_env, monkeypatch, tmp_path):
    """Spawns that succeed then die young must bound the crash loop.

    Every fake proc delivers the 'Recording started' marker (spawn succeeds)
    and then exits immediately — the watchdog's young-crash counter must stop
    the respawn loop at MAX_CONSECUTIVE_FAILURES instead of spawning forever.
    """
    created: list[FakeRecorderProcess] = []

    async def spawn(*argv, **kwargs):
        proc = FakeRecorderProcess()
        created.append(proc)

        def die_young():
            if proc.returncode is None:
                proc.returncode = 1
                proc._exit.set()

        asyncio.get_running_loop().call_later(0.01, die_young)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    recorder = IosScreenRecorder(UDID)

    session = await recorder.start(output_dir=tmp_path)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and session.is_active:
        await asyncio.sleep(0.02)

    assert not session.is_active
    assert len(created) <= rec.MAX_CONSECUTIVE_FAILURES + 1
    assert any("recovery limit" in error for error in session.errors)
    # No orphan process survives the cap.
    assert all(proc.returncode is not None for proc in created)


@pytest.mark.asyncio
async def test_finalize_cancellation_reaps_child_and_drops_part(tmp_path, monkeypatch):
    """Cancelling finalize must reap the ffmpeg child and remove its .part."""
    source = tmp_path / "segment_0000.mov"
    source.write_bytes(b"mov")
    entered = asyncio.Event()
    release = asyncio.Event()
    reaped: list[object] = []

    class _Proc:
        returncode = None

        async def communicate(self):
            entered.set()
            await release.wait()
            return b"", b""

    async def fake_exec(*args, **kwargs):
        return _Proc()

    async def fake_reap(process):
        reaped.append(process)

    monkeypatch.setattr(rec.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(rec, "reap_process", fake_reap)
    output = tmp_path / "segment_0000.mp4"
    task = asyncio.create_task(finalize_mov_to_mp4(source, output, 100, 200, 1.0))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(reaped) == 1
    assert not output.exists()
    assert not (tmp_path / "segment_0000.part.mp4").exists()
