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

"""Tests for screenshot media-type detection and data-URI construction."""

import base64

from artemis.utils.image_mime import image_data_uri, image_mime_type

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 20


def test_png_bytes_detected():
    assert image_mime_type(PNG_BYTES) == "image/png"


def test_jpeg_bytes_detected():
    assert image_mime_type(JPEG_BYTES) == "image/jpeg"


def test_unknown_bytes_default_to_png():
    # Screenshot paths emit PNG; an unrecognized header keeps the honest
    # project default rather than the historical hardcoded jpeg.
    assert image_mime_type(b"\x00\x01\x02\x03") == "image/png"


def test_base64_string_detected():
    b64 = base64.b64encode(JPEG_BYTES).decode()
    assert image_mime_type(b64) == "image/jpeg"
    b64 = base64.b64encode(PNG_BYTES).decode()
    assert image_mime_type(b64) == "image/png"


def test_data_uri_uses_detected_type():
    uri = image_data_uri(PNG_BYTES)
    b64 = base64.b64encode(PNG_BYTES).decode()
    assert uri == f"data:image/png;base64,{b64}"


def test_data_uri_for_jpeg_bytes():
    uri = image_data_uri(JPEG_BYTES)
    b64 = base64.b64encode(JPEG_BYTES).decode()
    assert uri == f"data:image/jpeg;base64,{b64}"


def test_data_uri_from_base64_input():
    b64 = base64.b64encode(PNG_BYTES).decode()
    assert image_data_uri(b64) == f"data:image/png;base64,{b64}"
