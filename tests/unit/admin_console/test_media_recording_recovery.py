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

"""Recording files of cancelled / killed workers must survive and be served whole."""

import os
from pathlib import Path
import time
from unittest.mock import patch

import pytest

from apps.admin_console.services import media_service as media_module

MediaService = media_module.MediaService


@pytest.fixture(autouse=True)
def clean_cache():
    MediaService._playable_cache.clear()
    yield
    MediaService._playable_cache.clear()


def _fake_convert(src: Path, dst: Path) -> Path:
    dst.write_bytes(b"mp4:" + src.read_bytes())
    return dst


def _age(path: Path, seconds: float) -> None:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def test_live_recording_is_not_converted(tmp_path):
    mkv = tmp_path / "recording.mkv"
    mkv.write_bytes(b"x" * 10)  # fresh mtime: scrcpy is still writing

    with patch.object(MediaService, "_convert_to_mp4", side_effect=_fake_convert) as convert:
        assert MediaService.ensure_browser_playable_video(mkv) == mkv

    convert.assert_not_called()
    assert not (tmp_path / "recording.mp4").exists()


def test_stale_partial_mp4_is_reconverted_from_newer_mkv(tmp_path):
    mkv = tmp_path / "recording.mkv"
    mkv.write_bytes(b"full")
    partial = tmp_path / "recording.mp4"
    partial.write_bytes(b"partial")
    _age(partial, 120)
    _age(mkv, 60)

    with patch.object(MediaService, "_convert_to_mp4", side_effect=_fake_convert) as convert:
        result = MediaService.ensure_browser_playable_video(mkv)

    convert.assert_called_once()
    assert result == partial
    assert partial.read_bytes() == b"mp4:full"


def test_finished_mp4_newer_than_mkv_is_reused(tmp_path):
    mkv = tmp_path / "recording.mkv"
    mkv.write_bytes(b"full")
    mp4 = tmp_path / "recording.mp4"
    mp4.write_bytes(b"final")
    _age(mkv, 120)
    _age(mp4, 60)

    with patch.object(MediaService, "_convert_to_mp4", side_effect=_fake_convert) as convert:
        result = MediaService.ensure_browser_playable_video(mkv)

    convert.assert_not_called()
    assert result == mp4


def test_build_video_index_skips_live_recordings(tmp_path, monkeypatch):
    traces = tmp_path / "traces"
    live = traces / "web_1_live"
    live.mkdir(parents=True)
    (live / "recording.mkv").write_bytes(b"live")
    done = traces / "web_2_done_PASS_2026-09-02T12-00-00"
    done.mkdir()
    (done / "recording.mp4").write_bytes(b"done")
    monkeypatch.setattr(media_module, "TRACES_PATH", traces)
    monkeypatch.setattr(media_module, "WORKSPACE_ROOT", tmp_path)

    with patch.object(MediaService, "_convert_to_mp4", side_effect=_fake_convert) as convert:
        idx = MediaService.build_video_index()

    convert.assert_not_called()
    assert "web_1_live" not in idx
    assert idx["web_2_done"] == idx["web_2_done_PASS_2026-09-02T12-00-00"]
    assert idx["web_2_done"].endswith("recording.mp4")


def test_strip_trace_status_suffix_handles_every_terminal_marker():
    assert MediaService.strip_trace_status_suffix("web_1_PASS_2026") == "web_1"
    assert MediaService.strip_trace_status_suffix("web_1_FAIL_2026") == "web_1"
    assert MediaService.strip_trace_status_suffix("web_1_TESTFAIL_2026") == "web_1"
    assert MediaService.strip_trace_status_suffix("web_1") == "web_1"


def test_recover_orphaned_recording_remuxes_and_renames(tmp_path, monkeypatch):
    traces = tmp_path / "traces"
    folder = traces / "web_1788391889_5d5287b9"
    folder.mkdir(parents=True)
    mkv = folder / "recording.mkv"
    mkv.write_bytes(b"full")
    partial = folder / "recording.mp4"
    partial.write_bytes(b"partial")
    _age(partial, 120)
    _age(mkv, 60)
    monkeypatch.setattr(media_module, "TRACES_PATH", traces)
    start = time.mktime(time.strptime("2026-09-02 16:31:32", "%Y-%m-%d %H:%M:%S"))

    with patch.object(MediaService, "_convert_to_mp4", side_effect=_fake_convert):
        final = MediaService.recover_orphaned_recording(str(mkv), start)

    assert final is not None
    assert final.parent.name == "web_1788391889_5d5287b9_FAIL_2026-09-02T16-31-32"
    assert final.name == "recording.mp4"
    assert final.read_bytes() == b"mp4:full"
    assert not folder.exists()
    assert not (final.parent / "recording.mkv").exists()


def test_recover_orphaned_recording_leaves_live_file_alone(tmp_path, monkeypatch):
    traces = tmp_path / "traces"
    folder = traces / "web_live"
    folder.mkdir(parents=True)
    mkv = folder / "recording.mkv"
    mkv.write_bytes(b"live")
    monkeypatch.setattr(media_module, "TRACES_PATH", traces)

    with patch.object(MediaService, "_convert_to_mp4", side_effect=_fake_convert) as convert:
        assert MediaService.recover_orphaned_recording(str(mkv), None) is None

    convert.assert_not_called()
    assert folder.exists()


def test_recover_orphaned_recording_keeps_raw_when_remux_fails(tmp_path, monkeypatch):
    traces = tmp_path / "traces"
    folder = traces / "web_broken"
    folder.mkdir(parents=True)
    mkv = folder / "recording.mkv"
    mkv.write_bytes(b"corrupt")
    _age(mkv, 60)
    monkeypatch.setattr(media_module, "TRACES_PATH", traces)

    with patch.object(MediaService, "_convert_to_mp4", side_effect=lambda src, dst: src):
        assert MediaService.recover_orphaned_recording(str(mkv), None) is None

    assert mkv.exists()
    assert folder.exists()


def test_recover_orphaned_recording_returns_none_without_files(tmp_path):
    missing = tmp_path / "missing" / "recording.mkv"
    assert MediaService.recover_orphaned_recording(str(missing)) is None
    assert MediaService.recover_orphaned_recording(None) is None
