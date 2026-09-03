"""Tests for the LLM gateway: complete(), classified recovery, and fallback."""

from types import SimpleNamespace

from langchain_core.messages import AIMessage, AIMessageChunk
import pytest

from artemis.llm.reliability import LLMExhaustedError, LLMPermanentError
from artemis.services import llm as llm_service
from artemis.services.llm import (
    RobustChatModelWrapper,
    acomplete,
    acomplete_structured,
    with_fallback,
)


@pytest.fixture(autouse=True)
def _isolate_gateway_state(monkeypatch, tmp_path):
    llm_service._ENDPOINT_BREAKER._states.clear()
    llm_service._NON_STREAMING_ENDPOINTS.clear()
    monkeypatch.setattr(llm_service, "PAUSE_FILE", tmp_path / ".artemis_paused")
    monkeypatch.setattr(
        llm_service,
        "retry_policy_for",
        lambda category: SimpleNamespace(max_attempts=2, delay_for=lambda attempt: 0.0),
    )
    monkeypatch.setattr(llm_service.settings, "LLM_PAUSE_TIMEOUT_SECONDS", 1.0, raising=False)
    yield
    llm_service._ENDPOINT_BREAKER._states.clear()
    llm_service._NON_STREAMING_ENDPOINTS.clear()


@pytest.mark.asyncio
async def test_mid_stream_failure_retries_without_duplicating_chunks():
    class BreaksMidStream:
        def __init__(self):
            self.astream_calls = 0

        async def astream(self, *args, **kwargs):
            self.astream_calls += 1
            if self.astream_calls == 1:
                yield AIMessageChunk(content="Hello")
                raise RuntimeError("503 service unavailable")
            yield AIMessageChunk(content="Hello")
            yield AIMessageChunk(content=" world")

    base = BreaksMidStream()
    wrapper = RobustChatModelWrapper(base)
    result = await wrapper.complete([])

    assert base.astream_calls == 2
    # The partial first attempt was discarded entirely: no duplicated prefix.
    assert result.content == "Hello world"


@pytest.mark.asyncio
async def test_auth_error_fails_fast_without_retry_or_pause(tmp_path):
    class AuthFails:
        def __init__(self):
            self.astream_calls = 0
            self.ainvoke_calls = 0

        async def astream(self, *args, **kwargs):
            self.astream_calls += 1
            raise RuntimeError("401 unauthorized: invalid api key")
            yield  # pragma: no cover - makes this an async generator

        async def ainvoke(self, *args, **kwargs):
            self.ainvoke_calls += 1
            return AIMessage(content="should never happen")

    base = AuthFails()
    wrapper = RobustChatModelWrapper(base)
    with pytest.raises(LLMPermanentError):
        await wrapper.complete([])

    assert base.astream_calls == 1
    assert base.ainvoke_calls == 0
    assert not llm_service.PAUSE_FILE.exists()


@pytest.mark.asyncio
async def test_stream_unsupported_downgrades_loudly_and_is_remembered():
    class NoStreaming:
        def __init__(self):
            self.astream_calls = 0
            self.ainvoke_calls = 0

        async def astream(self, *args, **kwargs):
            self.astream_calls += 1
            raise NotImplementedError("streaming is not supported")
            yield  # pragma: no cover

        async def ainvoke(self, *args, **kwargs):
            self.ainvoke_calls += 1
            return AIMessage(content="ok")

    base = NoStreaming()
    wrapper = RobustChatModelWrapper(base)

    first = await wrapper.complete([])
    assert first.content == "ok"
    assert wrapper._endpoint_key() in llm_service._NON_STREAMING_ENDPOINTS

    second = await wrapper.complete([])
    assert second.content == "ok"
    # The capability was cached: streaming is not probed again.
    assert base.astream_calls == 1
    assert base.ainvoke_calls == 2


@pytest.mark.asyncio
async def test_exhausted_retries_pause_then_raise_on_deadline():
    class AlwaysUnavailable:
        def __init__(self):
            self.calls = 0

        async def astream(self, *args, **kwargs):
            self.calls += 1
            raise RuntimeError("503 service unavailable")
            yield  # pragma: no cover

    wrapper = RobustChatModelWrapper(AlwaysUnavailable())
    with pytest.raises(LLMExhaustedError):
        await wrapper.complete([])
    # The pause file was written for the pause window, then cleaned up.
    assert not llm_service.PAUSE_FILE.exists()


@pytest.mark.asyncio
async def test_with_fallback_takes_over_without_pausing():
    class AlwaysUnavailable:
        async def astream(self, *args, **kwargs):
            raise RuntimeError("503 service unavailable")
            yield  # pragma: no cover

    wrapper = RobustChatModelWrapper(AlwaysUnavailable())

    async def fallback_call():
        return AIMessage(content="fallback answer")

    result = await with_fallback(
        main_call=lambda: wrapper.complete([]),
        fallback_call=fallback_call,
    )
    assert result.content == "fallback answer"
    # Because a fallback existed, exhaustion handed over instead of pausing.
    assert not llm_service.PAUSE_FILE.exists()


@pytest.mark.asyncio
async def test_with_fallback_refuses_to_hide_bad_requests():
    class CodedError(Exception):
        code = 400

    calls = {"fallback": 0}

    async def main_call():
        raise CodedError("malformed tool schema")

    async def fallback_call():
        calls["fallback"] += 1
        return "hidden bug"

    with pytest.raises(CodedError):
        await with_fallback(main_call=main_call, fallback_call=fallback_call)
    assert calls["fallback"] == 0


@pytest.mark.asyncio
async def test_acomplete_structured_corrective_reask():
    class CorrectsItself:
        def __init__(self):
            self.calls = []

        async def ainvoke(self, messages, **kwargs):
            self.calls.append(messages)
            if len(self.calls) == 1:
                return AIMessage(content="oops, here is prose instead of JSON")
            return AIMessage(content='{"a": 1}')

    base = CorrectsItself()
    result = await acomplete_structured(base, [])
    assert result == {"a": 1}
    assert len(base.calls) == 2
    correction_text = str(base.calls[1][-1].content)
    assert "could not be parsed" in correction_text


@pytest.mark.asyncio
async def test_acomplete_compat_path_for_plain_models():
    class PlainModel:
        async def astream(self, *args, **kwargs):
            yield AIMessageChunk(content="a")
            yield AIMessageChunk(content="b")

    result = await acomplete(PlainModel(), [])
    assert result.content == "ab"

    class InvokeOnly:
        async def ainvoke(self, *args, **kwargs):
            return AIMessage(content="direct")

    result = await acomplete(InvokeOnly(), [])
    assert result.content == "direct"


@pytest.mark.asyncio
async def test_mid_stream_failure_records_stream_reset_payload(monkeypatch):
    recorded_events = []

    def mock_record_event(name, payload, *, status="retrying"):
        recorded_events.append((name, payload, status))

    monkeypatch.setattr(llm_service, "_record_llm_event", mock_record_event)

    class BreaksMidStream:
        def __init__(self):
            self.calls = 0

        async def astream(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                yield AIMessageChunk(content="Partial thoughts...")
                raise RuntimeError("503 high demand spike")
            yield AIMessageChunk(content="Recovered output")

    base = BreaksMidStream()
    wrapper = RobustChatModelWrapper(base)
    result = await wrapper.complete([])

    assert result.content == "Recovered output"
    resets = [e for e in recorded_events if e[0] == "llm_stream_reset"]
    assert len(resets) == 1
    event_name, payload, status = resets[0]
    assert event_name == "llm_stream_reset"
    assert payload["action"] == "discard"
    assert payload["reason"] == "mid_stream_failure"
    assert "stream_exec_id" in payload
    assert "lower API priority" in payload["message"]

