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

"""Perceptual-hash stability and distance behavior (M4 similarity hint)."""

import io

from PIL import Image, ImageDraw

from artemis.utils.image_hash import HASH_SIZE, dhash_hex, hamming_distance_hex


def _screen(draw_fn, size=(270, 600), quality=85) -> bytes:
    img = Image.new("RGB", size, "white")
    draw_fn(ImageDraw.Draw(img))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def _login_screen(d):
    d.rectangle([20, 40, 250, 90], fill="#3355ff")
    d.rectangle([30, 200, 240, 250], outline="#333333", width=4)
    d.rectangle([30, 300, 240, 350], outline="#333333", width=4)
    d.rectangle([60, 450, 210, 510], fill="#22aa66")


def _settings_screen(d):
    for y in range(60, 560, 80):
        d.rectangle([10, y, 260, y + 50], fill="#dddddd")
    d.ellipse([200, 20, 250, 45], fill="#aa2222")


def test_dhash_is_stable_for_identical_bytes():
    img = _screen(_login_screen)
    assert dhash_hex(img) == dhash_hex(img)
    assert len(dhash_hex(img)) == HASH_SIZE * HASH_SIZE // 4


def test_dhash_survives_jpeg_reencoding_with_small_distance():
    original = _screen(_login_screen, quality=95)
    with Image.open(io.BytesIO(original)) as img:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=60)
    reencoded = buf.getvalue()

    distance = hamming_distance_hex(dhash_hex(original), dhash_hex(reencoded))
    assert distance is not None and distance <= 4


def test_dhash_separates_different_screens():
    distance = hamming_distance_hex(
        dhash_hex(_screen(_login_screen)), dhash_hex(_screen(_settings_screen))
    )
    assert distance is not None and distance > 8


def test_dhash_best_effort_on_garbage():
    assert dhash_hex(None) is None
    assert dhash_hex(b"") is None
    assert dhash_hex(b"not an image at all") is None


def test_hamming_distance_edge_cases():
    assert hamming_distance_hex(None, "ff") is None
    assert hamming_distance_hex("ff", None) is None
    assert hamming_distance_hex("ff", "ffff") is None  # length mismatch
    assert hamming_distance_hex("zz", "aa") is None  # not hex
    assert hamming_distance_hex("00ff", "00ff") == 0
    assert hamming_distance_hex("0000", "000f") == 4
