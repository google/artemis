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

"""Agentic video understanding sub-agent (Gemini Interactions API)."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from artemis.agents.video_analyzer import chunk_agentic
from artemis.agents.video_analyzer.chunk_agentic import (
    _drive_agentic_loop,
    build_interactions_submit_tool,
    run_agentic_chunk_conversation,
)
from artemis.agents.video_analyzer.chunk_conversation import (
    _MISSING_SUMMARY_ERROR,
    _NO_TOOL_CALL_ERROR,
    _WRONG_TOOL_ERROR,
    SubAgentAnswerExhausted,
    _ChunkMedia,
)
from artemis.agents.video_analyzer.reliability import (
    AgenticVideoDegraded,
    VideoFailureCategory,
    classify_video_failure,
    is_agentic_rejection,
)
from artemis.agents.video_analyzer.video_analyzer import VideoAnalyzer
from artemis.context import ArtemisContext
from pydantic import SecretStr
import pytest

# --------------------------------------------------------------------- fakes


def _context(model: str = "gemini-3.8-flash", **video_cfg) -> MagicMock:
    ctx = MagicMock(spec=ArtemisContext)
    ctx._video_blackboard = None
    ctx._video_circuit_breaker = None
    ctx._mobile_controller = None
    ctx._genai_client = None
    ctx.execution_setup = None
    ctx.data_engine = None
    ctx.agent_config = SimpleNamespace(
        video_analyzer=SimpleNamespace(
            enable_ledger=True,
            chunk_size_seconds=60.0,
            min_chunk_seconds=4.0,
            max_split_depth=4,
            circuit_breaker_threshold=2,
            circuit_breaker_cooldown_seconds=30.0,
            action_window_seconds=2.0,
            dense_action_fps=4.0,
            max_dense_action_frames=24,
            **video_cfg,
        )
    )
    ctx.device = SimpleNamespace(device_id="agentic-device")
    ctx.llm_config = MagicMock()
    ctx.llm_config.utils.video_analyzer.model = model
    ctx.llm_config.utils.video_analyzer.temperature = 0.2
    return ctx


def _function_call(call_id: str, arguments: dict, name: str = "submit_answer"):
    return SimpleNamespace(type="function_call", id=call_id, name=name, arguments=arguments)


def _model_output(text: str):
    return SimpleNamespace(type="model_output", content=[SimpleNamespace(type="text", text=text)])


_USAGE = SimpleNamespace(
    total_input_tokens=191,
    total_output_tokens=655,
    total_tokens=10571,
    total_cached_tokens=0,
    total_thought_tokens=0,
    total_tool_use_tokens=9725,
)


def _interaction(interaction_id: str, steps: list, status: str = "requires_action"):
    return SimpleNamespace(id=interaction_id, status=status, steps=steps, usage=_USAGE)


def _events(interaction, processing_calls: int = 2, *, argument_chunks: int = 1):
    """Scripts the SSE stream the real API emits for ``interaction``.

    Mirrors production: processing steps arrive as start/delta/stop triples,
    function-call arguments arrive only as ``arguments_delta`` JSON text, and
    ``interaction.completed`` carries no steps.
    """
    events = [
        SimpleNamespace(
            event_type="interaction.created",
            interaction=SimpleNamespace(id=interaction.id, status="in_progress"),
        )
    ]
    index = 0
    for _ in range(processing_calls):
        events.append(
            SimpleNamespace(
                event_type="step.start",
                index=index,
                step=SimpleNamespace(type="processing_call", id=f"proc_{index}"),
            )
        )
        events.append(SimpleNamespace(event_type="step.stop", index=index, usage=None))
        index += 1
    for step in interaction.steps:
        if step.type == "function_call":
            events.append(
                SimpleNamespace(
                    event_type="step.start",
                    index=index,
                    step=SimpleNamespace(
                        type="function_call", id=step.id, name=step.name, arguments={}
                    ),
                )
            )
            payload = json.dumps(step.arguments)
            size = max(1, len(payload) // argument_chunks)
            for start in range(0, len(payload), size):
                events.append(
                    SimpleNamespace(
                        event_type="step.delta",
                        index=index,
                        delta=SimpleNamespace(
                            type="arguments_delta", arguments=payload[start : start + size]
                        ),
                    )
                )
        elif step.type == "model_output":
            events.append(
                SimpleNamespace(
                    event_type="step.start",
                    index=index,
                    step=SimpleNamespace(type="model_output", content=[]),
                )
            )
            for content in step.content:
                events.append(
                    SimpleNamespace(
                        event_type="step.delta",
                        index=index,
                        delta=SimpleNamespace(type="text", text=content.text),
                    )
                )
        events.append(SimpleNamespace(event_type="step.stop", index=index, usage=None))
        index += 1
    events.append(
        SimpleNamespace(
            event_type="interaction.completed",
            interaction=SimpleNamespace(
                id=interaction.id, status=interaction.status, steps=None, usage=interaction.usage
            ),
        )
    )
    return events


class FakeInteractions:
    """Replays scripted event streams and records every request."""

    def __init__(self, turns, get_results=()):
        self.turns = list(turns)
        self.get_results = list(get_results)
        self.requests: list[dict] = []
        self.get_calls: list[str] = []

    async def create(self, **request):
        self.requests.append(request)
        events = self.turns.pop(0)
        if isinstance(events, Exception):
            raise events

        async def gen():
            for event in events:
                yield event

        return gen()

    async def get(self, interaction_id):
        self.get_calls.append(interaction_id)
        if not self.get_results:
            raise AssertionError(f"unexpected interactions.get({interaction_id})")
        return self.get_results.pop(0)


def _fake_client(turns, get_results=()):
    client = MagicMock()
    client.aio.interactions = FakeInteractions(turns, get_results)
    client.aio.files.delete = AsyncMock()
    return client


def _valid_event(start=1.0, end=2.0):
    return {
        "start_time": start,
        "end_time": end,
        "transcription": "Battery Saver toggled on",
        "confidence_score": 0.9,
        "verification_timestamp_secs": (start + end) / 2,
    }


def _media(tmp_path):
    media = _ChunkMedia()
    media.path = tmp_path / "segment.mp4"
    media.path.write_bytes(b"video")
    media.compressed_path = media.path
    media.actual_start = 0.0
    media.prompt_with_context = "Watch the toggle."
    return media


def _stub_analyzer(client):
    analyzer = MagicMock()
    analyzer.client = client
    analyzer.model_name = "gemini-3.8-flash"
    analyzer.sub_system_prompt = "system"
    analyzer.agentic_call_timeout_seconds = 30.0
    analyzer.ctx.data_engine = None
    return analyzer


_UPLOADED = SimpleNamespace(name="files/abc", uri="https://files/abc", mime_type="video/mp4")


def _extraction(tmp_path):
    raw_video = tmp_path / "segment.mp4"
    raw_video.write_bytes(b"video")
    extracted = SimpleNamespace(
        success=True,
        video_path=raw_video,
        actual_start_relative_time=0.0,
        duration_seconds=5.0,
        warning=None,
    )
    controller = SimpleNamespace(extract_segment_metadata=AsyncMock(return_value=extracted))
    return raw_video, controller


# ------------------------------------------------------------------- tools


def test_submit_tool_is_flat_json_schema_with_required_timeline():
    tool = build_interactions_submit_tool()
    assert tool["type"] == "function"
    assert tool["name"] == "submit_answer"
    assert "function" not in tool
    assert "timeline_events" in tool["parameters"]["required"]
    assert tool["parameters"]["properties"]["timeline_events"]["type"] == "array"


# --------------------------------------------------------- mode resolution


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model, video_cfg, expected_mode, expected_chunk",
    [
        ("gemini-3.8-flash", {}, "agentic", 600.0),
        ("gemini-3.8-flash", {"processing": "static"}, "static", 60.0),
        ("gemini-3.8-flash", {"agentic_chunk_size_seconds": 240.0}, "agentic", 240.0),
        ("gemini-3.5-flash-lite", {}, "static", 60.0),
        ("gemini-3.5-flash-lite", {"processing": "agentic"}, "agentic", 600.0),
        ("gemini-2.5-flash", {"processing": "agentic"}, "static", 60.0),
    ],
)
async def test_engine_resolves_video_processing(model, video_cfg, expected_mode, expected_chunk):
    with (
        patch(
            "artemis.agents.video_analyzer.video_analyzer.settings.GOOGLE_API_KEY",
            SecretStr("test-key"),
        ),
        patch(
            "artemis.agents.video_analyzer.video_analyzer.genai.Client",
            return_value=MagicMock(),
        ),
        patch(
            "artemis.agents.video_analyzer.video_analyzer.cleanup_abandoned_gemini_files",
            AsyncMock(),
        ),
    ):
        analyzer = VideoAnalyzer(_context(model, **video_cfg))

    assert analyzer.use_native_gemini is True
    assert analyzer.video_processing == expected_mode
    assert analyzer.chunk_size_seconds == expected_chunk


def test_universal_engine_never_uses_agentic_processing():
    with patch("artemis.agents.video_analyzer.video_analyzer.settings.GOOGLE_API_KEY", None):
        analyzer = VideoAnalyzer(_context("gemini-3.8-flash"))
    assert analyzer.use_native_gemini is False
    assert analyzer.video_processing == "static"
    assert analyzer.chunk_size_seconds == 60.0


# ------------------------------------------------------------ conversation


@pytest.mark.asyncio
async def test_agentic_chunk_commits_answer_and_records_usage(tmp_path):
    with patch("artemis.agents.video_analyzer.video_analyzer.settings.GOOGLE_API_KEY", None):
        analyzer = VideoAnalyzer(_context())
    interaction = _interaction(
        "v1_first",
        [
            _function_call(
                "call_1",
                {
                    "summary": "Toggle enabled",
                    "analysis": "Switch turned on at 1.5s",
                    "timeline_events": [_valid_event()],
                },
            )
        ],
    )
    client = _fake_client([_events(interaction, argument_chunks=3)])
    analyzer.client = client
    analyzer.use_native_gemini = True
    analyzer.video_processing = "agentic"
    analyzer.chunk_size_seconds = analyzer.agentic_chunk_size_seconds
    raw_video, controller = _extraction(tmp_path)

    spans = []

    class RecordingSpan:
        def __init__(self, name, **_):
            self.name = name
            self.payload = {}
            self.result = None
            spans.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    with (
        patch(
            "artemis.agents.video_analyzer.video_analyzer.get_controller",
            return_value=controller,
        ),
        patch(
            "artemis.agents.video_analyzer.video_analyzer.compress_video_for_api",
            new=AsyncMock(return_value=raw_video),
        ),
        patch.object(analyzer, "upload_and_poll_file", new=AsyncMock(return_value=_UPLOADED)),
        patch.object(chunk_agentic, "_persist_timeline_events", new=AsyncMock()) as persist,
        patch.object(chunk_agentic, "TraceSpan", RecordingSpan),
    ):
        result = await analyzer._exec_single_chunk(0.0, 5.0, "verify toggle")

    assert (
        result == "[from 0.0s to 5.0s] Summary: Toggle enabled Analysis: Switch turned on at 1.5s"
    )
    assert analyzer.blackboard.missing_intervals(0.0, 5.0, "verify toggle") == []
    persist.assert_awaited_once()
    assert persist.await_args.args[1] == [_valid_event()]

    request = client.aio.interactions.requests[0]
    assert request["model"] == "gemini-3.8-flash"
    assert request["stream"] is True
    assert request["input"][0] == {
        "type": "video",
        "uri": "https://files/abc",
        "mime_type": "video/mp4",
        "processing": "agentic",
    }
    assert request["input"][1]["type"] == "text"
    assert request["tools"][0]["name"] == "submit_answer"
    assert "previous_interaction_id" not in request
    assert client.aio.interactions.get_calls == []
    client.aio.files.delete.assert_awaited_once_with(name="files/abc")

    call_span = next(s for s in spans if s.name == "gemini_agentic_video_call")
    assert call_span.payload["usage_metadata"]["tool_use_tokens"] == 9725
    assert call_span.payload["processing_calls"] == 2
    assert call_span.result == "Function call: submit_answer"


@pytest.mark.asyncio
async def test_rejected_answer_is_sent_back_as_stateful_function_result(tmp_path):
    bad_event = dict(_valid_event(), confidence_score=1.7)
    first = _interaction(
        "v1_first",
        [
            _function_call(
                "call_1", {"summary": "s1", "analysis": "a1", "timeline_events": [bad_event]}
            )
        ],
    )
    second = _interaction(
        "v1_second",
        [
            _function_call(
                "call_2", {"summary": "s2", "analysis": "a2", "timeline_events": [_valid_event()]}
            )
        ],
    )
    client = _fake_client([_events(first), _events(second, processing_calls=0)])
    analyzer = _stub_analyzer(client)

    with patch.object(chunk_agentic, "_persist_timeline_events", new=AsyncMock()) as persist:
        summary, analysis = await _drive_agentic_loop(
            analyzer, _UPLOADED, None, _media(tmp_path), 0.0, 5.0, "verify toggle"
        )

    assert (summary, analysis) == ("s2", "a2")
    persist.assert_awaited_once()
    follow_up = client.aio.interactions.requests[1]
    assert follow_up["previous_interaction_id"] == "v1_first"
    assert len(follow_up["input"]) == 1
    result_step = follow_up["input"][0]
    assert result_step["type"] == "function_result"
    assert result_step["call_id"] == "call_1"
    assert result_step["name"] == "submit_answer"
    assert "confidence_score" in result_step["result"]["error"]
    # system_instruction and tools are interaction-scoped: they are not
    # inherited through previous_interaction_id and must be re-sent.
    assert follow_up["system_instruction"] == analyzer.sub_system_prompt
    assert follow_up["tools"][0]["name"] == "submit_answer"


@pytest.mark.asyncio
async def test_wrong_tool_is_rejected_then_accepted(tmp_path):
    first = _interaction("v1_first", [_function_call("call_1", {}, name="ns:other_tool")])
    second = _interaction(
        "v1_second",
        [_function_call("call_2", {"summary": "s2", "analysis": "a2", "timeline_events": []})],
    )
    client = _fake_client([_events(first, 0), _events(second, 0)])
    analyzer = _stub_analyzer(client)

    with patch.object(chunk_agentic, "_persist_timeline_events", new=AsyncMock()):
        summary, analysis = await _drive_agentic_loop(
            analyzer, _UPLOADED, None, _media(tmp_path), 0.0, 5.0, "verify toggle"
        )

    assert (summary, analysis) == ("s2", "a2")
    follow_up = client.aio.interactions.requests[1]
    assert follow_up["input"][0]["call_id"] == "call_1"
    assert "submit_answer" in follow_up["input"][0]["result"]["error"]
    assert follow_up["system_instruction"] == analyzer.sub_system_prompt


@pytest.mark.asyncio
async def test_missing_tool_call_gets_one_reminder_then_raises(tmp_path):
    first = _interaction(
        "v1_first", [_model_output("I looked but did not call the tool.")], "completed"
    )
    second = _interaction("v1_second", [_model_output("Still just text.")], "completed")
    client = _fake_client([_events(first, 0), _events(second, 0)])
    analyzer = _stub_analyzer(client)

    with pytest.raises(SubAgentAnswerExhausted) as raised:
        await _drive_agentic_loop(
            analyzer, _UPLOADED, None, _media(tmp_path), 0.0, 5.0, "verify toggle"
        )

    assert raised.value.reason == _NO_TOOL_CALL_ERROR
    assert "never called submit_answer" in str(raised.value)
    assert len(client.aio.interactions.requests) == 2
    reminder = client.aio.interactions.requests[1]
    assert reminder["previous_interaction_id"] == "v1_first"
    assert reminder["input"][0]["type"] == "text"
    assert "submit_answer" in reminder["input"][0]["text"]
    assert reminder["system_instruction"] == analyzer.sub_system_prompt
    assert client.aio.interactions.get_calls == []


@pytest.mark.asyncio
async def test_wrong_tool_on_every_turn_raises(tmp_path):
    first = _interaction("v1_first", [_function_call("call_1", {}, name="ns:other_tool")])
    second = _interaction("v1_second", [_function_call("call_2", {}, name="other_tool")])
    client = _fake_client([_events(first, 0), _events(second, 0)])
    analyzer = _stub_analyzer(client)

    with (
        patch.object(chunk_agentic, "_persist_timeline_events", new=AsyncMock()) as persist,
        pytest.raises(SubAgentAnswerExhausted) as raised,
    ):
        await _drive_agentic_loop(
            analyzer, _UPLOADED, None, _media(tmp_path), 0.0, 5.0, "verify toggle"
        )

    assert raised.value.reason == _WRONG_TOOL_ERROR
    persist.assert_not_awaited()
    assert len(client.aio.interactions.requests) == 2


@pytest.mark.asyncio
async def test_unparsed_call_on_requires_action_is_reread_by_id(tmp_path):
    """A stream that ends in requires_action without a parsable call is re-read via get()."""
    events = [
        SimpleNamespace(event_type="interaction.created", interaction=SimpleNamespace(id="v1_x")),
        SimpleNamespace(
            event_type="interaction.completed",
            interaction=SimpleNamespace(
                id="v1_x", status="requires_action", steps=None, usage=_USAGE
            ),
        ),
    ]
    full = _interaction(
        "v1_x",
        [_function_call("call_9", {"summary": "s9", "analysis": "a9", "timeline_events": []})],
    )
    client = _fake_client([events], get_results=[full])
    analyzer = _stub_analyzer(client)

    with patch.object(chunk_agentic, "_persist_timeline_events", new=AsyncMock()):
        summary, analysis = await _drive_agentic_loop(
            analyzer, _UPLOADED, None, _media(tmp_path), 0.0, 5.0, "verify toggle"
        )

    assert (summary, analysis) == ("s9", "a9")
    assert client.aio.interactions.get_calls == ["v1_x"]


@pytest.mark.asyncio
async def test_unparsable_arguments_drop_the_step_and_reread_by_id(tmp_path):
    """Malformed streamed arguments must not stand in as an empty answer."""
    events = [
        SimpleNamespace(event_type="interaction.created", interaction=SimpleNamespace(id="v1_x")),
        SimpleNamespace(
            event_type="step.start",
            index=0,
            step=SimpleNamespace(
                type="function_call", id="call_7", name="submit_answer", arguments={}
            ),
        ),
        SimpleNamespace(
            event_type="step.delta",
            index=0,
            delta=SimpleNamespace(type="arguments_delta", arguments='{"summary": "trunc'),
        ),
        SimpleNamespace(event_type="step.stop", index=0, usage=None),
        SimpleNamespace(
            event_type="interaction.completed",
            interaction=SimpleNamespace(
                id="v1_x", status="requires_action", steps=None, usage=_USAGE
            ),
        ),
    ]
    full = _interaction(
        "v1_x",
        [_function_call("call_7", {"summary": "s7", "analysis": "a7", "timeline_events": []})],
    )
    client = _fake_client([events], get_results=[full])
    analyzer = _stub_analyzer(client)

    with patch.object(chunk_agentic, "_persist_timeline_events", new=AsyncMock()) as persist:
        summary, analysis = await _drive_agentic_loop(
            analyzer, _UPLOADED, None, _media(tmp_path), 0.0, 5.0, "verify toggle"
        )

    assert (summary, analysis) == ("s7", "a7")
    assert client.aio.interactions.get_calls == ["v1_x"]
    assert len(client.aio.interactions.requests) == 1
    persist.assert_awaited_once()


def test_pending_step_with_invalid_arguments_json_is_dropped():
    step = chunk_agentic._PendingStep(kind="function_call", call_id="c", name="submit_answer")
    step.arguments_json.append("{not json")
    assert step.finish() is None

    intact = chunk_agentic._PendingStep(kind="function_call", call_id="c", name="submit_answer")
    intact.arguments_json.extend(['{"summary": ', '"ok"}'])
    assert intact.finish().arguments == {"summary": "ok"}


@pytest.mark.asyncio
async def test_submit_answer_without_summary_is_sent_back_not_committed(tmp_path):
    first = _interaction(
        "v1_first",
        [_function_call("call_1", {"timeline_events": [_valid_event()]})],
    )
    second = _interaction(
        "v1_second",
        [
            _function_call(
                "call_2", {"summary": "s2", "analysis": "a2", "timeline_events": [_valid_event()]}
            )
        ],
    )
    client = _fake_client([_events(first), _events(second, processing_calls=0)])
    analyzer = _stub_analyzer(client)

    with patch.object(chunk_agentic, "_persist_timeline_events", new=AsyncMock()) as persist:
        summary, analysis = await _drive_agentic_loop(
            analyzer, _UPLOADED, None, _media(tmp_path), 0.0, 5.0, "verify toggle"
        )

    assert (summary, analysis) == ("s2", "a2")
    # Only the accepted answer reaches persistence.
    persist.assert_awaited_once()
    follow_up = client.aio.interactions.requests[1]
    assert follow_up["previous_interaction_id"] == "v1_first"
    result_step = follow_up["input"][0]
    assert result_step["type"] == "function_result"
    assert result_step["call_id"] == "call_1"
    assert "summary" in result_step["result"]["error"]


@pytest.mark.asyncio
async def test_answer_never_given_a_summary_raises_and_persists_nothing(tmp_path):
    first = _interaction("v1_first", [_function_call("call_1", {"timeline_events": []})])
    second = _interaction("v1_second", [_function_call("call_2", {"summary": "  "})])
    client = _fake_client([_events(first, 0), _events(second, 0)])
    analyzer = _stub_analyzer(client)

    with (
        patch.object(chunk_agentic, "_persist_timeline_events", new=AsyncMock()) as persist,
        pytest.raises(SubAgentAnswerExhausted) as raised,
    ):
        await _drive_agentic_loop(
            analyzer, _UPLOADED, None, _media(tmp_path), 0.0, 5.0, "verify toggle"
        )

    assert raised.value.reason == _MISSING_SUMMARY_ERROR
    persist.assert_not_awaited()
    assert len(client.aio.interactions.requests) == 2


@pytest.mark.asyncio
async def test_last_turn_timeline_validation_failure_raises_and_persists_nothing(tmp_path):
    """A summary whose timeline still fails validation on the last turn is never accepted."""
    bad_event = dict(_valid_event(), confidence_score=1.7)
    first = _interaction(
        "v1_first",
        [
            _function_call(
                "call_1", {"summary": "s1", "analysis": "a1", "timeline_events": [bad_event]}
            )
        ],
    )
    out_of_range = dict(_valid_event(), verification_timestamp_secs=42.0)
    second = _interaction(
        "v1_second",
        [
            _function_call(
                "call_2",
                {"summary": "s2", "analysis": "a2", "timeline_events": [out_of_range]},
            )
        ],
    )
    client = _fake_client([_events(first, 0), _events(second, 0)])
    analyzer = _stub_analyzer(client)

    with (
        patch.object(chunk_agentic, "_persist_timeline_events", new=AsyncMock()) as persist,
        pytest.raises(SubAgentAnswerExhausted) as raised,
    ):
        await _drive_agentic_loop(
            analyzer, _UPLOADED, None, _media(tmp_path), 0.0, 5.0, "verify toggle"
        )

    # The message carries the last rejection the model received.
    assert "verification_timestamp_secs" in raised.value.reason
    assert "42.0" in str(raised.value)
    persist.assert_not_awaited()
    assert len(client.aio.interactions.requests) == 2
    assert client.aio.interactions.requests[1]["input"][0]["call_id"] == "call_1"


@pytest.mark.asyncio
async def test_exhausted_agentic_chunk_is_not_committed_to_the_blackboard(tmp_path):
    first = _interaction("v1_first", [_model_output("text only")], "completed")
    second = _interaction("v1_second", [_model_output("still text")], "completed")
    client = _fake_client([_events(first, 0), _events(second, 0)])
    analyzer = _stub_analyzer(client)
    analyzer.upload_and_poll_file = AsyncMock(return_value=_UPLOADED)
    analyzer.cloud_files_to_cleanup = set()

    with pytest.raises(SubAgentAnswerExhausted):
        await run_agentic_chunk_conversation(
            analyzer, _media(tmp_path), 0.0, 5.0, 0.0, 5.0, "verify toggle", "lease-1"
        )

    analyzer.blackboard.complete_segment.assert_not_called()
    # The uploaded clip is still cleaned up on the way out.
    client.aio.files.delete.assert_awaited_once_with(name="files/abc")


@pytest.mark.parametrize(
    "reason",
    [
        _NO_TOOL_CALL_ERROR,
        _WRONG_TOOL_ERROR,
        _MISSING_SUMMARY_ERROR,
        "Error: 'verification_timestamp_secs' 42.0 must fall within the start_time (0.0)"
        " and end_time (5.0) boundaries. Fix the payload and call submit_answer again.",
        "Error: 'confidence_score' is missing or out of bounds in one of the"
        " timeline_events. It must be a float between 0.0 and 1.0 inclusive.",
    ],
)
def test_exhaustion_is_classified_as_retryable_unknown(reason):
    """No rejection text may accidentally match a transport / media marker."""
    failure = classify_video_failure(SubAgentAnswerExhausted(2, reason))
    assert failure.category is VideoFailureCategory.UNKNOWN
    assert failure.retryable
    assert failure.should_split
    assert failure.should_fallback


@pytest.mark.asyncio
async def test_exhausted_chunk_leaves_the_interval_unanswered_for_re_analysis(tmp_path):
    """End to end: exhaustion fails the segment instead of caching an empty success."""
    with patch("artemis.agents.video_analyzer.video_analyzer.settings.GOOGLE_API_KEY", None):
        analyzer = VideoAnalyzer(_context())

    def text_only_turns():
        first = _interaction("v1_first", [_model_output("text only")], "completed")
        second = _interaction("v1_second", [_model_output("still text")], "completed")
        return [_events(first, 0), _events(second, 0)]

    # UNKNOWN is retried once from scratch, so two full conversations are scripted.
    analyzer.client = _fake_client(text_only_turns() + text_only_turns())
    analyzer.use_native_gemini = True
    analyzer.video_processing = "agentic"
    raw_video, controller = _extraction(tmp_path)

    with (
        patch(
            "artemis.agents.video_analyzer.video_analyzer.get_controller",
            return_value=controller,
        ),
        patch(
            "artemis.agents.video_analyzer.video_analyzer.compress_video_for_api",
            new=AsyncMock(return_value=raw_video),
        ),
        patch.object(analyzer, "upload_and_poll_file", new=AsyncMock(return_value=_UPLOADED)),
        patch.object(
            analyzer,
            "_exec_single_chunk_universal",
            new=AsyncMock(side_effect=RuntimeError("fallback engine declined")),
        ) as universal,
        patch("artemis.agents.video_analyzer.video_analyzer._record_llm_event"),
        patch("artemis.agents.video_analyzer.video_analyzer.asyncio.sleep", new=AsyncMock()),
        pytest.raises(RuntimeError, match="fallback engine declined"),
    ):
        await analyzer._exec_single_chunk(0.0, 5.0, "verify toggle")

    assert len(analyzer.client.aio.interactions.requests) == 4
    universal.assert_awaited_once()
    # Nothing succeeded, so the next claim re-analyses instead of hitting a cache.
    assert analyzer.blackboard.missing_intervals(0.0, 5.0, "verify toggle") == [(0.0, 5.0)]
    assert analyzer.blackboard.claim_segment(0.0, 5.0, "verify toggle").state == "claimed"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "cancelled", "incomplete", "budget_exceeded"])
async def test_completed_with_terminal_failure_status_raises(tmp_path, status):
    events = [
        SimpleNamespace(event_type="interaction.created", interaction=SimpleNamespace(id="v1")),
        SimpleNamespace(
            event_type="interaction.completed",
            interaction=SimpleNamespace(
                id="v1",
                status=status,
                steps=None,
                usage=None,
                errors=[SimpleNamespace(code="internal", message="video worker died")],
            ),
        ),
    ]
    client = _fake_client([events])
    analyzer = _stub_analyzer(client)

    with pytest.raises(RuntimeError, match=f"{status}.*video worker died"):
        await _drive_agentic_loop(
            analyzer, _UPLOADED, None, _media(tmp_path), 0.0, 5.0, "verify toggle"
        )

    # No follow-up is chained onto the failed interaction.
    assert len(client.aio.interactions.requests) == 1
    assert client.aio.interactions.get_calls == []


@pytest.mark.asyncio
async def test_completed_with_errors_raises_even_when_status_is_completed(tmp_path):
    events = [
        SimpleNamespace(event_type="interaction.created", interaction=SimpleNamespace(id="v1")),
        SimpleNamespace(
            event_type="interaction.completed",
            interaction=SimpleNamespace(
                id="v1",
                status="completed",
                steps=None,
                usage=None,
                errors=[SimpleNamespace(code="unavailable", message="overloaded")],
            ),
        ),
    ]
    client = _fake_client([events])
    analyzer = _stub_analyzer(client)

    with pytest.raises(RuntimeError, match="overloaded"):
        await _drive_agentic_loop(
            analyzer, _UPLOADED, None, _media(tmp_path), 0.0, 5.0, "verify toggle"
        )


@pytest.mark.asyncio
async def test_stream_error_event_raises(tmp_path):
    events = [
        SimpleNamespace(event_type="interaction.created", interaction=SimpleNamespace(id="v1")),
        SimpleNamespace(
            event_type="error", error=SimpleNamespace(code="unavailable", message="overloaded")
        ),
    ]
    client = _fake_client([events])
    analyzer = _stub_analyzer(client)

    with pytest.raises(RuntimeError, match="overloaded"):
        await _drive_agentic_loop(
            analyzer, _UPLOADED, None, _media(tmp_path), 0.0, 5.0, "verify toggle"
        )


@pytest.mark.asyncio
async def test_progress_is_streamed_into_the_trace(tmp_path):
    interaction = _interaction(
        "v1_first",
        [
            _model_output("Toggle appears at 74s."),
            _function_call("call_1", {"summary": "s", "analysis": "a", "timeline_events": []}),
        ],
    )
    client = _fake_client([_events(interaction, processing_calls=3)])
    analyzer = _stub_analyzer(client)
    lines = []
    analyzer.ctx.data_engine = SimpleNamespace(
        stream_output=lambda trace_id, text, is_thinking=None: lines.append((is_thinking, text)),
        record_trace=lambda **kw: None,
    )

    with patch.object(chunk_agentic, "_persist_timeline_events", new=AsyncMock()):
        await _drive_agentic_loop(
            analyzer, _UPLOADED, "trace-1", _media(tmp_path), 0.0, 5.0, "verify toggle"
        )

    thinking = [text for is_thinking, text in lines if is_thinking]
    text = [text for is_thinking, text in lines if not is_thinking]
    assert len(thinking) == 3
    assert "video lookup 3" in thinking[-1]
    assert text == ["Toggle appears at 74s."]


# ---------------------------------------------------------------- degrade


class _BadRequest(Exception):
    status_code = 400


class _PayloadTooLarge(Exception):
    status_code = 413


def test_agentic_rejection_is_only_a_400_about_processing():
    rejected = _BadRequest("400 INVALID_ARGUMENT: processing 'agentic' is not supported")
    assert is_agentic_rejection(classify_video_failure(rejected), rejected)

    other_400 = _BadRequest("400 INVALID_ARGUMENT: unknown field 'tools[0].foo'")
    assert not is_agentic_rejection(classify_video_failure(other_400), other_400)

    too_large = _PayloadTooLarge("413 request entity too large: processing")
    failure = classify_video_failure(too_large)
    assert failure.category is VideoFailureCategory.BAD_REQUEST
    assert failure.should_split
    assert not is_agentic_rejection(failure, too_large)

    outage = RuntimeError("503 service unavailable while processing")
    assert not is_agentic_rejection(classify_video_failure(outage), outage)


def test_degraded_marker_replans_without_fallback():
    failure = classify_video_failure(AgenticVideoDegraded("rejected"))
    assert failure.should_split
    assert not failure.should_fallback
    assert not failure.retryable


def _agentic_analyzer(tmp_path, first_turn):
    with patch("artemis.agents.video_analyzer.video_analyzer.settings.GOOGLE_API_KEY", None):
        analyzer = VideoAnalyzer(_context())
    analyzer.client = _fake_client([first_turn])
    analyzer.use_native_gemini = True
    analyzer.video_processing = "agentic"
    analyzer.chunk_size_seconds = analyzer.agentic_chunk_size_seconds
    raw_video, controller = _extraction(tmp_path)
    return analyzer, raw_video, controller


@pytest.mark.asyncio
async def test_rejected_agentic_request_degrades_run_and_hands_chunk_back(tmp_path):
    analyzer, raw_video, controller = _agentic_analyzer(
        tmp_path, _BadRequest("processing agentic is not supported")
    )

    with (
        patch(
            "artemis.agents.video_analyzer.video_analyzer.get_controller",
            return_value=controller,
        ),
        patch(
            "artemis.agents.video_analyzer.video_analyzer.compress_video_for_api",
            new=AsyncMock(return_value=raw_video),
        ),
        patch.object(analyzer, "upload_and_poll_file", new=AsyncMock(return_value=_UPLOADED)),
        patch(
            "artemis.agents.video_analyzer.chunk_native._run_native_chunk_conversation",
            new=AsyncMock(),
        ) as static_path,
        patch.object(analyzer, "_exec_single_chunk_universal", new=AsyncMock()) as universal,
        patch("artemis.agents.video_analyzer.video_analyzer._record_llm_event") as record_event,
        pytest.raises(AgenticVideoDegraded),
    ):
        await analyzer._exec_single_chunk(0.0, 300.0, "verify toggle")

    # The 300s clip is neither re-run statically nor handed to the fallback.
    static_path.assert_not_awaited()
    universal.assert_not_awaited()
    assert analyzer.video_processing == "static"
    assert analyzer.chunk_size_seconds == 60.0
    assert record_event.call_args.args[0] == "llm_fallback"
    assert record_event.call_args.args[1]["reason"] == "agentic_video_rejected"
    # The lease is released so the re-planned pieces can be claimed.
    assert analyzer.blackboard.missing_intervals(0.0, 300.0, "verify toggle") == [(0.0, 300.0)]


@pytest.mark.asyncio
async def test_degraded_chunk_is_replanned_at_the_static_chunk_size(tmp_path):
    analyzer, raw_video, controller = _agentic_analyzer(
        tmp_path, _BadRequest("processing agentic is not supported")
    )

    async def static_answer(analyzer_, media, cs, ce, *rest):
        return f"[from {cs:.1f}s to {ce:.1f}s] Summary: static Analysis: ok"

    with (
        patch(
            "artemis.agents.video_analyzer.video_analyzer.get_controller",
            return_value=controller,
        ),
        patch(
            "artemis.agents.video_analyzer.video_analyzer.compress_video_for_api",
            new=AsyncMock(return_value=raw_video),
        ),
        patch.object(analyzer, "upload_and_poll_file", new=AsyncMock(return_value=_UPLOADED)),
        patch(
            "artemis.agents.video_analyzer.chunk_native._run_native_chunk_conversation",
            new=AsyncMock(side_effect=static_answer),
        ) as static_path,
        patch("artemis.agents.video_analyzer.video_analyzer._record_llm_event"),
    ):
        result = await analyzer.exec_spawn_sub_agent(0.0, 150.0, "verify toggle")

    # One agentic attempt on the 150s clip, then 60s static pieces.
    assert len(analyzer.client.aio.interactions.requests) == 1
    segments = [(call.args[2], call.args[3]) for call in static_path.await_args_list]
    assert sorted(segments) == [(0.0, 60.0), (60.0, 120.0), (120.0, 150.0)]
    assert all(ce - cs <= 60.0 for cs, ce in segments)
    assert "PARTIAL" not in result
    assert result.count("Summary: static") == 3
    assert analyzer.video_processing == "static"


@pytest.mark.asyncio
async def test_413_in_agentic_mode_splits_without_degrading(tmp_path):
    analyzer, raw_video, controller = _agentic_analyzer(
        tmp_path, _PayloadTooLarge("413 request entity too large")
    )

    with (
        patch(
            "artemis.agents.video_analyzer.video_analyzer.get_controller",
            return_value=controller,
        ),
        patch(
            "artemis.agents.video_analyzer.video_analyzer.compress_video_for_api",
            new=AsyncMock(return_value=raw_video),
        ),
        patch.object(analyzer, "upload_and_poll_file", new=AsyncMock(return_value=_UPLOADED)),
        patch.object(
            analyzer,
            "_exec_single_chunk_universal",
            new=AsyncMock(side_effect=_PayloadTooLarge("413 still too large")),
        ),
        patch("artemis.agents.video_analyzer.video_analyzer._record_llm_event") as record_event,
        pytest.raises(_PayloadTooLarge) as raised,
    ):
        await analyzer._exec_single_chunk(0.0, 300.0, "verify toggle")

    assert classify_video_failure(raised.value).should_split
    assert analyzer.video_processing == "agentic"
    assert analyzer.chunk_size_seconds == analyzer.agentic_chunk_size_seconds
    reasons = [call.args[1].get("reason") for call in record_event.call_args_list]
    assert "agentic_video_rejected" not in reasons


@pytest.mark.asyncio
async def test_unrelated_400_in_agentic_mode_does_not_degrade(tmp_path):
    analyzer, raw_video, controller = _agentic_analyzer(
        tmp_path, _BadRequest("400 INVALID_ARGUMENT: unknown field 'tools[0].foo'")
    )

    with (
        patch(
            "artemis.agents.video_analyzer.video_analyzer.get_controller",
            return_value=controller,
        ),
        patch(
            "artemis.agents.video_analyzer.video_analyzer.compress_video_for_api",
            new=AsyncMock(return_value=raw_video),
        ),
        patch.object(analyzer, "upload_and_poll_file", new=AsyncMock(return_value=_UPLOADED)),
        patch.object(
            analyzer,
            "_exec_single_chunk_universal",
            new=AsyncMock(side_effect=_BadRequest("400 unknown field")),
        ),
        patch("artemis.agents.video_analyzer.video_analyzer._record_llm_event") as record_event,
        pytest.raises(_BadRequest),
    ):
        await analyzer._exec_single_chunk(0.0, 300.0, "verify toggle")

    assert analyzer.video_processing == "agentic"
    reasons = [call.args[1].get("reason") for call in record_event.call_args_list]
    assert "agentic_video_rejected" not in reasons


@pytest.mark.asyncio
async def test_transient_agentic_failure_flows_through_shared_retry(tmp_path):
    with patch("artemis.agents.video_analyzer.video_analyzer.settings.GOOGLE_API_KEY", None):
        analyzer = VideoAnalyzer(_context())
    interaction = _interaction(
        "v1_retry",
        [_function_call("call_1", {"summary": "s", "analysis": "a", "timeline_events": []})],
    )
    analyzer.client = _fake_client(
        [RuntimeError("503 provider unavailable"), _events(interaction, 1)]
    )
    analyzer.use_native_gemini = True
    analyzer.video_processing = "agentic"
    raw_video, controller = _extraction(tmp_path)

    with (
        patch(
            "artemis.agents.video_analyzer.video_analyzer.get_controller",
            return_value=controller,
        ),
        patch(
            "artemis.agents.video_analyzer.video_analyzer.compress_video_for_api",
            new=AsyncMock(return_value=raw_video),
        ),
        patch.object(analyzer, "upload_and_poll_file", new=AsyncMock(return_value=_UPLOADED)),
        patch(
            "artemis.agents.video_analyzer.video_analyzer.asyncio.sleep",
            new=AsyncMock(),
        ),
    ):
        result = await analyzer._exec_single_chunk(0.0, 5.0, "verify toggle")

    assert result == "[from 0.0s to 5.0s] Summary: s Analysis: a"
    assert analyzer.video_processing == "agentic"
    assert len(analyzer.client.aio.interactions.requests) == 2
