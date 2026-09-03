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

"""Recording playlist manifest: session-relative segment offsets."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from artemis.utils.video import RECORDING_MANIFEST_VERSION, write_recording_manifest


def _probe(durations: dict[str, float]):
    async def probe(path):
        return {"duration": durations[path.name], "width": 1080, "height": 1920}

    return AsyncMock(side_effect=probe)


@pytest.mark.asyncio
async def test_manifest_carries_session_offsets_and_restart_gaps(tmp_path):
    seg0 = tmp_path / "recording.mp4"
    seg1 = tmp_path / "recording_001.mp4"
    seg2 = tmp_path / "recording_002.mp4"
    for path in (seg0, seg1, seg2):
        path.write_bytes(b"mp4")

    # Recording started 1.2s after the session; scrcpy restart gaps of 1.5s and 0.8s.
    offsets = {seg0: 1.2, seg1: 12.7, seg2: 18.5}

    with patch(
        "artemis.utils.video.probe_video_segment",
        _probe({seg0.name: 10.0, seg1.name: 5.0, seg2.name: 4.0}),
    ):
        manifest_path = await write_recording_manifest(tmp_path, [seg0, seg1, seg2], offsets)

    assert manifest_path == tmp_path / "recording.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["version"] == RECORDING_MANIFEST_VERSION
    # Legacy back-to-back timeline is preserved for the existing player.
    assert payload["duration"] == 19.0
    assert [s["start"] for s in payload["segments"]] == [0.0, 10.0, 15.0]
    assert [s["duration"] for s in payload["segments"]] == [10.0, 5.0, 4.0]
    # Real session-relative timeline, including the gaps between segments.
    assert [s["offset_ms"] for s in payload["segments"]] == [1200, 12700, 18500]
    assert [s["duration_ms"] for s in payload["segments"]] == [10000, 5000, 4000]
    assert payload["session_offset_ms"] == 1200
    assert payload["session_end_ms"] == 22500
    assert [s["file"] for s in payload["segments"]] == [seg0.name, seg1.name, seg2.name]
    assert all(s["width"] == 1080 and s["height"] == 1920 for s in payload["segments"])
    assert not (tmp_path / "recording.part.json").exists()


@pytest.mark.asyncio
async def test_manifest_without_offsets_falls_back_to_back_to_back(tmp_path):
    seg0 = tmp_path / "recording.mp4"
    seg1 = tmp_path / "recording_001.mp4"
    seg0.write_bytes(b"mp4")
    seg1.write_bytes(b"mp4")

    with patch(
        "artemis.utils.video.probe_video_segment",
        _probe({seg0.name: 3.25, seg1.name: 2.0}),
    ):
        manifest_path = await write_recording_manifest(tmp_path, [seg0, seg1])

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert [s["offset_ms"] for s in payload["segments"]] == [0, 3250]
    assert [s["duration_ms"] for s in payload["segments"]] == [3250, 2000]
    assert payload["session_offset_ms"] == 0
    assert payload["session_end_ms"] == 5250


@pytest.mark.asyncio
async def test_manifest_fills_missing_offset_after_known_segment(tmp_path):
    seg0 = tmp_path / "recording.mp4"
    seg1 = tmp_path / "recording_001.mp4"
    seg0.write_bytes(b"mp4")
    seg1.write_bytes(b"mp4")

    with patch(
        "artemis.utils.video.probe_video_segment",
        _probe({seg0.name: 10.0, seg1.name: 5.0}),
    ):
        manifest_path = await write_recording_manifest(tmp_path, [seg0, seg1], {seg0: 2.0})

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    # Unknown offsets continue right after the previous segment (no gap assumed).
    assert [s["offset_ms"] for s in payload["segments"]] == [2000, 12000]


@pytest.mark.asyncio
async def test_manifest_skips_invalid_segments_and_clamps_negative_offsets(tmp_path):
    good = tmp_path / "recording.mp4"
    broken = tmp_path / "recording_001.mp4"
    missing = tmp_path / "recording_002.mp4"
    good.write_bytes(b"mp4")
    broken.write_bytes(b"mp4")

    with patch(
        "artemis.utils.video.probe_video_segment",
        _probe({good.name: 4.0, broken.name: 0.0}),
    ):
        manifest_path = await write_recording_manifest(
            tmp_path, [good, broken, missing], {good: -0.4}
        )

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert [s["file"] for s in payload["segments"]] == [good.name]
    assert payload["segments"][0]["offset_ms"] == 0


@pytest.mark.asyncio
async def test_manifest_returns_none_without_valid_segments(tmp_path):
    with patch(
        "artemis.utils.video.probe_video_segment",
        AsyncMock(return_value={"duration": 0, "width": 0, "height": 0}),
    ):
        assert await write_recording_manifest(tmp_path, [tmp_path / "nope.mp4"]) is None
    assert not (tmp_path / "recording.json").exists()
