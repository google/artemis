import logging

from artemis.services import llm


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

    llm._ProviderRetryTelemetryHandler().emit(record)

    assert engine.trace_kwargs == {
        "type": "llm_call",
        "name": "llm_retry",
        "payload": {
            "error": "ServerError: 503 UNAVAILABLE: high demand",
            "delay": 1.18,
            "provider": "google",
            "source": "provider_sdk",
            "recoverable": True,
        },
        "step_id": "step-7",
        "parent_trace_id": None,
        "status": "retrying",
    }
    event_type, payload = engine.published
    assert event_type == "llm_retrying"
    assert payload["trace_id"] == "trace-1"
    assert payload["delay"] == 1.18
    assert payload["error"].startswith("ServerError: 503")
