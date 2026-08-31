import asyncio
import logging

import pytest

from artemis.services import llm


@pytest.mark.asyncio
async def test_timeout_wrapper_cancels_llm_call_when_caller_is_cancelled():
    started = asyncio.Event()
    child_cancelled = asyncio.Event()

    async def pending_llm_call():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            child_cancelled.set()
            raise

    invocation = asyncio.create_task(
        llm.invoke_llm_with_timeout_message(
            pending_llm_call(),
            timeout_seconds=60,
        )
    )
    await started.wait()
    invocation.cancel()

    with pytest.raises(asyncio.CancelledError):
        await invocation

    assert child_cancelled.is_set()


class _RecordingEngine:
    current_step_id = "step-7"
    current_session_id = "session-1"

    def __init__(self):
        self.trace_kwargs = None
        self.published = None

    def record_trace(self, **kwargs):
        self.trace_kwargs = kwargs
        return "trace-1"

    def _publish(self, event_type, payload):
        self.published = (event_type, payload)


def test_pause_error_is_persisted_and_published(monkeypatch, tmp_path):
    engine = _RecordingEngine()
    monkeypatch.setattr(llm, "_CURRENT_DATA_ENGINE", engine)
    monkeypatch.setattr(llm.settings, "TRACES_PATH", str(tmp_path / "traces"))

    pause_file = llm._handle_llm_pause_and_resume(RuntimeError("503 high demand"))

    assert pause_file.read_text() == "LLM Error: 503 high demand"
    assert engine.trace_kwargs == {
        "type": "llm_call",
        "name": "llm_pause",
        "payload": {"error": "503 high demand", "pause": True},
        "step_id": "step-7",
        "status": "failed",
    }
    event_type, payload = engine.published
    assert event_type == "task_paused"
    assert payload["error"] == "503 high demand"
    assert payload["step_id"] == "step-7"
    assert isinstance(payload["timestamp"], float)


def test_provider_sdk_retry_is_persisted_and_published(monkeypatch):
    engine = _RecordingEngine()
    monkeypatch.setattr(llm, "_CURRENT_DATA_ENGINE", engine)
    record = logging.LogRecord(
        name="google_genai._api_client",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=(
            "Retrying google.genai request in 1.18 seconds as it raised "
            "ServerError: 503 UNAVAILABLE: high demand"
        ),
        args=(),
        exc_info=None,
    )

    request_token = llm._ACTIVE_LLM_REQUEST.set(
        {
            "request_id": "request-1",
            "provider": "google",
            "started_at": 100.0,
            "retries": [],
        }
    )
    try:
        llm._ProviderRetryTelemetryHandler().emit(record)
    finally:
        llm._ACTIVE_LLM_REQUEST.reset(request_token)

    assert engine.trace_kwargs["type"] == "llm_call"
    assert engine.trace_kwargs["name"] == "llm_retry"
    assert engine.trace_kwargs["status"] == "retrying"
    assert engine.trace_kwargs["payload"] == {
        "error": "ServerError: 503 UNAVAILABLE: high demand",
        "delay": 1.18,
        "provider": "google",
        "source": "provider_sdk",
        "recoverable": True,
        "request_id": "request-1",
        "scheduled_at": engine.trace_kwargs["payload"]["scheduled_at"],
    }
    event_type, payload = engine.published
    assert event_type == "llm_retrying"
    assert payload["trace_id"] == "trace-1"
    assert payload["delay"] == 1.18
    assert payload["error"].startswith("ServerError: 503")


def test_provider_sdk_retry_is_hidden_for_non_gemini_request(monkeypatch):
    engine = _RecordingEngine()
    monkeypatch.setattr(llm, "_CURRENT_DATA_ENGINE", engine)
    record = logging.LogRecord(
        name="google_genai._api_client",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Retrying request in 2 seconds as it raised 503 UNAVAILABLE",
        args=(),
        exc_info=None,
    )
    request_token = llm._ACTIVE_LLM_REQUEST.set(
        {
            "request_id": "request-openai",
            "provider": "openai",
            "started_at": 100.0,
            "retries": [],
        }
    )
    try:
        llm._ProviderRetryTelemetryHandler().emit(record)
    finally:
        llm._ACTIVE_LLM_REQUEST.reset(request_token)

    assert engine.trace_kwargs is None
    assert engine.published is None


def test_terminal_failure_aggregates_observed_retries_and_total_wait(monkeypatch, tmp_path):
    engine = _RecordingEngine()
    monkeypatch.setattr(llm, "_CURRENT_DATA_ENGINE", engine)
    monkeypatch.setattr(llm.settings, "TRACES_PATH", str(tmp_path / "traces"))
    monkeypatch.setattr(llm.time, "time", lambda: 120.0)
    retries = [
        {
            "error": "503 high demand",
            "delay": 10.0,
            "provider": "google",
            "source": "provider_sdk",
            "request_id": "request-2",
            "scheduled_at": 101.0,
            "timestamp": 101.0,
        }
    ]
    request_token = llm._ACTIVE_LLM_REQUEST.set(
        {
            "request_id": "request-2",
            "provider": "google",
            "started_at": 100.0,
            "retries": retries,
        }
    )
    try:
        llm._handle_llm_pause_and_resume(RuntimeError("503 high demand"))
    finally:
        llm._ACTIVE_LLM_REQUEST.reset(request_token)

    payload = engine.trace_kwargs["payload"]
    assert payload["request_id"] == "request-2"
    assert payload["provider"] == "google"
    assert payload["waited_seconds"] == 20.0
    assert payload["retries"] == retries
