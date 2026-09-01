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

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from artemis.utils.credentials_validator import validate_api_key, _extract_error_message


def test_extract_error_message():
    resp_google = MagicMock()
    resp_google.json.return_value = {"error": {"message": "API key invalid."}}
    assert _extract_error_message(resp_google) == "API key invalid."

    resp_openai = MagicMock()
    resp_openai.json.return_value = {"message": "Incorrect API key."}
    assert _extract_error_message(resp_openai) == "Incorrect API key."

    resp_fallback = MagicMock()
    resp_fallback.json.side_effect = ValueError("JSON parse error")
    resp_fallback.text = "Raw error text"
    assert _extract_error_message(resp_fallback) == "Raw error text"


@pytest.mark.asyncio
async def test_validate_empty_and_placeholder():
    is_valid, msg = await validate_api_key("google", "")
    assert not is_valid
    assert "empty" in msg.lower()

    is_valid, msg = await validate_api_key("google", "API_KEY")
    assert not is_valid
    assert "placeholder" in msg.lower()


@pytest.mark.asyncio
async def test_validate_gemini_success():
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp

        is_valid, msg = await validate_api_key("google", "valid_key_12345")
        assert is_valid
        assert "verified successfully" in msg


@pytest.mark.asyncio
async def test_validate_gemini_failure():
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.json.return_value = {"error": {"message": "API key not valid."}}
        mock_get.return_value = mock_resp

        is_valid, msg = await validate_api_key("google", "bad_key_12345")
        assert not is_valid
        assert "API key not valid" in msg


@pytest.mark.asyncio
async def test_validate_ocr_success():
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        is_valid, msg = await validate_api_key("ocr", "valid_ocr_key_12345")
        assert is_valid
        assert "verified successfully" in msg
