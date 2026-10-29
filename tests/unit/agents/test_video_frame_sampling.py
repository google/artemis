"""Video request bounds without a device, model call, or loss of source media."""

import pytest
from pydantic import ValidationError

from artemis.agents.video_analyzer.universal_engine import _select_video_frames
from artemis.config.agent import VideoAnalyzerConfig


@pytest.mark.parametrize("count", [0, 1, 2, 7, 8, 9, 51, 53, 54, 150])
def test_selection_is_bounded_and_preserves_original_order_timestamps_and_bytes(count):
    frames = [(index / 4, bytes([index])) for index in range(count)]
    before = frames.copy()
    selected = _select_video_frames(frames, 8, [1.1, 7.1, 11.1])
    assert frames == before
    assert len(selected) == min(count, 8)
    assert selected == sorted(selected)
    assert all(frame in frames for frame in selected)
    assert len({timestamp for timestamp, _ in selected}) == len(selected)
    if count:
        assert selected[0] is frames[0] and selected[-1] is frames[-1]
    if count <= 8:
        assert selected is frames


def test_recent_action_pairs_are_preserved_and_unrelated_ranges_do_not_consume_slots():
    frames = [(float(index), bytes([index])) for index in range(53)]
    actions = [-4.0, 25.1, 50.1, 90.0]
    selected = _select_video_frames(frames, 8, actions)
    assert {frames[index] for index in [0, 25, 26, 50, 51, 52]} <= set(selected)
    assert selected == _select_video_frames(frames, 8, list(reversed(actions)))
    assert selected == _select_video_frames(frames, 8, [25.1, 50.1])


def test_tight_limit_preserves_endpoints_and_prioritizes_most_recent_action_pair():
    frames = [(float(index), bytes([index])) for index in range(20)]
    assert _select_video_frames(frames, 2, [4.5, 15.5]) == [frames[0], frames[-1]]
    assert _select_video_frames(frames, 4, [4.5, 15.5]) == [
        frames[0],
        frames[15],
        frames[16],
        frames[-1],
    ]


def test_exact_action_timestamp_keeps_frames_on_both_sides():
    frames = [(float(index), bytes([index])) for index in range(12)]
    selected = _select_video_frames(frames, 4, [6.0])
    assert selected == [frames[0], frames[5], frames[7], frames[-1]]


@pytest.mark.parametrize("limit", [0, 1, 151, True, 8.5])
def test_total_frame_config_rejects_invalid_limits(limit):
    with pytest.raises(ValidationError):
        VideoAnalyzerConfig(max_total_frames=limit)


@pytest.mark.parametrize("limit", [None, 2, 8, 150])
def test_total_frame_config_is_optional_and_bounded(limit):
    assert VideoAnalyzerConfig(max_total_frames=limit).max_total_frames == limit
