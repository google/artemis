"""The user is told which UI-hierarchy backend serves a run, everywhere they look.

Covers the agent's announcement fan-out (startup progress, log trace, session
device_info, status.json), the provisioning progress stage, the run_outcome
environment block, the `mobile_manage_task` field, the `artemis helper` CLI
(`--all`, status rendering) and the `artemis doctor` helper row.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from artemis.clients.screen_client_factory import FallbackScreenClient
from artemis.runtime import trace_store
from artemis.runtime.helper_manager import HelperSession, ProvisionResult
from artemis.sdk.agent import Agent


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _composite(helper_version: str = "1.1.2") -> FallbackScreenClient:
    helper = MagicMock()
    helper.session = HelperSession(
        serial="dev",
        local_port=41000,
        transport_id="1",
        version_code=4,
        version_name=helper_version,
        owns_forward=True,
    )
    helper.get_hierarchy.return_value = "<h/>"
    client = FallbackScreenClient(
        "dev", helper=helper, uiautomator_factory=MagicMock(), state_probe=lambda _s: "device"
    )
    return client


def _agent_with(client) -> Agent:
    agent = object.__new__(Agent)
    agent._ui_adb_client = client
    return agent


def _engine():
    engine = MagicMock()
    engine.current_session_id = "sess-1"
    engine.get_relative_time = lambda ts: "T+00:05"
    return engine


# --------------------------------------------------------------------------- #
# Agent announcement fan-out
# --------------------------------------------------------------------------- #


def test_announce_first_backend_reaches_every_sink(tmp_path, monkeypatch):
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path))
    trace_store.init_trace("sess-1", "goal", "flash")
    client = _composite()
    client.get_hierarchy()  # helper becomes active
    agent = _agent_with(client)
    engine = _engine()
    context = SimpleNamespace(data_engine=engine)

    with patch("artemis.sdk.agent.publish_startup_progress") as progress:
        agent._announce_hierarchy_backend(context, "sess-1", None, "helper", None)

    stage, message = progress.call_args.args[:2]
    assert stage == "hierarchy_backend"
    assert message == "UI hierarchy source: Artemis accessibility helper v1.1.2"
    assert progress.call_args.kwargs["backend"] == "helper"

    trace_call = engine.record_trace.call_args.kwargs
    assert trace_call["name"] == "hierarchy_backend" and trace_call["type"] == "log"
    assert trace_call["payload"]["helper_version"] == "1.1.2"
    assert trace_call["payload"]["level"] == "INFO"

    info = engine.update_session_device_info.call_args.kwargs
    assert info["hierarchy_backend"] == "helper"
    assert info["hierarchy_backend_note"].startswith("UI hierarchy source: the Artemis")

    status = trace_store.read_status("sess-1")
    assert status["hierarchy_backend"] == "helper"
    assert "accessibility helper v1.1.2" in status["hierarchy_backend_note"]


def test_announce_switch_is_a_warning_with_the_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path))
    client = _composite()
    agent = _agent_with(client)
    engine = _engine()
    context = SimpleNamespace(data_engine=engine)

    with patch("artemis.sdk.agent.publish_startup_progress") as progress:
        agent._announce_hierarchy_backend(
            context, "sess-1", "helper", "uiautomator", "HelperUnavailable: tunnel gone"
        )

    stage, message = progress.call_args.args[:2]
    assert stage == "hierarchy_backend_changed"
    assert message == (
        "UI hierarchy source switched from Artemis accessibility helper to UIAutomator2 "
        "because HelperUnavailable: tunnel gone"
    )
    assert engine.record_trace.call_args.kwargs["payload"]["level"] == "WARNING"
    # No status.json for this session: silently skipped, nothing raised.
    assert trace_store.read_status("sess-1") is None


def test_announce_survives_sink_failures(monkeypatch, tmp_path):
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path))
    client = _composite()
    agent = _agent_with(client)
    engine = _engine()
    engine.record_trace.side_effect = RuntimeError("db closed")
    with patch("artemis.sdk.agent.publish_startup_progress", side_effect=OSError("no ipc")):
        agent._announce_hierarchy_backend(
            SimpleNamespace(data_engine=engine), "sess-1", None, "uiautomator", None
        )


@pytest.mark.asyncio
async def test_connect_publishes_install_stage_and_registers_listener():
    client = _composite()

    def fake_connect(on_event=None):
        on_event("installing", {"serial": "dev", "version_name": "1.1.2", "to_version": 4})
        client._set_active("helper")

    client.connect = fake_connect  # type: ignore[method-assign]
    agent = _agent_with(client)
    engine = _engine()
    context = SimpleNamespace(data_engine=engine)

    with (
        patch("artemis.sdk.agent.publish_startup_progress") as progress,
        patch.object(trace_store, "update_trace_fields"),
    ):
        await agent._connect_screen_client(context, "sess-1")
        stages = [c.args[0] for c in progress.call_args_list]
        assert stages == ["uiautomator", "helper_install", "uiautomator_ready", "hierarchy_backend"]
        install_msg = progress.call_args_list[1].args[1]
        assert (
            "first time" in install_msg and "v1.1.2" in install_msg and "3 seconds" in install_msg
        )

        # A later fallback goes through the registered listener.
        progress.reset_mock()
        client._set_active("uiautomator", "RuntimeError: tunnel gone")
        assert progress.call_args.args[0] == "hierarchy_backend_changed"


@pytest.mark.asyncio
async def test_connect_without_provisioning_client_takes_no_callback():
    plain = MagicMock(spec=["connect", "get_hierarchy"])
    plain.connect = MagicMock()  # signature without on_event
    agent = _agent_with(plain)
    with (
        patch("artemis.sdk.agent.publish_startup_progress"),
        patch.object(trace_store, "update_trace_fields"),
    ):
        await agent._connect_screen_client(SimpleNamespace(data_engine=None), "sess-1")
    plain.connect.assert_called_once_with()


@pytest.mark.asyncio
async def test_reused_agent_records_backend_changes_only_for_current_session():
    client = _composite()
    agent = _agent_with(client)
    context = SimpleNamespace(data_engine=None)
    with (
        patch("artemis.sdk.agent.publish_startup_progress") as progress,
        patch.object(trace_store, "update_trace_fields"),
    ):
        await agent._connect_screen_client(context, "sess-1")
        client._set_active("uiautomator", "tunnel gone")
        client.disconnect()
        await agent._connect_screen_client(context, "sess-2")
        assert len(client.backend_history) == 1
        progress.reset_mock()
        client._set_active("uiautomator", "tunnel gone again")
        assert progress.call_count == 1
        assert progress.call_args.kwargs["session_id"] == "sess-2"


# --------------------------------------------------------------------------- #
# trace_store / engine / task manager
# --------------------------------------------------------------------------- #


def test_update_trace_fields_merges_and_ignores_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path))
    assert trace_store.update_trace_fields("nope", hierarchy_backend="helper") is None
    trace_store.init_trace("t1", "goal", "pro")
    data = trace_store.update_trace_fields("t1", hierarchy_backend="helper", extra=1)
    assert data["hierarchy_backend"] == "helper" and data["extra"] == 1
    assert data["task_desc"] == "goal"


def test_update_session_device_info_merges_fields():
    from artemis.data_engine.engine import DataEngine

    engine = object.__new__(DataEngine)
    engine.current_session_id = "s"
    session = SimpleNamespace(device_info={"device_id": "dev"})
    engine.storage = MagicMock()
    engine.storage.get_session.return_value = session
    engine.update_session_device_info(hierarchy_backend="helper", hierarchy_backend_note="n")
    engine.storage.update_session.assert_called_once_with(session)
    assert session.device_info == {
        "device_id": "dev",
        "hierarchy_backend": "helper",
        "hierarchy_backend_note": "n",
    }


def test_manage_task_status_exposes_hierarchy_backend(tmp_path, monkeypatch):
    from mcp_server.tools import task_manager

    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path))
    trace_store.init_trace("t9", "goal", "flash", device_serial="dev")
    trace_store.update_trace_fields(
        "t9", hierarchy_backend="uiautomator", hierarchy_backend_note="switched"
    )
    with patch.object(task_manager, "_reconcile_status", lambda *a, **k: None, create=True):
        result = task_manager.mobile_manage_task(action="status", trace_id="t9")
    assert result["hierarchy_backend"] == "uiautomator"
    assert result["hierarchy_backend_note"] == "switched"


# --------------------------------------------------------------------------- #
# run_outcome environment block
# --------------------------------------------------------------------------- #


def test_run_outcome_extra_carries_environment(tmp_path):
    from artemis.graph.checkpoints import read_run_outcome, write_run_outcome
    from artemis.clients.screen_client_factory import hierarchy_backend_summary

    client = _composite()
    client.get_hierarchy()
    client._set_active("uiautomator", "RuntimeError: gone")
    outcome = MagicMock()
    outcome.model_dump.return_value = {"task_status": "completed"}
    write_run_outcome(tmp_path, outcome, {"environment": hierarchy_backend_summary(client)})
    data = read_run_outcome(tmp_path)
    assert data["environment"]["backend"] == "uiautomator"
    assert data["environment"]["helper_version"] == "1.1.2"
    assert data["environment"]["switches"][0]["reason"] == "RuntimeError: gone"


# --------------------------------------------------------------------------- #
# CLI: artemis helper
# --------------------------------------------------------------------------- #


def _status(**overrides):
    base = {
        "package": "com.artemis.helper",
        "installed": True,
        "installed_version": 4,
        "bundled_version": 4,
        "bundled_apk_present": True,
        "outdated": False,
        "newer_than_bundled": False,
        "enabled": True,
        "forward_port": None,
        "tunnel": "probe",
        "reachable": True,
        "reported_version": 4,
        "transport_id": "1",
        "session": None,
    }
    base.update(overrides)
    return base


def test_helper_status_explains_probe_tunnel_and_newer_build(monkeypatch):
    from artemis.interfaces.cli.commands import helper as helper_cli

    monkeypatch.setattr(
        helper_cli.helper_manager,
        "status",
        lambda serial: _status(installed_version=9, newer_than_bundled=True),
    )
    result = CliRunner().invoke(helper_cli.helper_app, ["status", "--serial", "dev"])
    assert result.exit_code == 0, result.output
    assert "temporary forward" in result.output
    assert "newer than the bundled v4" in result.output


def test_helper_install_all_targets_idle_devices_only(monkeypatch):
    from artemis.interfaces.cli.commands import helper as helper_cli

    devices = [
        SimpleNamespace(serial="a", is_busy=False),
        SimpleNamespace(serial="b", is_busy=True),
        SimpleNamespace(serial="c", is_busy=False),
    ]
    monkeypatch.setattr(helper_cli.device_pool, "get_ready_devices", lambda: devices)
    calls = []

    def provision(serial, force=False, on_event=None):
        calls.append(serial)
        on_event("installing", {"version_name": "1.1.2"})
        return ProvisionResult(
            ok=True, action="installed", installed_version=4, bundled_version=4, enabled=True
        )

    monkeypatch.setattr(helper_cli.helper_manager, "provision", provision)
    result = CliRunner().invoke(helper_cli.helper_app, ["install", "--all"])
    assert result.exit_code == 0, result.output
    assert calls == ["a", "c"]
    assert "a: installing v1.1.2..." in result.output


def test_helper_install_prints_manual_path_on_enable_failure(monkeypatch):
    from artemis.interfaces.cli.commands import helper as helper_cli
    from artemis.runtime.helper_manager import MANUAL_ENABLE_PATH

    monkeypatch.setattr(
        helper_cli.helper_manager,
        "provision",
        lambda serial, force=False, on_event=None: ProvisionResult(
            ok=False,
            action="installed",
            installed_version=4,
            bundled_version=4,
            enabled=False,
            error=f"This device rejected enabling the accessibility service from adb. {MANUAL_ENABLE_PATH}.",
        ),
    )
    result = CliRunner().invoke(helper_cli.helper_app, ["install", "--serial", "dev"])
    assert result.exit_code == 1
    assert "Settings > Accessibility" in result.output


# --------------------------------------------------------------------------- #
# CLI: artemis doctor row
# --------------------------------------------------------------------------- #


def _adb_probe(serials):
    return SimpleNamespace(
        id="android_adb",
        metadata={"devices": [{"serial": s, "state": "device"} for s in serials]},
    )


def test_doctor_row_suggests_preinstall_when_missing(monkeypatch):
    from artemis.interfaces.cli.commands import doctor

    with patch(
        "artemis.runtime.helper_manager.helper_manager.status",
        return_value=_status(installed=False, reachable=False),
    ):
        row = doctor._helper_row([_adb_probe(["dev"])])
    assert row.status == "missing" and row.summary == "Not installed"
    assert "artemis helper install --serial dev" in row.detail
    assert "about 3 s" in row.detail


def test_doctor_row_ready_mentions_uninstall(monkeypatch):
    from artemis.interfaces.cli.commands import doctor

    with patch("artemis.runtime.helper_manager.helper_manager.status", return_value=_status()):
        row = doctor._helper_row([_adb_probe(["dev"])])
    assert row.status == "pass" and "v4 on dev" in row.summary
    assert "artemis helper uninstall --serial dev" in row.detail


def test_doctor_row_absent_for_multiple_devices_or_uiautomator_backend(monkeypatch):
    from artemis.interfaces.cli.commands import doctor

    assert doctor._helper_row([_adb_probe(["a", "b"])]) is None
    monkeypatch.setenv("ARTEMIS_HIERARCHY_BACKEND", "uiautomator")
    assert doctor._helper_row([_adb_probe(["a"])]) is None
