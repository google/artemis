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

"""get_step_screenshot: pre / post / overlay, missing files, invalid variants."""

import base64
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image

from artemis.tools.history import get_step_screenshot, load_step_screenshot


def _jpeg_file(tmp_path, name="pre.jpg", color="white"):
    path = tmp_path / name
    Image.new("RGB", (200, 400), color).save(path, format="JPEG")
    return path


def _reader(pre=None, post=None, action=None, record=True):
    reader = MagicMock()
    reader.get_step_image_path.side_effect = lambda n, which="pre": pre if which == "pre" else post
    reader.get_step_record.return_value = SimpleNamespace(action_taken=action) if record else None
    return reader


def test_pre_and_post_screenshots_are_attached_as_content_blocks(tmp_path):
    pre = _jpeg_file(tmp_path, "pre.jpg", "white")
    post = _jpeg_file(tmp_path, "post.jpg", "black")
    reader = _reader(pre=pre, post=post)

    shot = load_step_screenshot(reader, 2, "pre")
    reader.get_step_image_path.assert_called_with(2, "pre")
    assert shot.image_bytes == pre.read_bytes()
    assert shot.description == "Screenshot of step 2 (pre-action) is attached."
    blocks = shot.to_content_blocks()
    assert blocks[0] == {"type": "text", "text": shot.description}
    assert blocks[1]["type"] == "image_url"
    url = blocks[1]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == pre.read_bytes()

    shot = load_step_screenshot(reader, 2, "post")
    reader.get_step_image_path.assert_called_with(2, "post")
    assert shot.image_bytes == post.read_bytes()
    assert "post-action" in shot.description


def test_overlay_draws_the_recorded_raw_action(tmp_path):
    pre = _jpeg_file(tmp_path)
    reader = _reader(pre=pre, action={"action": "click", "target": [100, 200]})

    shot = load_step_screenshot(reader, 2, "overlay")
    reader.get_step_image_path.assert_called_once_with(2, "pre")
    assert shot.overlay_drawn is True
    assert "action drawn on it" in shot.description
    assert shot.image_bytes != pre.read_bytes()


def test_overlay_falls_back_to_plain_pre_without_action(tmp_path):
    pre = _jpeg_file(tmp_path)
    shot = load_step_screenshot(_reader(pre=pre, action=None), 2, "overlay")
    assert shot.overlay_drawn is False
    assert shot.image_bytes == pre.read_bytes()
    assert "plain screenshot" in shot.description


def test_missing_file_and_missing_step_return_plain_text():
    shot = load_step_screenshot(_reader(pre=None, post=None), 3, "post")
    assert shot.image_bytes is None
    assert shot.to_content_blocks() == "No post-action screenshot recorded for step 3."

    shot = load_step_screenshot(_reader(pre=None, record=False), 9, "pre")
    assert shot.to_content_blocks() == "Error: step 9 not found."


def test_invalid_variant_and_step_number_are_rejected():
    shot = load_step_screenshot(_reader(), 1, "bogus")
    assert shot.image_bytes is None
    assert "'pre', 'post' or 'overlay'" in shot.description
    assert "must be an integer" in load_step_screenshot(_reader(), "one").description
    assert "no execution history" in load_step_screenshot(None, 1).description


@pytest.mark.asyncio
async def test_tool_execute_returns_blocks_or_text(tmp_path):
    pre = _jpeg_file(tmp_path)
    ctx = SimpleNamespace(data_engine=_reader(pre=pre))
    out = await get_step_screenshot.execute(ctx=ctx, step_number=1, which="pre")
    assert isinstance(out, list) and out[1]["type"] == "image_url"

    out = await get_step_screenshot.execute(ctx=ctx, step_number=1, which="post")
    assert out == "No post-action screenshot recorded for step 1."

    out = await get_step_screenshot.execute(ctx=SimpleNamespace(data_engine=None), step_number=1)
    assert "no execution history" in out
