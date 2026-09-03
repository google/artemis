"""Regression tests for Explorer perception-tool failure paths."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from artemis.agents.explorer.perception_tools import PerceptionToolsMixin
from artemis.agents.explorer.screen_index import ScreenIndex


def _perception_host() -> PerceptionToolsMixin:
    host = PerceptionToolsMixin()
    host.ctx = MagicMock()
    host.global_label_idx = 1
    host.width = 1080
    host.height = 2400
    host.image_name = "screen-1"
    host.screenshot_path = "screen-1.jpg"
    host.image_pool = {
        "img_0": {
            "path": "screen-1.jpg",
            "transform": {
                "offset_x": 0.0,
                "offset_y": 0.0,
                "scale_x": 1.0,
                "scale_y": 1.0,
            },
        }
    }
    host.next_img_id = 1
    host.screen_index = ScreenIndex.empty(1080, 2400)
    host.label_registry = {}
    return host


@pytest.mark.asyncio
async def test_get_ocr_list_reads_the_screen_index_without_storage() -> None:
    host = _perception_host()

    with patch("artemis.agents.explorer.explorer.StorageManager") as storage_cls:
        result = await host.exec_get_ocr_list()

    assert result == {
        "text": "No text elements detected on the screen.",
        "image_path": None,
    }
    storage_cls.assert_not_called()


@pytest.mark.asyncio
async def test_image_processor_empty_outputs_returns_stable_error() -> None:
    host = _perception_host()
    processor = MagicMock()
    processor.run = AsyncMock(return_value={"outputs": [], "summary": "nothing found"})

    with patch(
        "artemis.agents.explorer.perception_tools.ImageProcessor",
        return_value=processor,
    ):
        result = await host.exec_ask_image_processor("crop", target_image_id="img_0")

    assert result == {
        "text": "ImageProcessor error: no output images were produced.",
        "image_paths": [],
    }
