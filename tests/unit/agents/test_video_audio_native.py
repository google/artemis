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

"""Native Gemini audio agent loop: only accepted answers reach the blackboard."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from artemis.agents.video_analyzer.audio_native import (
    _BAD_CONFIDENCE_ERROR,
    _AudioMedia,
    _drive_audio_agent_loop,
    _run_native_audio_conversation,
)
from artemis.agents.video_analyzer.chunk_conversation import (
    _MISSING_SUMMARY_ERROR,
    _NO_TOOL_CALL_ERROR,
    SUB_AGENT_MAX_TURNS,
)
from artemis.agents.video_analyzer.reliability import (
    SubAgentAnswerExhausted,
    VideoFailureCategory,
    classify_video_failure,
)
from artemis.agents.video_analyzer.universal_tools import build_submit_answer_declaration
from google.genai import types
import pytest


def _audio_response(text: str, function_calls: list) -> SimpleNamespace:
    """Fakes a non-streamed ``generate_content`` response for one audio turn."""
    parts = [types.Part(function_call=fc) for fc in function_calls]
    if text:
        parts.append(types.Part.from_text(text=text))
    return SimpleNamespace(
        text=text,
        function_calls=function_calls,
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=parts))],
    )


def _audio_analyzer(*turns) -> MagicMock:
    """Analyzer stub whose Gemini client answers the scripted ``(text, calls)`` turns."""
    analyzer = MagicMock()
    analyzer.model_name = "gemini-3.7-flash"
    analyzer.audio_system_prompt = "system"
    analyzer.submit_answer_tool = build_submit_answer_declaration()
    analyzer.model_call_timeout_seconds = 5.0
    analyzer.ctx.data_engine = None
    analyzer.client.aio.models.generate_content = AsyncMock(
        side_effect=[_audio_response(text, calls) for text, calls in turns]
    )
    return analyzer


def _audio_media(tmp_path) -> _AudioMedia:
    media = _AudioMedia()
    media.audio_path = tmp_path / "segment.wav"
    media.audio_path.write_bytes(b"audio")
    media.actual_start = 0.0
    media.prompt_with_context = "Listen for the chime."
    return media


@pytest.mark.asyncio
async def test_audio_submit_answer_without_summary_is_sent_back_then_accepted():
    first = types.FunctionCall(
        name="submit_answer", args={"summary": "  ", "confidence_score": 0.9}
    )
    second = types.FunctionCall(
        name="submit_answer",
        args={"summary": "chime\nheard", "analysis": "a2", "confidence_score": 0.7},
    )
    analyzer = _audio_analyzer(("", [first]), ("", [second]))
    contents: list = []

    answer = await _drive_audio_agent_loop(analyzer, contents)

    assert answer == ("chime heard", "a2", 0.7)
    assert analyzer.client.aio.models.generate_content.await_count == 2
    # The blank summary went back to the model as a function_response error.
    rejections = [
        c
        for c in contents
        if c.role == "user" and c.parts and c.parts[0].function_response is not None
    ]
    assert len(rejections) == 1
    assert rejections[0].parts[0].function_response.response["error"] == _MISSING_SUMMARY_ERROR


@pytest.mark.asyncio
async def test_audio_loop_raises_when_no_turn_calls_submit_answer():
    analyzer = _audio_analyzer(("text only", []), ("still text", []))

    with pytest.raises(SubAgentAnswerExhausted) as raised:
        await _drive_audio_agent_loop(analyzer, [])

    assert raised.value.turns == SUB_AGENT_MAX_TURNS
    assert raised.value.reason == _NO_TOOL_CALL_ERROR
    assert "No summary provided" not in str(raised.value)


@pytest.mark.asyncio
async def test_audio_loop_raises_when_confidence_score_is_never_valid():
    first = types.FunctionCall(
        name="submit_answer", args={"summary": "s1", "confidence_score": "HIGH"}
    )
    second = types.FunctionCall(
        name="submit_answer", args={"summary": "s2", "confidence_score": 1.5}
    )
    analyzer = _audio_analyzer(("", [first]), ("", [second]))
    contents: list = []

    with pytest.raises(SubAgentAnswerExhausted) as raised:
        await _drive_audio_agent_loop(analyzer, contents)

    assert raised.value.reason == _BAD_CONFIDENCE_ERROR
    assert contents[-1].role == "user"
    assert contents[-1].parts[0].function_response.response["error"] == _BAD_CONFIDENCE_ERROR


@pytest.mark.asyncio
async def test_exhausted_audio_segment_is_not_committed_and_cloud_file_is_deleted(tmp_path):
    analyzer = _audio_analyzer(("text only", []), ("still text", []))
    uploaded = SimpleNamespace(name="files/abc", uri="https://files/abc", mime_type="audio/wav")
    analyzer.upload_and_poll_file = AsyncMock(return_value=uploaded)
    analyzer.client.aio.files.delete = AsyncMock()
    analyzer.cloud_files_to_cleanup = {"files/abc"}
    analyzer._record_blackboard_entry = MagicMock()
    analyzer.blackboard = MagicMock()

    with pytest.raises(SubAgentAnswerExhausted):
        await _run_native_audio_conversation(
            analyzer, _audio_media(tmp_path), 0.0, 5.0, 0.0, 5.0, "chime", "lease-1"
        )

    analyzer.blackboard.complete_segment.assert_not_called()
    analyzer._record_blackboard_entry.assert_not_called()
    analyzer.client.aio.files.delete.assert_awaited_once_with(name="files/abc")
    assert analyzer.cloud_files_to_cleanup == set()


def test_sub_agent_exhaustion_is_classified_unknown():
    failure = classify_video_failure(SubAgentAnswerExhausted(2, "x"))

    assert failure.category is VideoFailureCategory.UNKNOWN
