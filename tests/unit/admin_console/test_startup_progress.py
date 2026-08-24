from apps.admin_console.core.state import ServerState


def test_startup_progress_is_ordered_and_upserted_by_stage():
    state = ServerState()

    state.record_startup_progress(
        {
            "session_id": "session-1",
            "stage": "queued",
            "message": "Task queued",
            "timestamp": 1.0,
        }
    )
    state.record_startup_progress(
        {
            "session_id": "session-1",
            "stage": "device",
            "message": "Checking device",
            "timestamp": 2.0,
        }
    )
    state.record_startup_progress(
        {
            "session_id": "session-1",
            "stage": "device",
            "message": "Device connected",
            "timestamp": 3.0,
        }
    )

    events = state.get_startup_progress("session-1")
    assert [event["stage"] for event in events] == ["queued", "device"]
    assert events[-1]["message"] == "Device connected"


def test_startup_progress_snapshot_is_not_mutable_by_callers():
    state = ServerState()
    state.record_startup_progress(
        {
            "session_id": "session-1",
            "stage": "queued",
            "message": "Task queued",
            "timestamp": 1.0,
        }
    )

    snapshot = state.get_startup_progress("session-1")
    snapshot[0]["message"] = "changed"

    assert state.get_startup_progress("session-1")[0]["message"] == "Task queued"
