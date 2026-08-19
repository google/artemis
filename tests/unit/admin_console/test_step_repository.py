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
