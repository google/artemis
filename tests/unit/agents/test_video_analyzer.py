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
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from artemis.agents.video_analyzer import chunk_conversation
from artemis.agents.video_analyzer.chunk_conversation import (
    _MISSING_SUMMARY_ERROR,
    _NO_TOOL_CALL_ERROR,
    SubAgentAnswerExhausted,
    _ChunkMedia,
    _drive_sub_agent_loop,
    _run_native_chunk_conversation,
)
from artemis.agents.video_analyzer.video_analyzer import VideoAnalyzer
from artemis.context import ArtemisContext
from google.genai import types
import pytest


@pytest.mark.asyncio
async def test_video_analyzer_run():
    # Mock context
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = None
    mock_ctx.llm_config = MagicMock()
    mock_ctx.llm_config.utils.video_analyzer = MagicMock()
    mock_ctx.llm_config.utils.video_analyzer.model = "gemini-3.7-flash"
    mock_ctx.agent_config = SimpleNamespace(video_analyzer=SimpleNamespace(processing="static"))

    # Mock Gemini Client
    mock_client = MagicMock()
    mock_client.aio = MagicMock()
    mock_client.aio.models = MagicMock()
    mock_chunk = MagicMock()
    mock_chunk.text = "Final analysis: The action succeeded."
    mock_chunk.function_calls = []

    async def mock_stream():
        yield mock_chunk

    mock_client.aio.models.generate_content_stream = AsyncMock(return_value=mock_stream())

    # Mock file reading for prompts
    mock_prompt = "# Dynamic Video Analyzer..."

    with (
        patch(
            "artemis.agents.video_analyzer.video_analyzer.genai.Client",
            return_value=mock_client,
        ),
        patch(
            "pathlib.Path.exists",
            autospec=True,
            side_effect=lambda self: not str(self).endswith(".jpg"),
        ),
        patch("pathlib.Path.read_text", return_value=mock_prompt),
        patch(
            "artemis.agents.video_analyzer.video_analyzer.cleanup_abandoned_gemini_files",
            AsyncMock(),
        ),
    ):
        analyzer = VideoAnalyzer(mock_ctx)
        result, status = await analyzer.run(
            time_description="from 10s to 15s", purpose="Verify action"
        )

        assert result == "Final analysis: The action succeeded."
        assert status == "success"
        assert mock_client.aio.models.generate_content_stream.called


@pytest.mark.asyncio
async def test_video_analyzer_preserves_thought_signature():
    # Mock context
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = None
    mock_ctx.llm_config = MagicMock()
    mock_ctx.llm_config.utils.video_analyzer = MagicMock()
    mock_ctx.llm_config.utils.video_analyzer.model = "gemini-3.7-flash"
    mock_ctx.agent_config = SimpleNamespace(video_analyzer=SimpleNamespace(processing="static"))

    # Mock MobileDeviceController
    mock_controller = MagicMock()
    mock_controller.extract_segment_metadata = AsyncMock(
        return_value=MagicMock(
            success=True,
            video_path="/tmp/video.mp4",
            actual_start_relative_time=10.0,
        )
    )

    from google.genai import types

    dummy_signature = b"dummy_thought_signature_bytes_56789"

    # Turn 1: Model calls spawn_sub_agent
    spawn_call = types.FunctionCall(
        name="spawn_sub_agent",
        args={
            "start_time": 10.0,
            "end_time": 15.0,
            "specific_query": "Verify login screen",
        },
    )
    spawn_part = types.Part(
        function_call=spawn_call,
        thought_signature=dummy_signature,
    )

    mock_chunk_1 = MagicMock()
    mock_chunk_1.text = ""
    mock_chunk_1.function_calls = [spawn_call]
    mock_chunk_1.candidates = [
        types.Candidate(content=types.Content(role="model", parts=[spawn_part]))
    ]

    # Turn 2: Model returns final answer
    mock_chunk_2 = MagicMock()
    mock_chunk_2.text = "Final analysis: The spawn succeeded and login button is visible."
    mock_chunk_2.function_calls = []
    mock_chunk_2.candidates = [
        types.Candidate(
            content=types.Content(
                role="model",
                parts=[types.Part.from_text(text=mock_chunk_2.text)],
            )
        )
    ]

    # We mock generate_content_stream
    call_count = 0
    captured_contents = []

    async def mock_stream_1():
        yield mock_chunk_1

    async def mock_stream_2():
        yield mock_chunk_2

    async def mock_generate_content_stream(model, contents, config):
        nonlocal call_count
        has_submit_answer = False
        if config.tools:
            for t in config.tools:
                fd_list = getattr(t, "function_declarations", None)
                if fd_list:
                    for fd in fd_list:
                        if fd.name == "submit_answer":
                            has_submit_answer = True
                            break
        if not has_submit_answer:
            captured_contents.append(list(contents))  # copy history contents passed to call
            call_count += 1
            if call_count == 1:
                return mock_stream_1()
            else:
                return mock_stream_2()
        else:
            submit_call = types.FunctionCall(
                name="submit_answer",
                args={
                    "confidence_score": 0.9,
                    "summary": "Sub-agent verification finished",
                    "analysis": "Analysis here",
                    "verification_timestamp_secs": 10.0,
                },
            )
            mock_sub_chunk = MagicMock()
            mock_sub_chunk.text = ""
            mock_sub_chunk.function_calls = [submit_call]

            async def sub_stream():
                yield mock_sub_chunk

            return sub_stream()

    # Mock Gemini Client
    mock_client = MagicMock()
    mock_client.aio = MagicMock()
    mock_client.aio.files = MagicMock()

    mock_file = MagicMock()
    mock_file.name = "dummy_cloud_file_123"
    mock_file.uri = "https://gemini-file-api/dummy_cloud_file_123"
    mock_file.mime_type = "video/mp4"
    mock_client.aio.files.upload = AsyncMock(return_value=mock_file)

    mock_f_state = MagicMock()
    mock_f_state.state.name = "ACTIVE"
    mock_client.aio.files.get = AsyncMock(return_value=mock_f_state)
    mock_client.aio.files.delete = AsyncMock()

    mock_client.aio.models = MagicMock()
    mock_client.aio.models.generate_content_stream = AsyncMock(
        side_effect=mock_generate_content_stream
    )

    # Mock file reading for prompts
    mock_prompt = "# Dynamic Video Analyzer..."

    with (
        patch(
            "artemis.agents.video_analyzer.video_analyzer.genai.Client",
            return_value=mock_client,
        ),
        patch(
            "artemis.agents.video_analyzer.video_analyzer.get_controller",
            return_value=mock_controller,
        ),
        patch(
            "pathlib.Path.exists",
            autospec=True,
            side_effect=lambda self: not str(self).endswith(".jpg"),
        ),
        patch(
            "pathlib.Path.stat",
            return_value=os.stat_result((0o100644, 0, 0, 0, 0, 0, 1024 * 1024, 0, 0, 0)),
        ),
        patch("pathlib.Path.read_text", return_value=mock_prompt),
        patch(
            "artemis.agents.video_analyzer.video_analyzer.cleanup_abandoned_gemini_files",
            AsyncMock(),
        ),
        patch(
            "artemis.agents.video_analyzer.video_analyzer.compress_video_for_api",
            AsyncMock(return_value=Path("/tmp/video.mp4")),
        ),
    ):
        analyzer = VideoAnalyzer(mock_ctx)
        result, status = await analyzer.run(
            time_description="from 10s to 15s", purpose="Verify login"
        )

        assert status == "success"
        assert "spawn succeeded" in result
        assert call_count == 2

        # Verify that the second model call's history contains the model turn with the thought_signature
        second_call_history = captured_contents[1]
        assert len(second_call_history) >= 4

        model_turn = second_call_history[1]
        assert model_turn.role == "model"
        assert len(model_turn.parts) == 1
        assert model_turn.parts[0].function_call.name == "spawn_sub_agent"
        assert model_turn.parts[0].thought_signature == dummy_signature


@pytest.mark.asyncio
async def test_video_analyzer_sub_agent_confidence_validation():
    # Mock context
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = None
    mock_ctx.llm_config = MagicMock()
    mock_ctx.llm_config.utils.video_analyzer = MagicMock()
    mock_ctx.llm_config.utils.video_analyzer.model = "gemini-3.7-flash"
    mock_ctx.agent_config = SimpleNamespace(video_analyzer=SimpleNamespace(processing="static"))

    # Mock MobileDeviceController
    mock_controller = MagicMock()
    mock_controller.extract_segment_metadata = AsyncMock(
        return_value=MagicMock(
            success=True,
            video_path="/tmp/video.mp4",
            actual_start_relative_time=10.0,
        )
    )

    from google.genai import types

    # Main agent responses
    # Turn 1: Calls spawn_sub_agent
    spawn_call = types.FunctionCall(
        name="spawn_sub_agent",
        args={
            "start_time": 10.0,
            "end_time": 15.0,
            "specific_query": "Verify login screen",
        },
    )
    mock_main_chunk_1 = MagicMock()
    mock_main_chunk_1.text = ""
    mock_main_chunk_1.function_calls = [spawn_call]

    # Turn 2: Returns final answer
    mock_main_chunk_2 = MagicMock()
    mock_main_chunk_2.text = "Final blackboard status checked."
    mock_main_chunk_2.function_calls = []

    # Sub-agent responses
    # Turn 1: Calls submit_answer with invalid confidence_score (string)
    invalid_submit_call = types.FunctionCall(
        name="submit_answer",
        args={
            "timeline_events": [
                {
                    "confidence_score": "HIGH",
                    "transcription": "Invalid confidence score call",
                    "verification_timestamp_secs": 10.0,
                }
            ],
            "summary": "Invalid confidence score call",
            "analysis": "Some analysis",
        },
    )
    sub_dummy_signature = b"sub_dummy_signature_9999"
    sub_part_1 = types.Part(
        function_call=invalid_submit_call,
        thought_signature=sub_dummy_signature,
    )
    mock_sub_chunk_1 = MagicMock()
    mock_sub_chunk_1.text = ""
    mock_sub_chunk_1.function_calls = [invalid_submit_call]
    mock_sub_chunk_1.candidates = [
        types.Candidate(content=types.Content(role="model", parts=[sub_part_1]))
    ]

    # Turn 2: Calls submit_answer with valid confidence_score
    valid_submit_call = types.FunctionCall(
        name="submit_answer",
        args={
            "timeline_events": [
                {
                    "confidence_score": 0.8,
                    "transcription": "Valid confidence score call",
                    "verification_timestamp_secs": 10.0,
                }
            ],
            "summary": "Valid confidence score call",
            "analysis": "Some analysis",
        },
    )
    mock_sub_chunk_2 = MagicMock()
    mock_sub_chunk_2.text = ""
    mock_sub_chunk_2.function_calls = [valid_submit_call]
    mock_sub_chunk_2.candidates = [
        types.Candidate(
            content=types.Content(role="model", parts=[types.Part(function_call=valid_submit_call)])
        )
    ]

    # Stream generator mocks
    main_calls = 0
    sub_calls = 0
    captured_sub_contents = []

    async def mock_main_stream():
        nonlocal main_calls
        if main_calls == 0:
            main_calls += 1
            yield mock_main_chunk_1
        else:
            yield mock_main_chunk_2

    async def mock_sub_stream():
        nonlocal sub_calls
        if sub_calls == 0:
            sub_calls += 1
            yield mock_sub_chunk_1
        else:
            yield mock_sub_chunk_2

    async def mock_generate_content_stream(model, contents, config):
        system_instruction = config.system_instruction
        if "Dynamic Video Analyzer" in str(system_instruction):
            return mock_main_stream()
        else:
            captured_sub_contents.append(list(contents))
            return mock_sub_stream()

    # Mock Gemini Client
    mock_client = MagicMock()
    mock_client.aio = MagicMock()
    mock_client.aio.files = MagicMock()

    mock_file = MagicMock()
    mock_file.name = "dummy_cloud_file_123"
    mock_file.uri = "https://gemini-file-api/dummy_cloud_file_123"
    mock_file.mime_type = "video/mp4"
    mock_client.aio.files.upload = AsyncMock(return_value=mock_file)

    mock_f_state = MagicMock()
    mock_f_state.state.name = "ACTIVE"
    mock_client.aio.files.get = AsyncMock(return_value=mock_f_state)
    mock_client.aio.files.delete = AsyncMock()

    mock_client.aio.models = MagicMock()
    mock_client.aio.models.generate_content_stream = AsyncMock(
        side_effect=mock_generate_content_stream
    )

    # Mock file reading for prompts
    mock_main_prompt = "# Dynamic Video Analyzer..."
    mock_sub_prompt = "# Sub Agent..."

    def mock_read_text(self, encoding="utf-8"):
        if "video_analyzer.md" in str(self):
            return mock_main_prompt
        elif "video_sub_agent.md" in str(self):
            return mock_sub_prompt
        return ""

    with (
        patch(
            "artemis.agents.video_analyzer.video_analyzer.genai.Client",
            return_value=mock_client,
        ),
        patch(
            "artemis.agents.video_analyzer.video_analyzer.get_controller",
            return_value=mock_controller,
        ),
        patch(
            "pathlib.Path.exists",
            autospec=True,
            side_effect=lambda self: not str(self).endswith(".jpg"),
        ),
        patch(
            "pathlib.Path.stat",
            return_value=os.stat_result((0o100644, 0, 0, 0, 0, 0, 1024 * 1024, 0, 0, 0)),
        ),
        patch("pathlib.Path.read_text", mock_read_text),
        patch("pathlib.Path.mkdir"),
        patch("pathlib.Path.touch"),
        patch("pathlib.Path.is_dir", return_value=True),
        patch(
            "artemis.agents.video_analyzer.video_analyzer.cleanup_abandoned_gemini_files",
            AsyncMock(),
        ),
        patch(
            "artemis.agents.video_analyzer.video_analyzer.compress_video_for_api",
            AsyncMock(return_value=Path("/tmp/video.mp4")),
        ),
        patch(
            "asyncio.create_subprocess_exec",
            AsyncMock(return_value=MagicMock(communicate=AsyncMock(return_value=(b"", b"")))),
        ),
    ):
        analyzer = VideoAnalyzer(mock_ctx)
        result, status = await analyzer.run(
            time_description="from 10s to 15s", purpose="Verify login"
        )

        assert status == "success"
        assert sub_calls == 1
        assert len(analyzer.blackboard_entries) == 1
        entry = analyzer.blackboard_entries[0]
        assert entry["confidence_score"] == 0.8
        assert entry["summary"] == "Valid confidence score call"

        # Verify that sub-agent turn 2 received the model turn with the thought_signature
        assert len(captured_sub_contents) >= 2
        sub_turn_2_contents = captured_sub_contents[1]
        model_turns = [c for c in sub_turn_2_contents if c.role == "model"]
        assert len(model_turns) == 1
        assert model_turns[0].parts[0].thought_signature == sub_dummy_signature


# ------------------------------------------------- static sub-agent loop


def _static_media(tmp_path) -> _ChunkMedia:
    media = _ChunkMedia()
    media.path = tmp_path / "segment.mp4"
    media.path.write_bytes(b"video")
    media.compressed_path = media.path
    media.actual_start = 0.0
    media.prompt_with_context = "Watch the toggle."
    return media


def _valid_static_event() -> dict:
    return {
        "start_time": 1.0,
        "end_time": 2.0,
        "transcription": "Battery Saver toggled on",
        "confidence_score": 0.9,
        "verification_timestamp_secs": 1.5,
    }


def _scripted_static_turns(*turns):
    """Fakes ``_stream_sub_agent_turn`` with ``(text, function_calls)`` per turn."""
    scripted = list(turns)

    async def fake_turn(analyzer, sub_agent_contents, trace_id):
        text, function_calls = scripted.pop(0)
        return text, function_calls, []

    return fake_turn


def _static_analyzer() -> MagicMock:
    analyzer = MagicMock()
    analyzer.model_name = "gemini-3.7-flash"
    analyzer.sub_system_prompt = "system"
    analyzer.ctx.data_engine = None
    return analyzer


@pytest.mark.asyncio
async def test_static_submit_answer_without_summary_is_sent_back_then_accepted(tmp_path):
    first = types.FunctionCall(
        name="submit_answer", args={"timeline_events": [_valid_static_event()]}
    )
    second = types.FunctionCall(
        name="submit_answer",
        args={"summary": "s2", "analysis": "a2", "timeline_events": []},
    )
    contents: list = []

    with (
        patch.object(
            chunk_conversation,
            "_stream_sub_agent_turn",
            new=_scripted_static_turns(("", [first]), ("", [second])),
        ),
        patch.object(chunk_conversation, "_persist_timeline_events", new=AsyncMock()) as persist,
    ):
        answer = await _drive_sub_agent_loop(
            _static_analyzer(), contents, None, _static_media(tmp_path), 0.0, 5.0, "verify"
        )

    assert answer == ("s2", "a2")
    # Only the accepted answer reaches persistence.
    persist.assert_awaited_once()
    assert persist.await_args.args[1] == []
    rejection = contents[-1]
    assert rejection.role == "user"
    assert rejection.parts[0].function_response.response["error"] == _MISSING_SUMMARY_ERROR


@pytest.mark.asyncio
async def test_static_loop_raises_when_no_turn_calls_submit_answer(tmp_path):
    with (
        patch.object(
            chunk_conversation,
            "_stream_sub_agent_turn",
            new=_scripted_static_turns(("text only", []), ("still text", [])),
        ),
        patch.object(chunk_conversation, "_persist_timeline_events", new=AsyncMock()) as persist,
        pytest.raises(SubAgentAnswerExhausted) as raised,
    ):
        await _drive_sub_agent_loop(
            _static_analyzer(), [], None, _static_media(tmp_path), 0.0, 5.0, "verify"
        )

    assert raised.value.reason == _NO_TOOL_CALL_ERROR
    persist.assert_not_awaited()


@pytest.mark.asyncio
async def test_static_last_turn_timeline_failure_raises_and_persists_nothing(tmp_path):
    bad = dict(_valid_static_event(), confidence_score="HIGH")
    out_of_range = dict(_valid_static_event(), verification_timestamp_secs=42.0)
    first = types.FunctionCall(
        name="submit_answer", args={"summary": "s1", "timeline_events": [bad]}
    )
    second = types.FunctionCall(
        name="submit_answer", args={"summary": "s2", "timeline_events": [out_of_range]}
    )

    with (
        patch.object(
            chunk_conversation,
            "_stream_sub_agent_turn",
            new=_scripted_static_turns(("", [first]), ("", [second])),
        ),
        patch.object(chunk_conversation, "_persist_timeline_events", new=AsyncMock()) as persist,
        pytest.raises(SubAgentAnswerExhausted) as raised,
    ):
        await _drive_sub_agent_loop(
            _static_analyzer(), [], None, _static_media(tmp_path), 0.0, 5.0, "verify"
        )

    assert "verification_timestamp_secs" in raised.value.reason
    assert "42.0" in str(raised.value)
    persist.assert_not_awaited()


@pytest.mark.asyncio
async def test_exhausted_static_chunk_is_not_committed_to_the_blackboard(tmp_path):
    analyzer = _static_analyzer()
    uploaded = SimpleNamespace(name="files/abc", uri="https://files/abc", mime_type="video/mp4")
    analyzer.upload_and_poll_file = AsyncMock(return_value=uploaded)
    analyzer.client.aio.files.delete = AsyncMock()
    analyzer.cloud_files_to_cleanup = set()

    with (
        patch.object(
            chunk_conversation,
            "_stream_sub_agent_turn",
            new=_scripted_static_turns(("text only", []), ("still text", [])),
        ),
        pytest.raises(SubAgentAnswerExhausted),
    ):
        await _run_native_chunk_conversation(
            analyzer, _static_media(tmp_path), 0.0, 5.0, 0.0, 5.0, "verify", "lease-1"
        )

    analyzer.blackboard.complete_segment.assert_not_called()
    analyzer.client.aio.files.delete.assert_awaited_once_with(name="files/abc")
