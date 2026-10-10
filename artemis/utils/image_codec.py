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

"""Base64 image data-URI helpers.

OpenAI-compatible servers differ in how strictly they treat the declared
MIME type: some (e.g. strict local gateways) validate it against the actual
payload and reject a PNG byte stream labeled ``image/jpeg``. Always sniff the
magic bytes instead of assuming JPEG.
"""

import base64

_JPEG_SOI = b"\xff\xd8\xff"
_PNG_SIG = b"\x89PNG"
_WEBP_RIFF = b"RIFF"
_WEBP_TAG = b"WEBP"


def sniff_image_mime(image_bytes: bytes) -> str:
    """Return the ``image/*`` MIME type for encoded image bytes."""
    if image_bytes[:4] == _PNG_SIG:
        return "image/png"
    if image_bytes[:3] == _JPEG_SOI:
        return "image/jpeg"
    if image_bytes[:4] == _WEBP_RIFF and image_bytes[8:12] == _WEBP_TAG:
        return "image/webp"
    return "image/jpeg"


def image_data_uri(b64_data: str) -> str:
    """Wrap a base64 payload in a data URI with the MIME type sniffed from its
    decoded head. Unknown or corrupt prefixes keep the historic ``image/jpeg``
    label.
    """
    try:
        head = base64.b64decode(b64_data[:24])
        mime = sniff_image_mime(head)
    except Exception:
        mime = "image/jpeg"
    return f"data:{mime};base64,{b64_data}"
