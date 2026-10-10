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

import asyncio
import base64
import json
import os
from pathlib import Path
import sys
from typing import Any

import httpx
from artemis.config import settings
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)
_HTTP_CLIENT: httpx.AsyncClient | None = None

# Provider selection via ARTEMIS_OCR_PROVIDER: "auto" (default) prefers
# on-device Apple Vision on macOS and falls back to Google Cloud Vision when
# an API key exists; "apple" / "google" pin a single provider.
_OCR_PROVIDER_ENV = "ARTEMIS_OCR_PROVIDER"
_APPLE_VISION_SUPPORTED: bool | None = None
_APPLE_OCR_WORKER = Path(__file__).with_name("apple_vision_ocr.py")


def get_http_client() -> httpx.AsyncClient:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None or _HTTP_CLIENT.is_closed:
        _HTTP_CLIENT = httpx.AsyncClient(timeout=30.0)
    return _HTTP_CLIENT


def _ocr_provider() -> str:
    return os.environ.get(_OCR_PROVIDER_ENV, "auto").strip().lower()


def _apple_vision_supported() -> bool:
    """True on macOS when the pyobjc Vision bindings are importable and the
    standalone worker script exists. The real OCR runs in a subprocess: once
    cv2 is loaded in this process, VNRecognizeTextRequest intermittently
    fails compiling its ANE compute stream (e5rt errors)."""
    global _APPLE_VISION_SUPPORTED
    if _APPLE_VISION_SUPPORTED is None:
        if sys.platform != "darwin" or getattr(sys, "frozen", False):
            _APPLE_VISION_SUPPORTED = False
        else:
            try:
                import Vision  # noqa: F401

                _APPLE_VISION_SUPPORTED = _APPLE_OCR_WORKER.is_file()
            except ImportError:
                _APPLE_VISION_SUPPORTED = False
    return _APPLE_VISION_SUPPORTED


def _google_vision_key() -> str | None:
    """Raw Google Vision API key, or None (placeholders are not filtered)."""
    ocr_secret = settings.get_api_key("ocr")
    return (
        (ocr_secret.get_secret_value() if ocr_secret else None)
        or os.environ.get("OCR_API_KEY")
        or os.environ.get("VISION_API_KEY")
    )


def _google_vision_key_present() -> bool:
    """True when a non-placeholder Google Vision key is configured."""
    for val in (_google_vision_key(),):
        if (
            val
            and val.strip()
            and val.strip()
            not in (
                "API_KEY",
                "your_google_cloud_vision_api_key_here",
            )
        ):
            return True
    return False


def is_ocr_configured() -> bool:
    """True when some OCR provider is usable: Apple Vision on macOS, or a
    configured Google Cloud Vision API key."""
    provider = _ocr_provider()
    if provider == "apple":
        return _apple_vision_available()
    if provider == "google":
        return _google_vision_key_present()
    return _apple_vision_available() or _google_vision_key_present()


# A failing Vision attempt can stall for minutes inside the ObjC call (e5rt
# compute-stream compile), so every subprocess invocation is time-bounded and
# retried once in a fresh interpreter before degrading. When the OS-level
# Vision ML service is wedged, every call would burn 2x timeout forever —
# after consecutive failures the provider is disabled until a success resets
# the counter.
_APPLE_OCR_TIMEOUT_S = float(os.environ.get("APPLE_OCR_TIMEOUT", "20"))
_APPLE_OCR_MAX_CONSECUTIVE_FAILURES = 2
_apple_vision_consecutive_failures = 0


def _apple_vision_available() -> bool:
    return (
        _apple_vision_supported()
        and _apple_vision_consecutive_failures < _APPLE_OCR_MAX_CONSECUTIVE_FAILURES
    )


async def _apple_vision_subprocess(image_bytes: bytes) -> list[dict[str, Any]]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(_APPLE_OCR_WORKER),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(input=image_bytes), timeout=_APPLE_OCR_TIMEOUT_S
        )
    except BaseException:
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0:
        tail = err.decode(errors="replace")[-300:] if err else ""
        raise RuntimeError(f"Apple Vision worker exited {proc.returncode}: {tail}")
    return json.loads(out or b"[]")


async def _run_apple_vision(image_bytes: bytes) -> list[dict[str, Any]]:
    global _apple_vision_consecutive_failures
    try:
        results = await _apple_vision_subprocess(image_bytes)
    except Exception as e:
        logger.warning(f"Apple Vision OCR attempt failed ({e}); retrying once")
        try:
            results = await _apple_vision_subprocess(image_bytes)
        except Exception:
            _apple_vision_consecutive_failures += 1
            raise
    _apple_vision_consecutive_failures = 0
    return results


async def perform_ocr(
    screenshot_b64: str,
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    """Runs text recognition on an image with the selected provider.

    Provider order under "auto": Apple Vision on-device (macOS) first, then
    Google Cloud Vision when an API key is configured. An empty Apple result
    is authoritative — Google is only consulted when Apple Vision is
    unavailable or raises.

    Args:
        screenshot_b64: Base64 encoded screenshot image.
        client: Optional persistent httpx.AsyncClient.

    Returns:
        A list of dictionaries containing detected text and position vertices.
    """
    provider = _ocr_provider()
    if provider in ("auto", "apple"):
        if _apple_vision_available():
            try:
                return await _run_apple_vision(base64.b64decode(screenshot_b64))
            except Exception as e:
                logger.warning(f"Apple Vision OCR failed: {e}")
        elif provider == "apple":
            logger.warning("ARTEMIS_OCR_PROVIDER=apple but Apple Vision is unavailable")
            return []
    if provider == "apple":
        return []

    api_key = _google_vision_key()
    if not api_key:
        return []

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

    active_client = client if client is not None else get_http_client()
    response = await active_client.post(url, json=data, headers=headers, timeout=30.0)
    return _parse_ocr_response(response)


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
