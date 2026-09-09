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

"""Unit tests for Universal VideoAnalyzer dual-path execution engine."""

from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from langchain_core.messages import AIMessage

from artemis.agents.video_analyzer.video_analyzer import (
    UNIVERSAL_MAIN_TOOLS,
    UNIVERSAL_SUBMIT_ANSWER_TOOL,
    VideoAnalyzer,
)
from artemis.context import ArtemisContext


@pytest.fixture
def mock_context(tmp_path):
    ctx = Mock(spec=ArtemisContext)
    ctx.llm_config = Mock()
    mock_llm_cfg = Mock()
    mock_llm_cfg.model = "claude-3-7-sonnet"
    mock_llm_cfg.temperature = 0.2
    ctx.llm_config.utils = Mock()
    ctx.llm_config.utils.video_analyzer = mock_llm_cfg

    ctx.device = Mock()
    ctx.device.device_width = 1080
    ctx.device.device_height = 2400

    ctx.data_engine = Mock()
    ctx.data_engine.base_dir = tmp_path
    return ctx


def test_video_analyzer_universal_engine_detection(mock_context):
    """Test that VideoAnalyzer routes to Universal Engine when non-Gemini model is set."""
    from artemis.config import settings

    with patch.object(settings, "GOOGLE_API_KEY", None):
        agent = VideoAnalyzer(mock_context)
        assert agent.use_native_gemini is False
        assert agent.model_name == "claude-3-7-sonnet"


def test_universal_tool_schemas():
    """Verify schema structures for Universal tools."""
    assert UNIVERSAL_SUBMIT_ANSWER_TOOL["type"] == "function"
    assert UNIVERSAL_SUBMIT_ANSWER_TOOL["function"]["name"] == "submit_answer"
    assert len(UNIVERSAL_MAIN_TOOLS) == 3
    tool_names = [t["function"]["name"] for t in UNIVERSAL_MAIN_TOOLS]
    assert "extract_segment_metadata" in tool_names
    assert "spawn_sub_agent" in tool_names
    assert "analyze_audio_only" in tool_names


@pytest.mark.asyncio
async def test_exec_single_chunk_universal(mock_context, tmp_path):
    """Test keyframe extraction and LangChain tool invocation in universal sub-agent."""
    agent = VideoAnalyzer(mock_context)
    agent.use_native_gemini = False

    fake_video = tmp_path / "compressed.mp4"
    fake_video.write_bytes(b"video_bytes")
    raw_video = tmp_path / "raw.mp4"
    raw_video.write_bytes(b"raw_bytes")

    fake_keyframes = [
        (0.0, b"frame0"),
        (1.0, b"frame1"),
        (2.0, b"frame2"),
    ]

    mock_llm = MagicMock()
    mock_response = AIMessage(
        content="Sub-agent analyzed frames.",
        tool_calls=[
            {
                "name": "submit_answer",
                "args": {
                    "summary": "Detected settings button tap.",
                    "analysis": "At 1.0s user clicked Settings icon.",
                    "timeline_events": [
                        {
                            "start_time": 0.5,
                            "end_time": 1.5,
                            "transcription": "Tap on Settings",
                            "confidence_score": 0.95,
                            "verification_timestamp_secs": 1.0,
                        }
                    ],
                },
                "id": "sub_call_1",
            }
        ],
    )
    mock_bound = MagicMock()
    mock_bound.ainvoke = AsyncMock(return_value=mock_response)
    mock_llm.bind_tools.return_value = mock_bound

    with (
        patch(
            "artemis.agents.video_analyzer.video_analyzer.extract_keyframes_from_video",
            return_value=fake_keyframes,
        ),
        patch(
            "artemis.agents.video_analyzer.video_analyzer.get_llm",
            return_value=mock_llm,
        ),
    ):
        result = await agent._exec_single_chunk_universal(
            compressed_path=fake_video,
            raw_path=raw_video,
            start_time=0.0,
            end_time=3.0,
            actual_start=0.0,
            prompt_with_context="Find when settings was opened.",
            specific_query="settings icon",
        )

        assert "Detected settings button tap" in result
        assert "At 1.0s user clicked Settings icon" in result
        assert len(agent.blackboard_entries) == 1
        assert agent.blackboard_entries[0]["summary"] == "Tap on Settings"
        assert agent.blackboard_entries[0]["confidence_score"] == 0.95


@pytest.mark.asyncio
async def test_exec_analyze_audio_universal(mock_context, tmp_path):
    """Test audio analysis in universal mode."""
    agent = VideoAnalyzer(mock_context)
    agent.use_native_gemini = False

    fake_audio = tmp_path / "audio.aac"
    fake_audio.write_bytes(b"audio_bytes")

    mock_llm = MagicMock()
    mock_response = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "submit_answer",
                "args": {
                    "summary": "Heard notification sound.",
                    "analysis": "Notification bell sounded at 2.0s.",
                },
                "id": "audio_call_1",
            }
        ],
    )
    mock_bound = MagicMock()
    mock_bound.ainvoke = AsyncMock(return_value=mock_response)
    mock_llm.bind_tools.return_value = mock_bound

    with patch(
        "artemis.agents.video_analyzer.video_analyzer.get_llm",
        return_value=mock_llm,
    ):
        result = await agent._exec_analyze_audio_universal(
            audio_path=fake_audio,
            start_time=1.0,
            end_time=4.0,
            actual_start=1.0,
            prompt_with_context="Listen for notification sound.",
            specific_query="notification bell",
        )

        assert "Heard notification sound" in result
        assert len(agent.blackboard_entries) == 1
        assert agent.blackboard_entries[0]["summary"] == "Heard notification sound."
        sent_content = mock_bound.ainvoke.await_args.args[0][1].content
        assert sent_content[1]["type"] == "audio"
        assert sent_content[1]["base64"] == "YXVkaW9fYnl0ZXM="


@pytest.mark.asyncio
async def test_run_universal_multi_turn_reasoning(mock_context):
    """Test main reasoning loop of VideoAnalyzer in universal mode with tool dispatch."""
    agent = VideoAnalyzer(mock_context)
    agent.use_native_gemini = False

    # Turn 1: Call extract_segment_metadata
    msg_turn1 = AIMessage(
        content="Let me check the video metadata first.",
        tool_calls=[
            {
                "name": "extract_segment_metadata",
                "args": {"start_time": 0.0, "end_time": 10.0},
                "id": "main_call_1",
            }
        ],
    )
    # Turn 2: Final response
    msg_turn2 = AIMessage(
        content="The video shows the app launched successfully at 3.5s.",
        tool_calls=[],
    )

    mock_llm = MagicMock()
    mock_bound = MagicMock()
    mock_bound.ainvoke = AsyncMock(side_effect=[msg_turn1, msg_turn2])
    mock_llm.bind_tools.return_value = mock_bound

    with (
        patch(
            "artemis.agents.video_analyzer.video_analyzer.get_llm",
            return_value=mock_llm,
        ),
        patch.object(
            agent,
            "exec_extract_segment_metadata",
            new_callable=AsyncMock,
            return_value='{"duration_seconds": 10.0, "file_size_mb": 2.5}',
        ),
    ):
        outcome, status = await agent._run_universal(
            time_description="from 0s to 10s",
            purpose="Verify app launch",
            system_prompt="You are a video analyzer.",
        )

        assert status == "success"
        assert "launched successfully at 3.5s" in outcome
        assert agent.exec_extract_segment_metadata.call_count == 1


# --- No-usable-answer handling -------------------------------------------
#
# The universal engine is single-shot: when the model neither calls
# submit_answer with a summary nor replies with text, it must raise
# SubAgentAnswerExhausted so the caller fails the blackboard segment instead
# of committing placeholder text as a succeeded entry.

_BLANK_SUBMIT = AIMessage(
    content="",
    tool_calls=[
        {
            "name": "submit_answer",
            "args": {"summary": "   ", "analysis": "", "timeline_events": []},
            "id": "blank_call",
        }
    ],
)
_EMPTY_RESPONSE = AIMessage(content="", tool_calls=[])
_TEXT_ONLY = AIMessage(content="Prose answer: the toast appeared at 2.0s.", tool_calls=[])


def _universal_agent(mock_context) -> VideoAnalyzer:
    agent = VideoAnalyzer(mock_context)
    agent.use_native_gemini = False
    return agent


async def _run_chunk_with_response(agent, tmp_path, response):
    """Drives the universal chunk path with a stubbed model reply.

    Returns ``(result_or_exception, persist_mock)`` so tests can assert both
    the outcome and whether the blackboard persistence step was reached.
    """
    raw_video = tmp_path / "raw.mp4"
    raw_video.write_bytes(b"raw_bytes")
    persist = AsyncMock()
    with (
        patch(
            "artemis.agents.video_analyzer.video_analyzer.extract_keyframes_from_video",
            return_value=[(0.0, b"frame0")],
        ),
        patch(
            "artemis.agents.video_analyzer.video_analyzer.extract_frames_at_timestamps",
            return_value=[],
        ),
        patch(
            "artemis.agents.video_analyzer.video_analyzer.extract_audio_from_video",
            new_callable=AsyncMock,
            side_effect=RuntimeError("no audio track"),
        ),
        patch(
            "artemis.agents.video_analyzer.universal_engine._persist_universal_events",
            persist,
        ),
        patch.object(
            agent,
            "_invoke_universal_model",
            new_callable=AsyncMock,
            return_value=response,
        ),
    ):
        try:
            result = await agent._exec_single_chunk_universal(
                compressed_path=raw_video,
                raw_path=raw_video,
                start_time=0.0,
                end_time=3.0,
                actual_start=0.0,
                prompt_with_context="Find the toast.",
                specific_query="toast",
            )
        except Exception as exc:  # noqa: BLE001 - the exception type is the assertion
            return exc, persist
    return result, persist


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [_EMPTY_RESPONSE, _BLANK_SUBMIT])
async def test_exec_single_chunk_universal_raises_without_usable_answer(
    mock_context, tmp_path, response
):
    """No tool call + no text, or blank-summary tool call + no text -> raise, no persistence."""
    from artemis.agents.video_analyzer.reliability import SubAgentAnswerExhausted

    agent = _universal_agent(mock_context)
    outcome, persist = await _run_chunk_with_response(agent, tmp_path, response)

    assert isinstance(outcome, SubAgentAnswerExhausted)
    assert outcome.turns == 1
    assert "submit_answer" in outcome.reason
    persist.assert_not_awaited()
    assert agent.blackboard_entries == []


@pytest.mark.asyncio
async def test_exec_single_chunk_universal_text_only_is_degraded_answer(mock_context, tmp_path):
    """Prose instead of the tool is a real (degraded) answer, not a placeholder."""
    agent = _universal_agent(mock_context)
    outcome, persist = await _run_chunk_with_response(agent, tmp_path, _TEXT_ONLY)

    assert isinstance(outcome, str)
    assert "Summary: Prose answer: the toast appeared at 2.0s." in outcome
    assert "Analysis: Prose answer: the toast appeared at 2.0s." in outcome
    persist.assert_awaited_once()
    # A degraded text answer carries no timeline events.
    assert persist.await_args.args[1] == []


@pytest.mark.asyncio
async def test_exec_single_chunk_universal_blank_summary_falls_through_to_text(
    mock_context, tmp_path
):
    """submit_answer with a blank summary is ignored; the text content becomes the answer."""
    response = AIMessage(
        content="Fallback prose from the model.",
        tool_calls=[
            {
                "name": "submit_answer",
                "args": {
                    "summary": "",
                    "analysis": "Orphaned analysis.",
                    "timeline_events": [{"start_time": 0.0, "end_time": 1.0}],
                },
                "id": "blank_call",
            }
        ],
    )
    agent = _universal_agent(mock_context)
    outcome, persist = await _run_chunk_with_response(agent, tmp_path, response)

    assert isinstance(outcome, str)
    assert "Summary: Fallback prose from the model." in outcome
    assert "Orphaned analysis" not in outcome
    # The blank tool call is dropped wholesale, events included.
    assert persist.await_args.args[1] == []


async def _run_audio_with_response(agent, tmp_path, response):
    """Drives the universal audio path with a stubbed model reply."""
    fake_audio = tmp_path / "audio.aac"
    fake_audio.write_bytes(b"audio_bytes")
    with (
        patch.object(
            agent,
            "_invoke_universal_model",
            new_callable=AsyncMock,
            return_value=response,
        ),
        patch.object(
            agent, "_record_blackboard_entry", wraps=agent._record_blackboard_entry
        ) as record,
    ):
        try:
            result = await agent._exec_analyze_audio_universal(
                audio_path=fake_audio,
                start_time=1.0,
                end_time=4.0,
                actual_start=1.0,
                prompt_with_context="Listen for the toast.",
                specific_query="toast sound",
            )
        except Exception as exc:  # noqa: BLE001 - the exception type is the assertion
            return exc, record
    return result, record


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [_EMPTY_RESPONSE, _BLANK_SUBMIT])
async def test_exec_analyze_audio_universal_raises_without_usable_answer(
    mock_context, tmp_path, response
):
    """Audio path: no usable answer -> raise before anything reaches the blackboard."""
    from artemis.agents.video_analyzer.reliability import SubAgentAnswerExhausted

    agent = _universal_agent(mock_context)
    outcome, record = await _run_audio_with_response(agent, tmp_path, response)

    assert isinstance(outcome, SubAgentAnswerExhausted)
    assert outcome.turns == 1
    record.assert_not_called()
    assert agent.blackboard_entries == []


@pytest.mark.asyncio
async def test_exec_analyze_audio_universal_text_only_is_degraded_answer(mock_context, tmp_path):
    """Audio path: prose instead of the tool is still committed as a degraded answer."""
    agent = _universal_agent(mock_context)
    outcome, record = await _run_audio_with_response(agent, tmp_path, _TEXT_ONLY)

    assert isinstance(outcome, str)
    assert "Audio Summary: Prose answer: the toast appeared at 2.0s." in outcome
    record.assert_called_once()
    assert len(agent.blackboard_entries) == 1
    assert agent.blackboard_entries[0]["summary"] == "Prose answer: the toast appeared at 2.0s."


def test_sub_agent_answer_exhausted_classifies_as_unknown():
    """The raise must land in the retryable / split / fallback bucket of the callers."""
    from artemis.agents.video_analyzer.reliability import (
        SubAgentAnswerExhausted,
        VideoFailureCategory,
        classify_video_failure,
    )

    failure = classify_video_failure(SubAgentAnswerExhausted(1, "x"))

    assert failure.category is VideoFailureCategory.UNKNOWN
    assert failure.retryable is True
    assert failure.should_split is True
    assert failure.should_fallback is True
