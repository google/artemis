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

import os
from typing import Any

import httpx
from artemis.config import settings
from artemis.utils.logger import get_logger

logger = get_logger(__name__)
_HTTP_CLIENT: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None or _HTTP_CLIENT.is_closed:
        _HTTP_CLIENT = httpx.AsyncClient(timeout=30.0)
    return _HTTP_CLIENT


async def perform_ocr(
    screenshot_b64: str, client: httpx.AsyncClient = None
) -> list[dict[str, Any]]:
    """Calls Google Vision API for OCR using a persistent client to reuse TLS handshakes.

    Args:
        screenshot_b64: Base64 encoded screenshot image.

    Returns:
        A list of dictionaries containing detected text and position vertices.
    """
    ocr_secret = settings.get_api_key("ocr")
    api_key = (
        (ocr_secret.get_secret_value() if ocr_secret else None)
        or os.environ.get("OCR_API_KEY")
        or os.environ.get("VISION_API_KEY")
        or os.environ.get("API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or "API_KEY"
    )

    url = f"https://vision.googleapis.com/v1/images:annotate?key={api_key}"
    headers = {"Content-Type": "application/json"}
    data = {
        "requests": [
            {
                "image": {"content": screenshot_b64},
                "features": [{"type": "TEXT_DETECTION"}],
            }
        ]
    }

    try:
        active_client = client if client is not None else get_http_client()
        response = await active_client.post(url, json=data, headers=headers, timeout=30.0)
        return _parse_ocr_response(response)
    except Exception as e:
        logger.error(f"Failed to call Vision API for OCR: {e}")
        raise e


def _parse_ocr_response(response: httpx.Response) -> list[dict[str, Any]]:
    if response.status_code == 200:
        res_json = response.json()
        responses = res_json.get("responses", [])
        if responses and "textAnnotations" in responses[0]:
            annotations = responses[0]["textAnnotations"]
            results = []
            # Skip the first annotation (index 0) as it is the full-screen combined text
            for ann in annotations[1:]:
                desc = ann.get("description", "")
                vertices = ann.get("boundingPoly", {}).get("vertices", [])
                results.append({"text": desc, "position": vertices})
            return results
        else:
            return []
    else:
        raise Exception(f"Vision API returned status {response.status_code}: {response.text}")
