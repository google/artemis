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

"""Unit tests for ArtemisAgent connection pre-warming."""

from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from artemis.sdk.agent import Agent


@pytest.fixture
def mock_agent():
    agent = Agent.__new__(Agent)
    agent._session_id = "test-session-123"
    agent._context = MagicMock()
    return agent


@pytest.mark.asyncio
async def test_prewarm_uses_configured_google_model(mock_agent, monkeypatch):
    """Verifies pre-warming uses the active configured model from llm_config."""
    monkeypatch.delenv("ARTEMIS_FAKE_LLM", raising=False)
    mock_agent._context.llm_config.default.provider = "google"
    mock_agent._context.llm_config.default.model = "gemini-3.5-flash-lite"

    mock_client = MagicMock()
    mock_client.aio.models.count_tokens = AsyncMock(return_value=MagicMock())
    mock_chat = MagicMock()
    mock_chat.ainvoke = AsyncMock(return_value="pong")

    with (
        patch("artemis.sdk.agent.genai.Client", return_value=mock_client),
        patch("artemis.sdk.agent.ChatGoogleGenerativeAI", return_value=mock_chat) as mock_chat_cls,
        patch("artemis.sdk.agent.publish_startup_progress") as mock_progress,
    ):
        await mock_agent._prewarm_llm_connections(api_key="test-api-key")

        mock_chat_cls.assert_called_once_with(
            model="gemini-3.5-flash-lite", google_api_key="test-api-key"
        )
        mock_client.aio.models.count_tokens.assert_awaited_once_with(
            model="gemini-3.5-flash-lite", contents="ping"
        )
        mock_chat.ainvoke.assert_awaited_once_with("ping")
        assert mock_progress.call_count >= 2


@pytest.mark.asyncio
async def test_prewarm_skips_google_for_non_google_provider(mock_agent, monkeypatch):
    """Verifies pre-warming skips Google GenAI calls if provider is openai or anthropic."""
    monkeypatch.delenv("ARTEMIS_FAKE_LLM", raising=False)
    mock_agent._context.llm_config.default.provider = "openai"
    mock_agent._context.llm_config.default.model = "gpt-4o"

    with (
        patch("artemis.sdk.agent.genai.Client") as mock_client_cls,
        patch("artemis.sdk.agent.ChatGoogleGenerativeAI") as mock_chat_cls,
        patch("artemis.sdk.agent.publish_startup_progress") as mock_progress,
    ):
        await mock_agent._prewarm_llm_connections(api_key="test-api-key")

        mock_client_cls.assert_not_called()
        mock_chat_cls.assert_not_called()
        mock_progress.assert_called_with(
            "model_ready", "Model connection is ready", session_id="test-session-123"
        )


@pytest.mark.asyncio
async def test_prewarm_skips_when_fake_llm_set(mock_agent, monkeypatch):
    """Verifies ARTEMIS_FAKE_LLM=1 bypasses network pre-warming."""
    monkeypatch.setenv("ARTEMIS_FAKE_LLM", "1")

    with (
        patch("artemis.sdk.agent.genai.Client") as mock_client_cls,
        patch("artemis.sdk.agent.publish_startup_progress") as mock_progress,
    ):
        await mock_agent._prewarm_llm_connections(api_key="test-api-key")

        mock_client_cls.assert_not_called()
        mock_progress.assert_called_with(
            "model_ready", "Model connection is ready (fake LLM)", session_id="test-session-123"
        )


@pytest.mark.asyncio
async def test_prewarm_skips_gracefully_without_api_key(mock_agent, monkeypatch):
    """Verifies pre-warming handles missing API key without raising exception."""
    monkeypatch.delenv("ARTEMIS_FAKE_LLM", raising=False)
    with (
        patch("artemis.sdk.agent.settings") as mock_settings,
        patch("artemis.sdk.agent.publish_startup_progress") as mock_progress,
    ):
        mock_settings.GOOGLE_API_KEY = None
        await mock_agent._prewarm_llm_connections(api_key=None)

        mock_progress.assert_called_with(
            "model_ready",
            "Model connection will initialize on first use",
            session_id="test-session-123",
        )


@pytest.mark.asyncio
async def test_prewarm_handles_network_failure_gracefully(mock_agent, monkeypatch):
    """Verifies pre-warming handles network errors gracefully without crashing."""
    monkeypatch.delenv("ARTEMIS_FAKE_LLM", raising=False)
    mock_agent._context.llm_config.default.provider = "google"
    mock_agent._context.llm_config.default.model = "gemini-3.8-flash"

    with (
        patch("artemis.sdk.agent.genai.Client", side_effect=RuntimeError("Network down")),
        patch("artemis.sdk.agent.publish_startup_progress") as mock_progress,
    ):
        await mock_agent._prewarm_llm_connections(api_key="test-api-key")

        mock_progress.assert_called_with(
            "model_ready",
            "Model connection will initialize on first use",
            session_id="test-session-123",
        )
