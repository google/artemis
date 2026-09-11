"""Backend selection and the helper-first / UIAutomator-fallback composite."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from artemis.clients.accessibility_client import AccessibilityClient, HelperUnavailable
from artemis.clients.screen_client_factory import (
    DeviceOfflineError,
    FallbackScreenClient,
    HierarchyBackend,
    create_screen_client,
    describe_backend,
    hierarchy_backend_sentence,
    hierarchy_backend_summary,
    resolve_backend,
)
from artemis.clients.ui_automator_client import UIAutomatorClient
from artemis.config.constants import ENV_ARTEMIS_HIERARCHY_BACKEND


# --------------------------------------------------------------------------- #
# resolve_backend / create_screen_client
# --------------------------------------------------------------------------- #


def test_resolve_backend_prefers_explicit_then_env_then_settings(monkeypatch):
    monkeypatch.delenv(ENV_ARTEMIS_HIERARCHY_BACKEND, raising=False)
    from artemis.config import settings

    monkeypatch.setattr(settings, "ARTEMIS_HIERARCHY_BACKEND", "uiautomator", raising=False)
    assert resolve_backend() is HierarchyBackend.UIAUTOMATOR

    monkeypatch.setenv(ENV_ARTEMIS_HIERARCHY_BACKEND, "Helper")
    assert resolve_backend() is HierarchyBackend.HELPER

    assert resolve_backend("auto") is HierarchyBackend.AUTO
    assert resolve_backend(HierarchyBackend.HELPER) is HierarchyBackend.HELPER


def test_resolve_backend_unknown_value_falls_back_to_auto(monkeypatch):
    monkeypatch.setenv(ENV_ARTEMIS_HIERARCHY_BACKEND, "maestro")
    assert resolve_backend() is HierarchyBackend.AUTO


def test_create_screen_client_per_backend():
    assert isinstance(create_screen_client("d", "uiautomator"), UIAutomatorClient)
    assert isinstance(create_screen_client("d", "helper"), AccessibilityClient)
    composite = create_screen_client("d", "auto")
    assert isinstance(composite, FallbackScreenClient)
    assert describe_backend(composite) is None  # nothing served yet
    assert describe_backend(UIAutomatorClient("d")) == "uiautomator"
    assert describe_backend(AccessibilityClient("d")) == "helper"


# --------------------------------------------------------------------------- #
# FallbackScreenClient
# --------------------------------------------------------------------------- #


@pytest.fixture
def composite():
    helper = MagicMock(spec=AccessibilityClient)
    u2 = MagicMock(spec=UIAutomatorClient)
    factory = MagicMock(return_value=u2)
    clock = {"now": 0.0, "state": "device"}
    client = FallbackScreenClient(
        "dev",
        helper=helper,
        uiautomator_factory=factory,
        retry_after=30.0,
        clock=lambda: clock["now"],
        state_probe=lambda _serial: clock["state"],
    )
    return client, helper, u2, factory, clock


def test_healthy_helper_never_constructs_uiautomator(composite):
    client, helper, _, factory, _ = composite
    helper.get_hierarchy.return_value = "<hierarchy/>"
    client.connect()
    assert client.get_hierarchy() == "<hierarchy/>"
    assert client.active_backend == "helper"
    factory.assert_not_called()


def test_connect_failure_degrades_to_uiautomator(composite):
    client, helper, u2, factory, _ = composite
    helper.connect.side_effect = HelperUnavailable("not installed")
    client.connect()
    factory.assert_called_once()
    u2.connect.assert_called_once()
    assert client.active_backend == "uiautomator"


def test_call_failure_falls_back_then_retries_helper_after_window(composite):
    client, helper, u2, _, clock = composite
    helper.get_hierarchy.side_effect = [RuntimeError("tunnel gone"), "<from-helper/>"]
    u2.get_hierarchy.return_value = "<from-u2/>"

    assert client.get_hierarchy() == "<from-u2/>"
    assert client.active_backend == "uiautomator"

    # Inside the retry window the helper is not touched again.
    clock["now"] = 10.0
    assert client.get_hierarchy() == "<from-u2/>"
    assert helper.get_hierarchy.call_count == 1

    # After the window the helper is tried again and wins.
    clock["now"] = 31.0
    assert client.get_hierarchy() == "<from-helper/>"
    assert client.active_backend == "helper"


def test_uiautomator_failure_propagates(composite):
    client, helper, u2, _, _ = composite
    helper.get_screen_data.side_effect = RuntimeError("helper down")
    u2.get_screen_data.side_effect = RuntimeError("u2 down too")
    with pytest.raises(RuntimeError, match="u2 down too"):
        client.get_screen_data()


def test_set_clipboard_goes_to_helper_first(composite):
    client, helper, u2, _, _ = composite
    helper.set_clipboard.return_value = True
    assert client.set_clipboard("x") is True
    u2.set_clipboard.assert_not_called()


def test_disconnect_releases_both_when_uiautomator_was_used(composite):
    client, helper, u2, _, _ = composite
    helper.connect.side_effect = HelperUnavailable("nope")
    client.connect()
    client.disconnect()
    helper.disconnect.assert_called_once()
    u2.disconnect.assert_called_once()
    assert client.active_backend is None


def test_strict_helper_backend_surfaces_errors():
    manager = MagicMock()
    manager.attach.side_effect = HelperUnavailable("not installed")
    client = AccessibilityClient("dev", manager=manager)
    with pytest.raises(HelperUnavailable):
        client.connect()


def test_offline_device_fails_fast_without_touching_uiautomator(composite):
    client, helper, u2, factory, clock = composite
    helper.get_screen_data.side_effect = RuntimeError("connection refused")
    clock["state"] = "unauthorized"
    with pytest.raises(DeviceOfflineError, match="unauthorized"):
        client.get_screen_data()
    factory.assert_not_called()

    clock["state"] = None
    with pytest.raises(DeviceOfflineError, match="does not list it"):
        client.get_screen_data()
    factory.assert_not_called()


def test_backend_history_and_listeners_track_every_switch(composite):
    client, helper, u2, _, clock = composite
    seen = []
    client.add_backend_listener(lambda prev, new, reason: seen.append((prev, new, reason)))
    helper.get_hierarchy.side_effect = [
        "<h/>",
        RuntimeError("tunnel gone"),
        "<h/>",
    ]
    u2.get_hierarchy.return_value = "<u2/>"

    client.get_hierarchy()  # helper
    client.get_hierarchy()  # helper fails -> uiautomator
    clock["now"] = 60.0
    client.get_hierarchy()  # helper recovers

    assert [(p, n) for p, n, _ in seen] == [
        (None, "helper"),
        ("helper", "uiautomator"),
        ("uiautomator", "helper"),
    ]
    assert seen[1][2] == "RuntimeError: tunnel gone"
    assert seen[2][2] == "helper recovered"
    assert [h["backend"] for h in client.backend_history] == ["helper", "uiautomator", "helper"]

    summary = hierarchy_backend_summary(client)
    assert summary["backend"] == "helper" and len(summary["switches"]) == 2
    sentence = hierarchy_backend_sentence(client, relative_time=lambda t: "T+00:10")
    assert sentence.startswith("UI hierarchy source: the Artemis accessibility helper")
    assert "switched to UIAutomator2 at T+00:10 because RuntimeError: tunnel gone" in sentence


def test_listener_errors_do_not_break_the_client(composite):
    client, helper, _, _, _ = composite
    client.add_backend_listener(lambda *_: (_ for _ in ()).throw(RuntimeError("boom")))
    helper.get_hierarchy.return_value = "<h/>"
    assert client.get_hierarchy() == "<h/>"


def test_sentence_is_none_before_first_use(composite):
    client, *_ = composite
    assert hierarchy_backend_sentence(client) is None
    assert hierarchy_backend_summary(client) is None


def test_helper_retry_releases_uiautomation_first(composite):
    """While our UIAutomator2 server lives the helper cannot bind; stop it before retrying."""
    client, helper, u2, _, clock = composite
    helper.get_hierarchy.side_effect = [RuntimeError("tunnel gone"), "<from-helper/>"]
    u2.get_hierarchy.return_value = "<from-u2/>"
    assert client.get_hierarchy() == "<from-u2/>"
    u2.stop_server.assert_not_called()  # inside the retry window nothing is touched

    clock["now"] = 31.0
    assert client.get_hierarchy() == "<from-helper/>"
    u2.stop_server.assert_called_once()


def test_disconnect_stops_the_uiautomator_server_it_started(composite):
    client, helper, u2, _, _ = composite
    helper.connect.side_effect = HelperUnavailable("nope")
    client.connect()
    client.disconnect()
    u2.disconnect.assert_called_once_with(stop_server=True)
