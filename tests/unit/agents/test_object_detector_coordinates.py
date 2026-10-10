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

"""Coordinate normalization for multi-provider object detection.

The detector contract is ``[x, y]`` on a 0-1000 grid over the sent image.
Each endpoint declares the convention its model actually emits via
``coordinate_format``; clearly off-contract batches are rescaled.
"""

from types import SimpleNamespace

import pytest

from artemis.agents.object_detector.object_detector import (
    _detected_batch_scale,
    _endpoint_coordinate_format,
    _image_dimensions,
    _normalize_detected_box,
    _normalize_detected_point,
    _qwen_resized_dimensions,
)


def _item(a, b):
    return {"point": [a, b], "label": "x"}


# ---------------------------------------------------------------------------
# Declared formats
# ---------------------------------------------------------------------------


def test_yx_1000_swaps_axes_and_keeps_scale():
    item = _item(800, 200)
    _normalize_detected_point(item, "yx_1000", (1000, 2000), None)
    assert item["point"] == [200.0, 800.0]


def test_xy_1000_keeps_axes_and_scale():
    item = _item(200, 800)
    _normalize_detected_point(item, "xy_1000", (1000, 2000), None)
    assert item["point"] == [200.0, 800.0]


def test_xy_norm_scales_zero_to_one():
    item = _item(0.2, 0.8)
    _normalize_detected_point(item, "xy_norm", (1000, 2000), None)
    assert item["point"] == [200.0, 800.0]


def test_yx_norm_swaps_and_scales():
    item = _item(0.8, 0.2)
    _normalize_detected_point(item, "yx_norm", (1000, 2000), None)
    assert item["point"] == [200.0, 800.0]


def test_xy_px_scales_by_qwen_resize_grid():
    # 1000x2000 snaps to 992x1984 under the factor-32 smart resize.
    item = _item(496, 992)  # center of the resized grid
    _normalize_detected_point(item, "xy_px", (1000, 2000), None)
    assert item["point"] == [500.0, 500.0]


def test_yx_px_swaps_axes_and_scales():
    item = _item(992, 496)
    _normalize_detected_point(item, "yx_px", (1000, 2000), None)
    assert item["point"] == [500.0, 500.0]


def test_px_without_image_size_leaves_point_unchanged():
    item = _item(1500, 700)
    _normalize_detected_point(item, "xy_px", None, None)
    assert item["point"] == [1500.0, 700.0]


# ---------------------------------------------------------------------------
# Batch-level scale detection
# ---------------------------------------------------------------------------


def test_batch_scale_detects_norm_when_all_below_one():
    items = [_item(0.5, 0.2), _item(0.9, 0.1)]
    assert _detected_batch_scale(items) == "norm"


def test_batch_scale_detects_px_when_any_above_1000():
    items = [_item(500, 200), _item(1500, 300)]
    assert _detected_batch_scale(items) == "px"


def test_batch_scale_mixed_grid_stays_undeclared():
    # A legit 0-1000 corner point coexisting with mid-grid values must not
    # trip the norm heuristic — the batch still reads as the declared grid.
    items = [_item(0.5, 0.3), _item(500, 400)]
    assert _detected_batch_scale(items) is None


def test_batch_scale_all_zero_is_undeclared():
    assert _detected_batch_scale([_item(0, 0)]) is None


def test_batch_scale_scans_box_2d_coords():
    boxes = [
        {"box_2d": [0.1, 0.2, 0.3, 0.4], "label": "a"},
        {"box_2d": [0.5, 0.6, 0.7, 0.8], "label": "b"},
    ]
    assert _detected_batch_scale(boxes) == "norm"
    assert _detected_batch_scale([{"box_2d": [100, 200, 300, 1400]}]) == "px"


# ---------------------------------------------------------------------------
# box_2d detection output (Gemma 4 native [ymin, xmin, ymax, xmax] / 1000)
# ---------------------------------------------------------------------------


def test_box_2d_yx_1000_derives_center_point():
    # Gemma-style [ymin, xmin, ymax, xmax] -> contract [x1, y1, x2, y2].
    item = {"box_2d": [200, 100, 400, 300], "label": "icon"}
    assert _normalize_detected_box(item, "yx_1000", (1000, 2000), None) is True
    assert item["box_2d"] == [100.0, 200.0, 300.0, 400.0]
    assert item["point"] == [200.0, 300.0]


def test_box_2d_xy_1000_keeps_axes():
    item = {"box_2d": [100, 200, 300, 400]}
    assert _normalize_detected_box(item, "xy_1000", (1000, 2000), None) is True
    assert item["box_2d"] == [100.0, 200.0, 300.0, 400.0]
    assert item["point"] == [200.0, 300.0]


def test_box_2d_norm_scale_scales_and_swaps():
    item = {"box_2d": [0.2, 0.1, 0.4, 0.3]}
    assert _normalize_detected_box(item, "yx_norm", (1000, 2000), None) is True
    assert item["box_2d"] == [100.0, 200.0, 300.0, 400.0]
    assert item["point"] == [200.0, 300.0]


def test_box_2d_keeps_valid_point():
    item = {"point": [800, 200], "box_2d": [200, 100, 400, 300]}
    assert _normalize_detected_box(item, "yx_1000", (1000, 2000), None) is False
    _normalize_detected_point(item, "yx_1000", (1000, 2000), None)
    assert item["point"] == [200.0, 800.0]
    assert item["box_2d"] == [100.0, 200.0, 300.0, 400.0]


def test_box_2d_replaces_unusable_point():
    item = {"point": ["a", "b"], "box_2d": [200, 100, 400, 300]}
    assert _normalize_detected_box(item, "yx_1000", (1000, 2000), None) is True
    assert item["point"] == [200.0, 300.0]


def test_invalid_box_2d_is_untouched():
    for bad in ({"box_2d": [1, 2]}, {"box_2d": "x"}, {"box_2d": [1, 2, "c", 4]}):
        item = dict(bad)
        assert _normalize_detected_box(item, "yx_1000", (100, 100), None) is False
        assert item == bad


def test_batch_scale_override_fixes_norm_batch_under_1000_contract():
    item = _item(0.5, 0.2)
    _normalize_detected_point(item, "yx_1000", (1000, 2000), "norm")
    # yx order + norm scale: x=0.2*1000, y=0.5*1000
    assert item["point"] == [200.0, 500.0]


def test_batch_scale_override_fixes_px_batch_under_1000_contract():
    item = _item(992, 496)  # [y_px, x_px] on a 992x1984 grid
    _normalize_detected_point(item, "yx_1000", (1000, 2000), "px")
    assert item["point"] == [500.0, 500.0]


# ---------------------------------------------------------------------------
# Endpoint format resolution + image dims
# ---------------------------------------------------------------------------


def test_endpoint_coordinate_format_defaults_to_gemini():
    assert _endpoint_coordinate_format(SimpleNamespace(endpoint=None)) == "yx_1000"
    wrapper = SimpleNamespace(endpoint=SimpleNamespace(coordinate_format="xy_px"))
    assert _endpoint_coordinate_format(wrapper) == "xy_px"
    bogus = SimpleNamespace(endpoint=SimpleNamespace(coordinate_format="bogus"))
    assert _endpoint_coordinate_format(bogus) == "yx_1000"


def test_qwen_resize_snaps_to_factor_32():
    assert _qwen_resized_dimensions(1000, 2000) == (992, 1984)
    assert _qwen_resized_dimensions(2940, 1912) == (2944, 1920)


def test_image_dimensions_reads_png_header():
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (640, 480)).save(buf, format="PNG")
    assert _image_dimensions(buf.getvalue()) == (640, 480)
    assert _image_dimensions(b"not an image") is None


def test_invalid_points_are_untouched():
    for bad in ({}, {"point": "x"}, {"point": [1]}, {"point": ["a", "b"]}):
        item = dict(bad)
        _normalize_detected_point(item, "yx_1000", (100, 100), None)
        assert item == bad


@pytest.mark.asyncio
async def test_run_object_detection_uses_endpoint_coordinate_format():
    """End-to-end: an xy_px endpoint turns pixel output into the 0-1000 contract."""
    import io
    from unittest.mock import AsyncMock, patch

    from PIL import Image

    from artemis.agents.object_detector.object_detector import _run_object_detection

    buf = io.BytesIO()
    Image.new("RGB", (1000, 2000)).save(buf, format="PNG")
    image_bytes = buf.getvalue()

    fake_llm = SimpleNamespace(endpoint=SimpleNamespace(coordinate_format="xy_px"))
    # Model reports grid-center in pixels of the 992x1984 resized image.
    vlm_output = [{"point": [496, 992], "label": "home button"}]

    with (
        patch(
            "artemis.agents.object_detector.object_detector.get_llm",
            return_value=fake_llm,
        ),
        patch(
            "artemis.agents.object_detector.object_detector._detect_single_label",
            new_callable=AsyncMock,
            return_value=vlm_output,
        ),
    ):
        ctx = SimpleNamespace(llm_config=SimpleNamespace())
        result = await _run_object_detection(
            ctx=ctx,
            image_bytes=image_bytes,
            queries=["home button"],
        )

    point = result["detected"][0]["point"]
    # 496px/992w -> 500x, 992px/1984h -> 500y on the 0-1000 grid.
    assert point == [pytest.approx(500.0), pytest.approx(500.0)]


@pytest.mark.asyncio
async def test_run_object_detection_accepts_box_2d_output():
    """End-to-end: a box-only response (Gemma-style) still yields a contract point."""
    import io
    from unittest.mock import AsyncMock, patch

    from PIL import Image

    from artemis.agents.object_detector.object_detector import _run_object_detection

    buf = io.BytesIO()
    Image.new("RGB", (1000, 2000)).save(buf, format="PNG")
    image_bytes = buf.getvalue()

    fake_llm = SimpleNamespace(endpoint=SimpleNamespace(coordinate_format=None))
    vlm_output = [{"box_2d": [200, 100, 400, 300], "label": "home button"}]

    with (
        patch(
            "artemis.agents.object_detector.object_detector.get_llm",
            return_value=fake_llm,
        ),
        patch(
            "artemis.agents.object_detector.object_detector._detect_single_label",
            new_callable=AsyncMock,
            return_value=vlm_output,
        ),
    ):
        ctx = SimpleNamespace(llm_config=SimpleNamespace())
        result = await _run_object_detection(
            ctx=ctx,
            image_bytes=image_bytes,
            queries=["home button"],
        )

    detected = result["detected"][0]
    assert detected["point"] == [pytest.approx(200.0), pytest.approx(300.0)]
    assert detected["box_2d"] == [100.0, 200.0, 300.0, 400.0]
