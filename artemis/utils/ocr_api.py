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

"""Local OCR for screen text detection.

Recognition runs on RapidOCR through onnxruntime. The models ship inside the
wheel, so text detection needs no credential and no network call.
"""

import asyncio
import base64
import threading
from typing import Any

from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

_ENGINE: Any = None
_ENGINE_LOCK = threading.Lock()


def _get_engine() -> Any:
    """Return the process-wide recognizer, loading its models on first use."""
    global _ENGINE
    if _ENGINE is None:
        with _ENGINE_LOCK:
            if _ENGINE is None:
                from rapidocr import RapidOCR

                _ENGINE = RapidOCR()
    return _ENGINE


def is_ocr_configured() -> bool:
    """Whether local OCR is importable in this installation."""
    try:
        import rapidocr  # noqa: F401
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.debug(f"Local OCR is unavailable: {exc}")
        return False
    return True


def _recognize(screenshot_b64: str) -> list[dict[str, Any]]:
    """Run recognition in this thread and shape the result for the callers."""
    engine = _get_engine()
    result = engine(base64.b64decode(screenshot_b64))
    texts = getattr(result, "txts", None)
    boxes = getattr(result, "boxes", None)
    if not texts or boxes is None:
        return []
    return [
        {
            "text": str(text),
            "position": [{"x": float(point[0]), "y": float(point[1])} for point in box],
        }
        for text, box in zip(texts, boxes)
        if text
    ]


async def perform_ocr(
    screenshot_b64: str,
    client: Any = None,
) -> list[dict[str, Any]]:
    """Recognize on-screen text in a base64 encoded screenshot.

    Args:
        screenshot_b64: Base64 encoded screenshot image.
        client: Retained for backward compatibility; local OCR needs no client.

    Returns:
        One entry per detected text line: ``{"text": ..., "position": [...]}``
        with four pixel corners taken from the image as sent. Empty when the
        image carries no text or recognition fails, so callers degrade to the
        UI hierarchy.
    """
    if not screenshot_b64:
        return []
    try:
        return await asyncio.to_thread(_recognize, screenshot_b64)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning(f"Local OCR failed; continuing without text: {exc}")
        return []
