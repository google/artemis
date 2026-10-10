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

"""Standalone Apple Vision OCR worker — run as ``python apple_vision_ocr.py``.

Reads image bytes on stdin and writes a JSON list of
``{"text": str, "position": [{"x": int, "y": int}, ...]}`` on stdout,
matching the Google Vision ``boundingPoly.vertices`` contract.

This module is deliberately free of artemis imports and runs in a clean
interpreter: once cv2 (and its GPU/OpenCL contexts) is loaded in the host
process, ``VNRecognizeTextRequest`` fails intermittently with e5rt
compute-stream errors. A subprocess keeps the ML stack isolated.
"""

# pyright: reportAttributeAccessIssue=false
# pyobjc exposes AppKit/Vision classes dynamically; static stubs have no symbols.

import io
import json
import sys


def _vision_box_to_vertices(bounding_box, width: int, height: int) -> list[dict[str, int]]:
    """Convert a Vision normalized bounding box (origin bottom-left) to
    pixel-space vertices in Google's boundingPoly order: TL, TR, BR, BL."""
    x0 = bounding_box.origin.x * width
    x1 = (bounding_box.origin.x + bounding_box.size.width) * width
    y_top = (1.0 - bounding_box.origin.y - bounding_box.size.height) * height
    y_bottom = (1.0 - bounding_box.origin.y) * height
    return [
        {"x": round(x0), "y": round(y_top)},
        {"x": round(x1), "y": round(y_top)},
        {"x": round(x1), "y": round(y_bottom)},
        {"x": round(x0), "y": round(y_bottom)},
    ]


def ocr_image(image_bytes: bytes) -> list[dict]:
    """On-device text recognition via Apple's Vision framework."""
    import Vision
    from Foundation import NSData
    from PIL import Image

    with Image.open(io.BytesIO(image_bytes)) as img:
        width, height = img.width, img.height

    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(True)
    # The first entry in recognitionLanguages dominates per-request — en-US
    # first garbles CJK text. Per-observation language detection (revision 3)
    # avoids ordering games: every supported language is enabled and Vision
    # picks the right one for each text region.
    request.setAutomaticallyDetectsLanguage_(True)
    langs, _ = (
        Vision.VNRecognizeTextRequest.supportedRecognitionLanguagesForTextRecognitionLevel_revision_error_(
            Vision.VNRequestTextRecognitionLevelAccurate,
            request.revision(),
            None,
        )
    )
    if langs:
        request.setRecognitionLanguages_(list(langs))

    ns_data = NSData.dataWithBytes_length_(image_bytes, len(image_bytes))
    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(ns_data, None)
    ok, error = handler.performRequests_error_([request], None)
    if not ok:
        raise RuntimeError(f"VNRecognizeTextRequest failed: {error}")

    results = []
    for observation in request.results() or []:
        candidates = observation.topCandidates_(1)
        if not candidates:
            continue
        text = str(candidates[0].string()).strip()
        if not text:
            continue
        results.append(
            {
                "text": text,
                "position": _vision_box_to_vertices(observation.boundingBox(), width, height),
            }
        )
    return results


def main() -> None:
    image_bytes = sys.stdin.buffer.read()
    json.dump(ocr_image(image_bytes), sys.stdout)


if __name__ == "__main__":
    main()
