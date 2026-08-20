import json

from apps.admin_console.database.repositories.step_repository import StepRepository


def test_legacy_pause_log_is_normalized_to_failed_llm_trace():
    repository = StepRepository()
    trace = {
        "trace_id": "pause-log",
        "type": "log",
        "name": "artemis.services.llm",
        "status": "success",
        "payload": json.dumps(
            {
                "message": (
                    "LLM Error: 503 UNAVAILABLE. {'error': {'message': 'High demand'}}. "
                    "Pausing execution to wait for resume signal..."
                )
            }
        ),
    }

    normalized = repository._normalize_display_trace(trace)

    assert normalized["type"] == "llm_call"
    assert normalized["name"] == "llm_pause"
    assert normalized["status"] == "failed"
    assert normalized["payload"]["pause"] is True
    assert normalized["payload"]["error"].startswith("503 UNAVAILABLE")


def test_unrelated_log_is_not_presented_as_llm_failure():
    repository = StepRepository()
    trace = {
        "trace_id": "ordinary-log",
        "type": "log",
        "name": "artemis.services.llm",
        "status": "success",
        "payload": json.dumps({"message": "LLM request completed"}),
    }

    normalized = repository._normalize_display_trace(trace)

    assert normalized["type"] == "log"
    assert normalized["status"] == "success"


def test_retrying_llm_payload_keeps_user_visible_retry_details():
    repository = StepRepository()
    trace = {
        "trace_id": "retry-1",
        "type": "llm_call",
        "name": "llm_retry",
        "status": "retrying",
        "payload": json.dumps(
            {
                "error": "503 UNAVAILABLE: high demand",
                "delay": 1.18,
                "provider": "google",
                "source": "provider_sdk",
                "recoverable": True,
                "internal_noise": "do not expose",
            }
        ),
    }

    normalized = repository._normalize_display_trace(trace)

    assert normalized["payload"] == {
        "error": "503 UNAVAILABLE: high demand",
        "delay": 1.18,
        "provider": "google",
        "source": "provider_sdk",
        "recoverable": True,
    }
