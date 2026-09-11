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

"""Unit tests for Artemis entity extraction and package resolver."""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from artemis.agents.entity_extractor.entity_extractor import (
    ExtractionResult,
    extract_entity,
)


def test_extraction_result_model():
    """Verify ExtractionResult attributes and validation."""
    res = ExtractionResult(found=True, output="com.google.android.apps.nexuslauncher", reason="Exact match")
    assert res.found is True
    assert res.output == "com.google.android.apps.nexuslauncher"
    assert res.reason == "Exact match"


@pytest.mark.asyncio
async def test_extract_entity_success():
    """Verify successful LLM entity extraction invocation."""
    mock_ctx = MagicMock()
    mock_llm = MagicMock()
    expected_result = ExtractionResult(
        found=True,
        output="com.google.android.calculator",
        reason="Found calculator package in list",
    )
    mock_llm.ainvoke = AsyncMock(return_value=expected_result)

    with patch("artemis.agents.entity_extractor.entity_extractor.get_llm") as mock_get_llm:
        mock_get_llm.return_value.with_structured_output.return_value = mock_llm
        result = await extract_entity(
            request="Calculator",
            data="com.google.android.calculator\ncom.google.android.calendar",
            ctx=mock_ctx,
        )

    assert result.found is True
    assert result.output == "com.google.android.calculator"
    assert result.reason == "Found calculator package in list"


@pytest.mark.asyncio
async def test_extract_entity_handles_exception():
    """Verify extraction returns a graceful failure result when LLM throws an exception."""
    mock_ctx = MagicMock()
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(side_effect=RuntimeError("LLM connection timed out"))

    with patch("artemis.agents.entity_extractor.entity_extractor.get_llm") as mock_get_llm:
        mock_get_llm.return_value.with_structured_output.return_value = mock_llm
        result = await extract_entity(
            request="Settings",
            data="com.android.settings",
            ctx=mock_ctx,
        )

    assert result.found is False
    assert result.output is None
    assert "LLM extraction error" in result.reason
