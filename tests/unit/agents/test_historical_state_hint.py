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

"""HistoricalStateHintPromptComponent (M4): perceptual-hash hint + silence rules.

Division of labor under test: the hint fires only for matches *older* than the
recent window (returned-to-an-early-state regime); any match within the last 3
steps silences it entirely — that regime belongs to the pixel-level
ScreenshotSimilarityPromptComponent.
"""

import base64
import io
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image, ImageDraw

from artemis.agents.operator.prompts import HistoricalStateHintPromptComponent, PromptBuilder
from artemis.utils.image_hash import dhash_hex


def _jpeg(draw_fn) -> bytes:
    img = Image.new("RGB", (270, 600), "white")
    draw_fn(ImageDraw.Draw(img))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _screen_a(d):
    d.rectangle([20, 40, 250, 90], fill="#3355ff")
    d.rectangle([60, 450, 210, 510], fill="#22aa66")


def _screen_b(d):
    for y in range(60, 560, 80):
        d.rectangle([10, y, 260, y + 50], fill="#dddddd")


SCREEN_A = _jpeg(_screen_a)
SCREEN_B = _jpeg(_screen_b)
HASH_A = dhash_hex(SCREEN_A)
HASH_B = dhash_hex(SCREEN_B)
B64_A = base64.b64encode(SCREEN_A).decode("utf-8")


def _step(number: int, post_hash: str | None) -> dict:
    meta = {"post_image_dhash": post_hash} if post_hash else {}
    return {"step_number": number, "extra_metadata": meta}


def _cfg(similarity_hint=True, max_distance=8):
    return SimpleNamespace(
        memory=SimpleNamespace(
            transcript=SimpleNamespace(
                similarity_hint=similarity_hint,
                similarity_max_distance=max_distance,
            )
        )
    )


async def _run(steps, b64=B64_A, cfg=None):
    builder = PromptBuilder()
    component = HistoricalStateHintPromptComponent()
    with patch("artemis.config.load_agent_config", return_value=cfg or _cfg()):
        await component(
            builder, MagicMock(), MagicMock(), latest_screenshot_b64=b64, steps=steps
        )
    return [p for p in builder.human_parts if isinstance(p, str)]


@pytest.mark.asyncio
async def test_hint_fires_on_old_matching_step():
    steps = [
        _step(1, HASH_B),
        _step(2, HASH_A),  # old match
        _step(3, HASH_B),
        _step(4, HASH_B),
        _step(5, HASH_B),
        _step(6, HASH_B),
    ]
    parts = await _run(steps)
    assert len(parts) == 1
    assert "Historical state hint" in parts[0]
    assert "Step 2" in parts[0]
    assert "recall_history" in parts[0]


@pytest.mark.asyncio
async def test_silent_when_recent_step_also_matches():
    steps = [
        _step(1, HASH_A),  # old match exists...
        _step(2, HASH_B),
        _step(3, HASH_B),
        _step(4, HASH_B),
        _step(5, HASH_A),  # ...but a recent (last-3) match silences the hint
        _step(6, HASH_B),
    ]
    assert await _run(steps) == []


@pytest.mark.asyncio
async def test_silent_without_any_match():
    steps = [_step(n, HASH_B) for n in range(1, 7)]
    assert await _run(steps) == []


@pytest.mark.asyncio
async def test_silent_when_config_disabled():
    steps = [
        _step(1, HASH_A),
        _step(2, HASH_B),
        _step(3, HASH_B),
        _step(4, HASH_B),
        _step(5, HASH_B),
        _step(6, HASH_B),
    ]
    assert await _run(steps, cfg=_cfg(similarity_hint=False)) == []


@pytest.mark.asyncio
async def test_silent_with_too_few_steps():
    # Everything within the recent window belongs to the pixel-level note.
    assert await _run([_step(1, HASH_A), _step(2, HASH_A)]) == []


@pytest.mark.asyncio
async def test_tie_prefers_most_recent_old_step():
    steps = [
        _step(1, HASH_A),
        _step(2, HASH_A),  # same distance, more recent
        _step(3, HASH_B),
        _step(4, HASH_B),
        _step(5, HASH_B),
        _step(6, HASH_B),
    ]
    parts = await _run(steps)
    assert len(parts) == 1 and "Step 2" in parts[0]


@pytest.mark.asyncio
async def test_steps_without_hashes_are_skipped():
    steps = [
        _step(1, None),
        _step(2, None),
        _step(3, None),
        _step(4, None),
        _step(5, None),
        _step(6, None),
    ]
    assert await _run(steps) == []


def test_record_step_stamps_perceptual_hashes(tmp_path):
    from artemis.context import ArtemisContext
    from artemis.data_engine.engine import DataEngine

    mock_ctx = MagicMock(spec=ArtemisContext)
    setup = MagicMock()
    setup.traces_path = str(tmp_path)
    mock_ctx.execution_setup = setup
    mock_ctx.device = None
    engine = DataEngine(mock_ctx)
    engine.start_session("dhash stamping test")

    engine.record_step(
        pre_screenshot_bytes=SCREEN_A,
        post_screenshot_bytes=SCREEN_B,
        summary="two distinct screens",
    )
    # Same pre/post bytes: post_image_name is nulled but the post-action
    # screen IS the pre screen, so the post hash mirrors the pre hash.
    engine.record_step(
        pre_screenshot_bytes=SCREEN_A,
        post_screenshot_bytes=SCREEN_A,
        summary="screen unchanged",
    )
    for t in list(engine._pending_threads):
        t.join()

    steps = engine.storage.get_steps(engine.current_session_id)
    meta1 = steps[0].extra_metadata
    assert meta1["pre_image_dhash"] == HASH_A
    assert meta1["post_image_dhash"] == HASH_B
    meta2 = steps[1].extra_metadata
    assert meta2["pre_image_dhash"] == HASH_A
    assert meta2["post_image_dhash"] == HASH_A
