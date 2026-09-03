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

"""Pure-PIL perceptual hashing (dHash) for screen-similarity hints (M4).

A 64-bit difference hash: grayscale, resize to 9x8, compare each pixel to its
right neighbor. Robust to JPEG re-encoding and mild rendering noise, cheap to
compute (one small resize), and comparable with an integer Hamming distance —
no model call, no historical image bytes needed at compare time.

Hashes are stored per step in ``extra_metadata`` at record time
(``pre_image_dhash`` / ``post_image_dhash``, 16 hex chars) and compared
against the current screenshot before prompt construction.
"""

import io

from artemis.utils.logger import get_logger

logger = get_logger(__name__)

#: Grid width of the dHash (the hash carries HASH_SIZE * HASH_SIZE bits).
HASH_SIZE = 8


def dhash_hex(image_bytes: bytes | None, hash_size: int = HASH_SIZE) -> str | None:
    """64-bit dHash of an encoded image, as a 16-char hex string.

    Best-effort: returns ``None`` on any failure (missing/corrupt bytes,
    Pillow unavailable) — perceptual hashing is an enhancement, never a
    dependency.
    """
    if not image_bytes:
        return None
    try:
        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as img:
            # JPEG fast-path: DCT-scaled draft decode keeps the sync cost of
            # hashing at record time to a few milliseconds. The draft is only
            # an intermediate resolution; the final resize below fixes the
            # grid, so hashes stay comparable across code paths.
            try:
                img.draft("L", (hash_size * 8, hash_size * 8))
            except Exception as exc:  # pylint: disable=broad-exception-caught
                # Draft decode is an optimization only; full decode below still works.
                logger.debug(f"dhash draft decode skipped: {exc}", exc_info=True)
            small = img.convert("L").resize((hash_size + 1, hash_size), Image.Resampling.LANCZOS)
            pixels = list(small.getdata())
        bits = 0
        for row in range(hash_size):
            for col in range(hash_size):
                left = pixels[row * (hash_size + 1) + col]
                right = pixels[row * (hash_size + 1) + col + 1]
                bits = (bits << 1) | (1 if left > right else 0)
        return f"{bits:0{hash_size * hash_size // 4}x}"
    except Exception as e:
        logger.debug(f"dhash_hex failed: {e}")
        return None


def hamming_distance_hex(hash_a: str | None, hash_b: str | None) -> int | None:
    """Bit-level Hamming distance between two hex hashes; None if incomparable."""
    if not hash_a or not hash_b or len(hash_a) != len(hash_b):
        return None
    try:
        return bin(int(hash_a, 16) ^ int(hash_b, 16)).count("1")
    except ValueError:
        return None
