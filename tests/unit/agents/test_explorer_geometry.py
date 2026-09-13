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

"""Screen geometry shared by the Explorer and the ``ask_explorer`` tool."""

from types import SimpleNamespace

import pytest

from artemis.agents.explorer.geometry import (
    FALLBACK_SCREEN_SIZE,
    NORMALIZED_MAX,
    is_valid_norm_point,
    norm_to_pixel,
    pixel_to_norm,
    resolve_screen_size,
)


def _ctx(width=None, height=None, device=True):
    if not device:
        return SimpleNamespace(device=None)
    return SimpleNamespace(device=SimpleNamespace(device_width=width, device_height=height))


def _state(raw):
    return SimpleNamespace(operator_raw_data=raw)


# ---------------------------------------------------------------------------
# resolve_screen_size
# ---------------------------------------------------------------------------


def test_operator_observation_wins_over_the_device_context():
    ctx = _ctx(1080, 2400)
    state = _state({"width": 720, "height": 1600})
    assert resolve_screen_size(ctx, state) == (720, 1600)


def test_device_context_is_used_when_the_observation_has_no_size():
    ctx = _ctx(1080, 2400)
    assert resolve_screen_size(ctx, _state(None)) == (1080, 2400)
    assert resolve_screen_size(ctx, _state({})) == (1080, 2400)
    assert resolve_screen_size(ctx, _state("not a dict")) == (1080, 2400)


@pytest.mark.parametrize(
    "raw",
    [
        {"width": 0, "height": 1600},
        {"width": 720, "height": -1},
        {"width": "720", "height": 1600},
        {"width": 720.0, "height": 1600},
        {"width": None, "height": None},
        {"width": 720},
    ],
)
def test_non_int_or_non_positive_observation_sizes_are_ignored(raw):
    ctx = _ctx(1080, 2400)
    assert resolve_screen_size(ctx, _state(raw)) == (1080, 2400)


def test_fallback_when_neither_source_reports_a_size():
    assert resolve_screen_size(_ctx(device=False), _state(None)) == FALLBACK_SCREEN_SIZE
    assert resolve_screen_size(_ctx(None, None), _state(None)) == FALLBACK_SCREEN_SIZE
    assert resolve_screen_size(SimpleNamespace(), SimpleNamespace()) == FALLBACK_SCREEN_SIZE


def test_device_dimensions_fall_back_independently():
    fb_w, fb_h = FALLBACK_SCREEN_SIZE
    assert resolve_screen_size(_ctx(1440, 0), _state(None)) == (1440, fb_h)
    assert resolve_screen_size(_ctx("1440", 3200), _state(None)) == (fb_w, 3200)
    assert resolve_screen_size(_ctx(None, 3200), _state(None)) == (fb_w, 3200)


# ---------------------------------------------------------------------------
# norm_to_pixel / pixel_to_norm
# ---------------------------------------------------------------------------


def test_norm_to_pixel_scales_on_the_0_1000_grid():
    assert norm_to_pixel(500, 500, 1080, 2400) == (540, 1200)
    assert norm_to_pixel(0, 0, 1080, 2400) == (0, 0)
    assert norm_to_pixel(NORMALIZED_MAX, NORMALIZED_MAX, 1080, 2400) == (1080, 2400)
    assert norm_to_pixel(250, 750, 1080, 2400) == (270, 1800)


def test_norm_to_pixel_clamps_out_of_range_inputs():
    assert norm_to_pixel(-50, -1, 1080, 2400) == (0, 0)
    assert norm_to_pixel(1500, 2000, 1080, 2400) == (1080, 2400)


def test_pixel_to_norm_scales_back_to_the_0_1000_grid():
    assert pixel_to_norm(540, 1200, 1080, 2400) == (500, 500)
    assert pixel_to_norm(0, 0, 1080, 2400) == (0, 0)
    assert pixel_to_norm(1080, 2400, 1080, 2400) == (NORMALIZED_MAX, NORMALIZED_MAX)


def test_pixel_to_norm_clamps_out_of_range_inputs():
    assert pixel_to_norm(-10, -10, 1080, 2400) == (0, 0)
    assert pixel_to_norm(5000, 9000, 1080, 2400) == (NORMALIZED_MAX, NORMALIZED_MAX)


def test_conversions_round_trip_on_grid_points():
    for nx, ny in [(0, 0), (500, 500), (1000, 1000), (250, 750)]:
        px, py = norm_to_pixel(nx, ny, 1000, 2000)
        assert pixel_to_norm(px, py, 1000, 2000) == (nx, ny)


# ---------------------------------------------------------------------------
# is_valid_norm_point
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "coords",
    [[0, 0], [500, 500], (1000, 1000), ["500", "250"], [12.7, 999.2]],
)
def test_valid_norm_points(coords):
    assert is_valid_norm_point(coords)


@pytest.mark.parametrize(
    "coords",
    [
        None,
        "500,500",
        [500],
        [500, 500, 500],
        [-1, 0],
        [0, 1001],
        ["x", 0],
        [None, 5],
        {"x": 1, "y": 2},
    ],
)
def test_invalid_norm_points(coords):
    assert not is_valid_norm_point(coords)
