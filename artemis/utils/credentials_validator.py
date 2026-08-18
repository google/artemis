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

"""Validation utility to verify LLM and Vision API keys against live provider endpoints."""

import httpx

from artemis.utils.logger import get_logger

logger = get_logger(__name__)

# Minimal 1x1 PNG transparent pixel base64 for Vision API testing
_TINY_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="


def _extract_error_message(resp: httpx.Response) -> str:
    """Extract a human-friendly error message from a provider HTTP response."""
    try:
        data = resp.json()
        if isinstance(data, dict):
            # Google style: {"error": {"message": "..."}}
            err_obj = data.get("error")
            if isinstance(err_obj, dict):
                msg = err_obj.get("message")
                if msg:
                    return str(msg)
            elif isinstance(err_obj, str):
                return err_obj

            # OpenAI / Anthropic / OpenRouter style
            if "message" in data:
                return str(data["message"])
            if "detail" in data:
                return str(data["detail"])
    except Exception:
        pass

    # Fallback to response text or HTTP status
    text = resp.text.strip()
    if text and len(text) < 200:
        return text
    return f"HTTP {resp.status_code}: {resp.reason_phrase}"


async def validate_api_key(
    provider: str,
    api_key: str,
    base_url: str | None = None,
    timeout: float = 12.0,
) -> tuple[bool, str]:
    """Tests if the provided API key is valid and usable with the corresponding provider.

    Args:
        provider: Provider identifier (e.g. 'google', 'gemini', 'ocr', 'openai', 'anthropic', etc.)
        api_key: Secret API key string.
        base_url: Optional custom endpoint URL.
        timeout: Request timeout in seconds.

    Returns:
        tuple[bool, str]: (is_valid, descriptive_message)
    """
    clean_provider = provider.strip().lower()
    clean_key = api_key.strip()

    if not clean_key:
        return False, "API key cannot be empty."

    # Placeholders / default template values should be rejected
    if clean_key in (
        "API_KEY",
        "your_gemini_api_key_here",
        "your_google_cloud_vision_api_key_here",
        "your_openai_api_key_here",
        "your_anthropic_api_key_here",
    ):
        return False, "Please enter a real API key instead of the placeholder."

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if clean_provider in ("google", "gemini", "vertexai", "vertex"):
                # Test Google Gemini via generativelanguage API
                url = f"https://generativelanguage.googleapis.com/v1beta/models?key={clean_key}&pageSize=1"
                resp = await client.get(url)
                if resp.status_code == 200:
                    return True, "Google Gemini API key verified successfully."
                err_msg = _extract_error_message(resp)
                return False, f"Gemini API verification failed ({resp.status_code}): {err_msg}"

            elif clean_provider in ("ocr", "vision", "google_vision"):
                # Test Google Cloud Vision OCR API
                url = f"https://vision.googleapis.com/v1/images:annotate?key={clean_key}"
                payload = {
                    "requests": [
                        {
                            "image": {"content": _TINY_PNG_B64},
                            "features": [{"type": "TEXT_DETECTION"}],
                        }
                    ]
                }
                resp = await client.post(
                    url, json=payload, headers={"Content-Type": "application/json"}
                )
                if resp.status_code == 200:
                    return True, "Google Cloud Vision OCR API key verified successfully."
                err_msg = _extract_error_message(resp)
                return False, f"Vision OCR API verification failed ({resp.status_code}): {err_msg}"

            elif clean_provider == "openai":
                endpoint = (
                    f"{base_url.rstrip('/')}/models"
                    if base_url
                    else "https://api.openai.com/v1/models"
                )
                resp = await client.get(endpoint, headers={"Authorization": f"Bearer {clean_key}"})
                if resp.status_code == 200:
                    return True, "OpenAI API key verified successfully."
                err_msg = _extract_error_message(resp)
                return False, f"OpenAI API verification failed ({resp.status_code}): {err_msg}"

            elif clean_provider in ("anthropic", "claude"):
                url = "https://api.anthropic.com/v1/models"
                resp = await client.get(
                    url,
                    headers={
                        "x-api-key": clean_key,
                        "anthropic-version": "2023-06-01",
                    },
                )
                if resp.status_code == 200:
                    return True, "Anthropic Claude API key verified successfully."
                err_msg = _extract_error_message(resp)
                return False, f"Anthropic API verification failed ({resp.status_code}): {err_msg}"

            elif clean_provider == "openrouter":
                url = "https://openrouter.ai/api/v1/auth/key"
                resp = await client.get(url, headers={"Authorization": f"Bearer {clean_key}"})
                if resp.status_code == 200:
                    return True, "OpenRouter API key verified successfully."
                err_msg = _extract_error_message(resp)
                return False, f"OpenRouter API verification failed ({resp.status_code}): {err_msg}"

            elif clean_provider in ("xai", "grok"):
                url = "https://api.x.ai/v1/models"
                resp = await client.get(url, headers={"Authorization": f"Bearer {clean_key}"})
                if resp.status_code == 200:
                    return True, "xAI API key verified successfully."
                err_msg = _extract_error_message(resp)
                return False, f"xAI API verification failed ({resp.status_code}): {err_msg}"

            elif clean_provider in ("ollama", "vllm", "custom"):
                if base_url:
                    endpoint = f"{base_url.rstrip('/')}/models"
                    headers = (
                        {"Authorization": f"Bearer {clean_key}"} if clean_key != "EMPTY" else {}
                    )
                    resp = await client.get(endpoint, headers=headers)
                    if resp.status_code == 200:
                        return True, f"Custom endpoint ({base_url}) verified successfully."
                    err_msg = _extract_error_message(resp)
                    return (
                        False,
                        f"Custom endpoint verification failed ({resp.status_code}): {err_msg}",
                    )
                return True, "Custom endpoint configured."

            else:
                # Unknown provider - fallback to basic length check
                if len(clean_key) >= 8:
                    return True, f"Credentials for {provider} configured."
                return False, f"Invalid API key format for {provider}."

    except httpx.TimeoutException:
        logger.warning(f"Timeout verifying API key for {provider}")
        return (
            False,
            f"Connection to {provider.capitalize()} API timed out. Please check network/proxy settings.",
        )
    except httpx.RequestError as exc:
        logger.warning(f"Network error verifying API key for {provider}: {exc}")
        return False, f"Network error connecting to {provider.capitalize()} API: {exc}"
    except Exception as exc:
        logger.error(f"Unexpected error validating API key for {provider}: {exc}")
        return False, f"Validation error: {exc}"
