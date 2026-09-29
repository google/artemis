"""Session lifecycle: creation, reuse, device-lock contention, idle reaping."""

from unittest.mock import Mock

import pytest

from mcp_server.manual_session import ManualSession, ManualSessionRegistry
from artemis.runtime.device_lock import DeviceBusyError, DeviceExecutionLock


def _fake_executor_factory():
    calls: list[str | None] = []

    def factory(device_serial: str | None):
        calls.append(device_serial)
        return Mock(name=f"executor-{len(calls)}")

    factory.calls = calls
    return factory


def test_get_or_create_reuses_session_for_same_device():
    factory = _fake_executor_factory()
    registry = ManualSessionRegistry(executor_factory=factory)
    try:
        first = registry.get_or_create("dev-reuse")
        second = registry.get_or_create("dev-reuse")
        assert first is second
        assert factory.calls == ["dev-reuse"]
    finally:
        registry.end("dev-reuse")


def test_get_or_create_is_isolated_per_device():
    factory = _fake_executor_factory()
    registry = ManualSessionRegistry(executor_factory=factory)
    try:
        a = registry.get_or_create("dev-a")
        b = registry.get_or_create("dev-b")
        assert a is not b
        assert factory.calls == ["dev-a", "dev-b"]
    finally:
        registry.end("dev-a")
        registry.end("dev-b")


def test_get_or_create_none_serial_uses_default_key():
    factory = _fake_executor_factory()
    registry = ManualSessionRegistry(executor_factory=factory)
    try:
        first = registry.get_or_create(None)
        second = registry.get_or_create(None)
        assert first is second
    finally:
        registry.end(None)


def test_get_or_create_raises_when_device_locked_by_another_owner():
    other = DeviceExecutionLock(
        device_id="dev-busy", description="other Artemis task", session_id="other-task"
    )
    other.acquire(blocking=False)
    try:
        registry = ManualSessionRegistry(executor_factory=_fake_executor_factory())
        with pytest.raises(DeviceBusyError):
            registry.get_or_create("dev-busy")
    finally:
        other.release()


def test_end_releases_lock_and_allows_recreation():
    factory = _fake_executor_factory()
    registry = ManualSessionRegistry(executor_factory=factory)
    registry.get_or_create("dev-end")
    assert registry.end("dev-end") is True

    # The device lock must actually be released: another owner can now take it.
    other = DeviceExecutionLock(
        device_id="dev-end", description="other Artemis task", session_id="other-task"
    )
    other.acquire(blocking=False)
    other.release()

    registry.get_or_create("dev-end")
    registry.end("dev-end")
    assert factory.calls == ["dev-end", "dev-end"]


def test_end_on_unknown_device_is_not_an_error():
    registry = ManualSessionRegistry(executor_factory=_fake_executor_factory())
    assert registry.end("never-created") is False


def test_end_reports_true_when_the_call_itself_reaped_an_idle_session():
    # Regression: end() must not let its own _reap_idle() pass reap the
    # requested session before _end_key() runs, or the caller sees `ended:
    # false` for the exact call that released the device (google/artemis#114).
    factory = _fake_executor_factory()
    registry = ManualSessionRegistry(executor_factory=factory, idle_timeout_s=0.0)
    registry.get_or_create("dev-stale")
    assert registry.end("dev-stale") is True


def test_idle_session_is_reaped_on_next_registry_access():
    factory = _fake_executor_factory()
    registry = ManualSessionRegistry(executor_factory=factory, idle_timeout_s=0.0)
    registry.get_or_create("dev-idle")

    # idle_timeout_s=0.0 means the session is stale the instant any time passes;
    # the next access anywhere in the registry reaps it and creates a fresh one.
    registry.get_or_create("dev-idle")

    assert factory.calls == ["dev-idle", "dev-idle"]
    registry.end("dev-idle")


def test_manual_session_fields_default_empty():
    session = ManualSession(executor=Mock(), lock=Mock(), last_used=0.0)
    assert session.indexed_elements == []
    assert session.indexed_points == []
    assert session.latest_screenshot is None
    assert session.latest_ui_hierarchy is None
