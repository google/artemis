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

"""Unit tests for the mobile_diagnose MCP tool."""

import asyncio
from contextlib import ExitStack
import inspect
import shutil
import tempfile
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from artemis.core.diagnostics.emulator_manager import EmulatorLaunchStage
from artemis.core.diagnostics.probes.host_probe import IntegrationHostProbe
from artemis.core.diagnostics.schema import (
    DeviceInfo,
    ProbeAction,
    ProbeCategory,
    ProbeResult,
    ProbeStatus,
    SystemReadinessReport,
)
from artemis.runtime import trace_store
from artemis.runtime.device_lock import DeviceLockOwner
from mcp_server.tools import diagnose
from mcp_server.tools.diagnose import mobile_diagnose

_IDLE_EMULATOR = {
    "avd_name": None,
    "status": EmulatorLaunchStage.IDLE,
    "pid": None,
    "serial": None,
    "stage_message": "Ready to launch",
    "progress_percent": 0,
    "started_at": None,
    "elapsed_seconds": 0,
    "error": None,
    "logs": [],
    "can_retry": True,
}


def _emulator_state(status: EmulatorLaunchStage, avd: str = "Pixel_8", **extra) -> dict:
    state = dict(_IDLE_EMULATOR)
    state.update(
        {
            "avd_name": avd,
            "status": status,
            "pid": 4242,
            "stage_message": f"stage {status.value}",
            "progress_percent": 30,
            "started_at": time.time() - 12,
            "elapsed_seconds": 12,
            "logs": ["emulator: INFO: boot", "second log line"],
        }
    )
    state.update(extra)
    return state


@pytest.fixture
def temp_trace_env(monkeypatch):
    temp_dir = tempfile.mkdtemp()
    monkeypatch.setattr(trace_store, "TRACES_DIR", temp_dir)
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


def _probe(
    probe_id: str,
    status: ProbeStatus,
    *,
    blocker: bool = True,
    category: ProbeCategory = ProbeCategory.RUNTIME,
    summary: str = "",
    description: str = "",
    metadata: dict | None = None,
    actions: list[ProbeAction] | None = None,
) -> ProbeResult:
    return ProbeResult(
        id=probe_id,
        category=category,
        title=probe_id.replace("_", " ").title(),
        status=status,
        is_blocker=blocker,
        summary=summary or status.value,
        description=description or f"{probe_id} is {status.value}",
        metadata=metadata or {},
        actions=actions or [],
    )


def _credentials_metadata() -> dict:
    return {
        "providers": [
            {
                "provider": "google",
                "label": "Gemini",
                "masked": "AIza...abcd",
                "raw_key": "SECRET-GOOGLE",
                "key": "SECRET-GOOGLE",
            },
            {
                "provider": "openai",
                "label": "ChatGPT",
                "masked": "sk-...wxyz",
                "raw_key": "SECRET-OPENAI",
                "key": "SECRET-OPENAI",
            },
        ],
        "api_keys": {
            "google": "SECRET-GOOGLE",
            "gemini": "SECRET-GOOGLE",
            "openai": "SECRET-OPENAI",
            "ocr": "SECRET-OCR",
        },
        "current_key": "SECRET-GOOGLE",
        "current_gemini_key": "SECRET-GOOGLE",
        "has_ocr_key": True,
    }


def _healthy_probes() -> list[ProbeResult]:
    return [
        _probe("python_runtime", ProbeStatus.PASS),
        _probe("system_config", ProbeStatus.PASS),
        _probe("toolchain", ProbeStatus.PASS, blocker=False, category=ProbeCategory.TOOLCHAIN),
        _probe(
            "gemini_api_key",
            ProbeStatus.PASS,
            category=ProbeCategory.CREDENTIALS,
            metadata=_credentials_metadata(),
        ),
        _probe(
            "vision_ocr_key", ProbeStatus.PASS, blocker=False, category=ProbeCategory.CREDENTIALS
        ),
        _probe(
            "android_adb",
            ProbeStatus.PASS,
            category=ProbeCategory.DEVICE,
            metadata={
                "installed": True,
                "adb_keys": {"is_corrupted": False},
                "installed_avds": [],
                "devices": [
                    {
                        "serial": "pixel-1",
                        "state": "device",
                        "installed_packages": ["a", "b", "c"],
                    }
                ],
            },
        ),
    ]


def _no_device_probes(installed_avds: list[str] | None = None) -> list[ProbeResult]:
    probes = _healthy_probes()
    avds = installed_avds if installed_avds is not None else ["Pixel_8", "Tablet_API_35"]
    actions = [
        ProbeAction(
            action_type="command",
            label=f"Launch {avd}",
            payload=f"C:/Android/Sdk/emulator/emulator.exe -avd {avd}",
        )
        for avd in avds
    ] or [
        ProbeAction(
            action_type="command",
            label="Start Default Emulator",
            payload="emulator -avd Pixel_8_API_34",
        )
    ]
    actions.append(
        ProbeAction(
            action_type="hint",
            label="Connect via USB",
            payload="Connect your Android phone via USB cable and enable USB Debugging.",
        )
    )
    probes[5] = _probe(
        "android_adb",
        ProbeStatus.WARN,
        category=ProbeCategory.DEVICE,
        summary="No Device",
        description="ADB is ready, but no active Android device or emulator was detected.",
        metadata={
            "installed": True,
            "adb_keys": {"is_corrupted": False},
            "installed_avds": avds,
            "devices": [],
        },
        actions=actions,
    )
    return probes


def _host(status: ProbeStatus = ProbeStatus.PASS, **metadata) -> ProbeResult:
    base = {
        "os": "win32",
        "project_root": "/proj",
        "server_python": "/proj/.venv/bin/python",
        "runner_python": "/proj/.venv/bin/python",
        "venv_python": "/proj/.venv/bin/python",
        "venv_exists": True,
        "interpreter_matches_venv": True,
        "env_file": "/proj/.env",
        "env_file_exists": True,
        "traces_dir": "/proj/traces",
        "traces_dir_writable": True,
        "traces_dir_error": None,
        "daemon": {
            "host": "127.0.0.1",
            "port": 8000,
            "standalone_forced": False,
            "reachable": False,
            "port_in_use": False,
            "port_held_by_other_process": False,
            "log_path": "/logs/daemon.log",
        },
    }
    base.update(metadata)
    # Mirrors the real probe: a WARN there degrades, it does not block.
    return _probe("integration_host", status, blocker=status is not ProbeStatus.WARN, metadata=base)


def _report(probes: list[ProbeResult], active_device: DeviceInfo | None = None):
    blockers = [p for p in probes if p.is_blocker]
    passed = [p for p in blockers if p.status is ProbeStatus.PASS]
    return SystemReadinessReport(
        overall_ready=len(blockers) == len(passed),
        blocker_count=len(blockers),
        passed_blocker_count=len(passed),
        probes=probes,
        active_device=active_device,
        os_type="linux",
        timestamp=time.time(),
    )


def _owner(device_id: str = "pixel-1", session_id: str = "trace-123") -> DeviceLockOwner:
    return DeviceLockOwner(
        pid=777,
        process_created_at=1.0,
        token="tok-1",
        device_id=device_id,
        description="Open settings and toggle wifi",
        acquired_at="2026-09-09T10:00:00+00:00",
        session_id=session_id,
        ingress="mcp",
        lock_scope=None,
    )


def _run(
    probes,
    host=None,
    *,
    report=None,
    run_all=None,
    emulator_status=None,
    launch=None,
    active_owners=None,
    queued=None,
    cleanup=0,
    validate=None,
    smoke=None,
    helper_status=None,
    helper_provision=None,
    backend="auto",
    **kwargs,
):
    """Run the tool with every side-effecting collaborator stubbed."""
    with ExitStack() as stack:
        fake_helper = MagicMock()
        fake_helper.status = MagicMock(
            side_effect=lambda serial: dict(helper_status or _helper_healthy(serial))
        )
        fake_helper.provision = helper_provision or MagicMock(return_value=_provision_ok())
        stack.enter_context(patch.object(diagnose, "helper_manager", fake_helper))
        stack.enter_context(patch.object(diagnose, "_hierarchy_backend", lambda: backend))
        kwargs["_fake_helper"] = fake_helper
        stack.enter_context(
            patch.object(
                diagnose.readiness_engine,
                "run_all",
                run_all or AsyncMock(return_value=report or _report(probes)),
            )
        )
        stack.enter_context(
            patch.object(IntegrationHostProbe, "probe", AsyncMock(return_value=host or _host()))
        )
        stack.enter_context(
            patch.object(
                diagnose.readiness_engine,
                "get_emulator_status",
                MagicMock(return_value=emulator_status or dict(_IDLE_EMULATOR)),
            )
        )
        stack.enter_context(
            patch.object(
                diagnose.readiness_engine,
                "launch_emulator",
                launch or AsyncMock(return_value=_emulator_state(EmulatorLaunchStage.STARTING)),
            )
        )
        stack.enter_context(
            patch.object(
                diagnose.DeviceExecutionLock,
                "get_active_owners",
                MagicMock(return_value=active_owners or {}),
            )
        )
        stack.enter_context(
            patch.object(
                diagnose.DeviceExecutionLock,
                "get_queued_tasks",
                MagicMock(return_value=queued or []),
            )
        )
        stack.enter_context(
            patch.object(
                diagnose.DeviceExecutionLock,
                "cleanup_stale_locks",
                cleanup if isinstance(cleanup, MagicMock) else MagicMock(return_value=cleanup),
            )
        )
        stack.enter_context(
            patch.object(
                diagnose,
                "validate_api_key",
                validate or AsyncMock(return_value=(True, "verified")),
            )
        )
        stack.enter_context(
            patch.object(
                diagnose,
                "_device_smoke_test",
                smoke or AsyncMock(return_value=_smoke_ok()),
            )
        )
        fake_helper = kwargs.pop("_fake_helper")
        result = asyncio.run(mobile_diagnose(**kwargs))
        result["_fake_helper"] = fake_helper
        return result


def _helper_healthy(serial: str = "pixel-1") -> dict:
    return {
        "package": "com.artemis.helper",
        "installed": True,
        "installed_version": 2,
        "bundled_version": 2,
        "bundled_apk_present": True,
        "outdated": False,
        "enabled": True,
        "forward_port": 41234,
        "reachable": True,
        "reported_version": 2,
        "transport_id": "7",
        "session": None,
    }


def _provision_ok():
    from artemis.runtime.helper_manager import ProvisionResult

    return ProvisionResult(
        ok=True, action="installed", installed_version=2, bundled_version=2, enabled=True
    )


def _smoke_ok(serial: str = "pixel-1") -> dict:
    return {
        "ok": True,
        "serial": serial,
        "elapsed_seconds": 3.2,
        "screenshot_bytes": 120_000,
        "element_count": 42,
        "error": None,
        "fix": [],
    }


# --------------------------------------------------------------------------- #
# Schema / shape
# --------------------------------------------------------------------------- #


def test_tool_signature_and_registration():
    sig = inspect.signature(mobile_diagnose)
    assert sig.parameters["attempt_fix"].default is False
    assert sig.parameters["device_serial"].default is None
    assert sig.parameters["launch_avd"].default is None
    assert sig.parameters["verify_credentials"].default is False
    assert sig.parameters["probe_device"].default is False
    doc = mobile_diagnose.__doc__ or ""
    for field in (
        "next_steps",
        "checks",
        "host",
        "device",
        "emulator",
        "tasks",
        "credentials",
        "device_probe",
        "fixes_applied",
        "logs",
        "launch_avd",
        "verify_credentials",
        "probe_device",
    ):
        assert field in doc

    from mcp_server.base import mcp

    tool_names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert "mobile_diagnose" in tool_names


def test_ready_environment_reports_ready_with_slim_shape(temp_trace_env):
    device = DeviceInfo(
        serial="pixel-1",
        model="Pixel 8",
        android_version="15",
        is_locked=False,
        installed_packages=["a", "b"],
        screen_resolution="1080x2400",
    )
    result = _run(_healthy_probes(), report=_report(_healthy_probes(), active_device=device))

    assert result["verdict"] == "ready"
    assert "overall_ready" not in result
    assert "active_device" not in result
    assert result["next_steps"] == [
        "Environment is ready. Use mobile_run_task to delegate a task; pass device_serial "
        "when more than one device is attached."
    ]
    assert "SECRET" not in repr(result)

    # Passing checks are one line each: no detail / fix / facts.
    for check in result["checks"]:
        assert check["status"] == "pass"
        assert set(check) == {"id", "title", "status", "required", "summary"}

    # Fix order: runtime first, then host, credentials, device, toolchain.
    assert [c["id"] for c in result["checks"]][:5] == [
        "python_runtime",
        "system_config",
        "integration_host",
        "gemini_api_key",
        "android_adb",
    ]
    assert result["device"] == {
        "serial": "pixel-1",
        "state": "device",
        "model": "Pixel 8",
        "android_version": "15",
        "is_locked": False,
        "is_emulator": False,
        "accessibility_helper": {**_helper_healthy(), "serial": "pixel-1", "backend": "auto"},
    }
    assert result["host"] == {
        "server_python": "/proj/.venv/bin/python",
        "runner_python": "/proj/.venv/bin/python",
        "interpreter_matches_venv": True,
        "env_file": "/proj/.env",
        "env_file_exists": True,
        "traces_dir": "/proj/traces",
        "daemon": {
            "port": 8000,
            "reachable": False,
            "port_held_by_other_process": False,
            "log_path": "/logs/daemon.log",
        },
    }
    assert result["emulator"] is None
    assert result["credentials"] is None
    assert result["device_probe"] is None
    assert result["tasks"] == {"active": [], "queued": []}
    assert result["logs"]["mcp_stderr_log"].endswith("mcp_stderr.log")
    assert result["logs"]["daemon_log"] == "/logs/daemon.log"


def test_host_mcp_client_is_forwarded_when_probe_reports_it(temp_trace_env):
    result = _run(_healthy_probes(), host=_host(mcp_client={"name": "cursor", "key": "SECRET"}))
    assert result["host"]["mcp_client"] == {"name": "cursor"}
    assert result["device"] is None


def test_failing_checks_keep_scrubbed_facts_and_fix_list(temp_trace_env):
    probes = _healthy_probes()
    probes[3].status = ProbeStatus.WARN
    probes[5].status = ProbeStatus.WARN
    result = _run(probes)

    creds = next(c for c in result["checks"] if c["id"] == "gemini_api_key")
    assert creds["category"] == "auth"
    assert creds["facts"]["providers"] == [
        {"provider": "google", "label": "Gemini", "masked": "AIza...abcd"},
        {"provider": "openai", "label": "ChatGPT", "masked": "sk-...wxyz"},
    ]
    assert "api_keys" not in creds["facts"]
    assert creds["fix"] == []
    assert "SECRET" not in repr(result)

    adb = next(c for c in result["checks"] if c["id"] == "android_adb")
    device = adb["facts"]["devices"][0]
    assert device["installed_package_count"] == 3
    assert "installed_packages" not in device


def test_optional_toolchain_failure_is_degraded(temp_trace_env):
    probes = _healthy_probes()
    probes[2] = _probe(
        "toolchain",
        ProbeStatus.FAIL,
        blocker=False,
        category=ProbeCategory.TOOLCHAIN,
        summary="Missing scrcpy",
        description="scrcpy is not installed; video capture is disabled.",
        actions=[
            ProbeAction(action_type="command", label="Install", payload="brew install scrcpy")
        ],
    )
    result = _run(probes)

    assert result["verdict"] == "degraded"
    assert result["next_steps"][0] == (
        "[OPTIONAL] Toolchain: scrcpy is not installed; video capture is disabled."
    )
    assert "  Run: brew install scrcpy" in result["next_steps"]


# --------------------------------------------------------------------------- #
# Command hygiene
# --------------------------------------------------------------------------- #


def test_unauthorized_device_is_blocked_with_actionable_steps(temp_trace_env):
    probes = _healthy_probes()
    probes[5] = _probe(
        "android_adb",
        ProbeStatus.WARN,
        category=ProbeCategory.DEVICE,
        summary="Device Unauthorized",
        description="Device detected (abc), but USB debugging is not yet authorized.",
        metadata={
            "installed": True,
            "adb_keys": {"is_corrupted": False},
            "installed_avds": [],
            "devices": [{"serial": "abc", "state": "unauthorized"}],
        },
        actions=[
            ProbeAction(action_type="hint", label="Authorize", payload="Tap Allow on the phone."),
            ProbeAction(
                action_type="command",
                label="Restart ADB",
                payload=r"C:\sdk\platform-tools\adb.exe kill-server && C:\sdk\platform-tools\adb.exe start-server",
            ),
            ProbeAction(action_type="link", label="Docs", payload="https://example.test/adb"),
        ],
    )
    result = _run(probes)

    assert result["verdict"] == "blocked"
    steps = result["next_steps"]
    assert steps[0] == (
        "[REQUIRED] Android Adb: Device detected (abc), but USB debugging is not yet authorized."
    )
    assert "  Guidance: Tap Allow on the phone." in steps
    # `&&` chains are split into consecutive Run lines (PowerShell 5.1 cannot parse &&).
    first = steps.index(r"  Run: C:\sdk\platform-tools\adb.exe kill-server")
    assert steps[first + 1] == r"  Run: C:\sdk\platform-tools\adb.exe start-server"
    assert not any("&&" in s for s in steps)
    assert "  Docs: https://example.test/adb" in steps
    assert any("mobile_diagnose(attempt_fix=true)" in s for s in steps)
    assert steps[-1] == "After each fix, call mobile_diagnose again until verdict is 'ready'."
    assert "Attention: Android Adb: Device Unauthorized" in result["summary"]


def test_emulator_launch_commands_become_launch_avd_guidance(temp_trace_env):
    result = _run(_no_device_probes(["Pixel_8", "Tablet_API_35"]))
    steps = result["next_steps"]

    assert result["verdict"] == "blocked"
    assert not any(s.startswith("  Run:") and "-avd" in s for s in steps)
    assert (
        '  Guidance: Call mobile_diagnose(launch_avd="Pixel_8") to start it in the background.'
        in steps
    )
    assert (
        '  Guidance: Call mobile_diagnose(launch_avd="Tablet_API_35") to start it in the '
        "background." in steps
    )
    # Explicit proposal for the first installed AVD when nothing is ready and nothing boots.
    assert any(
        s.startswith('No device is ready. Call mobile_diagnose(launch_avd="Pixel_8")')
        and "Tablet_API_35" in s
        for s in steps
    )
    assert "  Guidance: Connect your Android phone via USB cable and enable USB Debugging." in steps


def test_default_emulator_command_without_installed_avd_is_not_run(temp_trace_env):
    result = _run(_no_device_probes([]))
    steps = result["next_steps"]

    assert not any(s.startswith("  Run:") for s in steps)
    assert any("No AVD named 'Pixel_8_API_34' is installed" in s for s in steps)
    assert not any(
        s.startswith("No device is ready. Call mobile_diagnose(launch_avd=") for s in steps
    )


# --------------------------------------------------------------------------- #
# Credentials guidance
# --------------------------------------------------------------------------- #


def test_missing_credentials_point_to_env_file_not_chat(temp_trace_env):
    probes = _healthy_probes()
    probes[3] = _probe(
        "gemini_api_key",
        ProbeStatus.FAIL,
        category=ProbeCategory.CREDENTIALS,
        summary="Key Missing",
        actions=[
            ProbeAction(action_type="command", label="Init", payload="artemis init"),
            ProbeAction(action_type="link", label="Key", payload="https://aistudio.google.com"),
        ],
    )
    result = _run(probes, host=_host(env_file="/home/u/artemis/.env"))

    steps = result["next_steps"]
    assert result["verdict"] == "blocked"
    assert not any(s.strip() == "Run: artemis init" for s in steps)
    assert any("/home/u/artemis/.env" in s and "Never ask them to paste" in s for s in steps)
    assert any("interactive" in s for s in steps)
    assert steps[-1].startswith("After changing the env file")


def test_host_problems_come_before_credentials_and_request_restart(temp_trace_env):
    """A host WARN (interpreter mismatch, missing venv, squatted port) is a
    degradation the runner works around: the verdict is degraded, never
    blocked, so a pip/conda checkout is not stuck in a diagnose loop."""
    host = _host(ProbeStatus.WARN, summary="Host Warning")
    host.actions = [
        ProbeAction(
            action_type="command", label="Regen", payload="uv run artemis mcp --install claude"
        )
    ]
    result = _run(_healthy_probes(), host=host)

    steps = result["next_steps"]
    assert result["verdict"] == "degraded"
    assert steps[0].startswith("[OPTIONAL] Integration Host")
    assert "  Run: uv run artemis mcp --install claude" in steps
    assert "restart the MCP server" in steps[-1]


def test_unwritable_traces_dir_blocks(temp_trace_env):
    host = _host(ProbeStatus.FAIL, summary="Host Misconfigured", traces_dir_writable=False)
    result = _run(_healthy_probes(), host=host)
    assert result["verdict"] == "blocked"
    assert result["next_steps"][0].startswith("[REQUIRED] Integration Host")


# --------------------------------------------------------------------------- #
# Requested device
# --------------------------------------------------------------------------- #


def test_requested_device_missing_blocks_even_when_environment_is_ready(temp_trace_env):
    result = _run(_healthy_probes(), device_serial=" emulator-5554 ")

    assert result["verdict"] == "blocked"
    assert result["next_steps"][0].startswith(
        "[REQUIRED] Requested device 'emulator-5554' is not attached. Attached: pixel-1 (device)."
    )


def test_requested_device_attached_and_ready_stays_ready(temp_trace_env):
    result = _run(_healthy_probes(), device_serial="pixel-1")
    assert result["verdict"] == "ready"


def test_multiple_ready_devices_ask_user_to_choose(temp_trace_env):
    probes = _healthy_probes()
    probes[5].metadata["devices"].append({"serial": "pixel-2", "state": "device"})
    result = _run(probes)

    assert result["verdict"] == "ready"
    assert any(
        s.startswith("Several devices are ready") and "pixel-2" in s for s in result["next_steps"]
    )


# --------------------------------------------------------------------------- #
# launch_avd
# --------------------------------------------------------------------------- #


def test_launch_avd_starts_installed_emulator_and_reports_compact_state(temp_trace_env):
    launch = AsyncMock(return_value=_emulator_state(EmulatorLaunchStage.STARTING))
    result = _run(_no_device_probes(["Pixel_8"]), launch=launch, launch_avd=" Pixel_8 ")

    launch.assert_awaited_once_with("Pixel_8")
    assert result["emulator"] == {
        "avd_name": "Pixel_8",
        "status": "starting",
        "stage_message": "stage starting",
        "error": None,
        "serial": None,
        "elapsed_seconds": 12,
        "progress_percent": 30,
    }
    assert "logs" not in result["emulator"]
    steps = result["next_steps"]
    launch_line = next(s for s in steps if s.startswith("Emulator 'Pixel_8' is starting"))
    assert "1-3 minutes" in launch_line
    assert "60 seconds" in launch_line
    assert "do not pass launch_avd again" in launch_line
    # A launch that was just started supersedes the launch proposal and the attempt_fix nudge.
    assert not any(
        s.startswith("No device is ready. Call mobile_diagnose(launch_avd=") for s in steps
    )
    assert not any("mobile_diagnose(attempt_fix=true)" in s for s in steps)


def test_launch_avd_rejects_uninstalled_avd_without_launching(temp_trace_env):
    launch = AsyncMock()
    result = _run(_no_device_probes(["Pixel_8", "Tablet_API_35"]), launch=launch, launch_avd="Nope")

    launch.assert_not_awaited()
    assert result["emulator"] is None
    assert any(
        s.startswith("[REQUIRED] AVD 'Nope' is not installed") and "Pixel_8, Tablet_API_35" in s
        for s in result["next_steps"]
    )


def test_launch_avd_does_not_relaunch_while_booting(temp_trace_env):
    launch = AsyncMock()
    booting = _emulator_state(EmulatorLaunchStage.BOOTING)
    result = _run(
        _no_device_probes(["Pixel_8"]),
        launch=launch,
        emulator_status=booting,
        launch_avd="Pixel_8",
    )

    launch.assert_not_awaited()
    assert result["emulator"]["status"] == "booting"
    assert any(
        s.startswith("Emulator 'Pixel_8' is already booting") and "60 seconds" in s
        for s in result["next_steps"]
    )


def test_launch_in_progress_replaces_connect_device_advice(temp_trace_env):
    result = _run(
        _no_device_probes(["Pixel_8"]),
        emulator_status=_emulator_state(EmulatorLaunchStage.WAITING_FOR_ADB),
    )
    steps = result["next_steps"]

    assert result["verdict"] == "blocked"
    assert result["emulator"]["status"] == "waiting_for_adb"
    assert any(
        s.startswith("  Guidance: Emulator 'Pixel_8' is waiting_for_adb")
        and "instead of asking the user to connect a device" in s
        for s in steps
    )
    assert not any("Connect your Android phone" in s for s in steps)
    assert not any("mobile_diagnose(attempt_fix=true)" in s for s in steps)
    assert not any("launch_avd=" in s for s in steps)


def test_launch_avd_failure_is_required_step(temp_trace_env):
    failed = _emulator_state(
        EmulatorLaunchStage.FAILED, error="emulator binary crashed", stage_message="Failed"
    )
    result = _run(
        _no_device_probes(["Pixel_8"]), launch=AsyncMock(return_value=failed), launch_avd="Pixel_8"
    )
    assert result["emulator"]["status"] == "failed"
    assert any(
        s == "[REQUIRED] Emulator 'Pixel_8' failed to launch: emulator binary crashed."
        for s in result["next_steps"]
    )


# --------------------------------------------------------------------------- #
# verify_credentials
# --------------------------------------------------------------------------- #


def test_verify_credentials_reports_every_provider_without_leaking_keys(temp_trace_env):
    validate = AsyncMock(return_value=(True, "verified"))
    result = _run(_healthy_probes(), validate=validate, verify_credentials=True)

    assert result["verdict"] == "ready"
    assert result["credentials"] == [
        {"provider": "google", "label": "Gemini", "valid": True, "message": "verified"},
        {"provider": "openai", "label": "ChatGPT", "valid": True, "message": "verified"},
        {"provider": "ocr", "label": "Vision OCR", "valid": True, "message": "verified"},
    ]
    called = {call.args[0]: call.args[1] for call in validate.await_args_list}
    assert called == {"google": "SECRET-GOOGLE", "openai": "SECRET-OPENAI", "ocr": "SECRET-OCR"}
    assert all(call.kwargs["timeout"] == 12.0 for call in validate.await_args_list)
    assert "SECRET" not in repr(result)


def test_verify_credentials_calls_endpoint_providers_by_url(temp_trace_env):
    """A custom / Ollama / vLLM entry carries its base URL as the "key": the
    check must hit that endpoint (no auth header) instead of validating the
    URL as if it were an API key."""
    validate = AsyncMock(return_value=(True, "verified"))
    cred = _probe(
        "gemini_api_key",
        ProbeStatus.PASS,
        category=ProbeCategory.CREDENTIALS,
        metadata={
            "providers": [
                {
                    "provider": "ollama",
                    "label": "Local Ollama",
                    "raw_key": "http://localhost:11434/v1",
                },
                {"provider": "google", "label": "Gemini", "raw_key": "SECRET-GOOGLE"},
            ],
            "api_keys": {},
        },
    )
    probes = [p for p in _healthy_probes() if p.id != "gemini_api_key"] + [cred]
    _run(probes, validate=validate, verify_credentials=True)

    calls = {call.args[0]: call for call in validate.await_args_list}
    assert calls["ollama"].args[1] == "EMPTY"
    assert calls["ollama"].kwargs["base_url"] == "http://localhost:11434/v1"
    assert calls["google"].args[1] == "SECRET-GOOGLE"
    assert "base_url" not in calls["google"].kwargs


def test_invalid_primary_credential_blocks_and_redacts_message(temp_trace_env):
    async def _validate(provider, api_key, timeout=12.0):
        if provider == "google":
            return False, f"Gemini API verification failed (400): key {api_key} invalid"
        if provider == "ocr":
            raise RuntimeError("boom SECRET-OCR")
        return True, "ok"

    result = _run(
        _healthy_probes(),
        host=_host(env_file="/home/u/.env"),
        validate=AsyncMock(side_effect=_validate),
        verify_credentials=True,
    )

    assert result["verdict"] == "blocked"
    assert "SECRET" not in repr(result)
    google, openai, ocr = result["credentials"]
    assert google["valid"] is False and "***" in google["message"]
    assert openai["valid"] is True
    assert ocr["valid"] is False and ocr["message"].startswith("verification raised RuntimeError")
    steps = result["next_steps"]
    required = next(s for s in steps if s.startswith("[REQUIRED] Gemini API key (google)"))
    assert "failed live verification" in required
    assert steps[steps.index(required) + 1].startswith("  Ask the user to add a provider key")
    assert any("/home/u/.env" in s for s in steps)
    assert any(s.startswith("[OPTIONAL] Vision OCR API key (ocr)") for s in steps)
    assert steps[-1].startswith("After changing the env file")
    assert "Gemini key verification" in result["summary"]


def test_invalid_secondary_credential_only_degrades(temp_trace_env):
    async def _validate(provider, api_key, timeout=12.0):
        return (provider != "openai", "ok" if provider != "openai" else "401 unauthorized")

    result = _run(
        _healthy_probes(), validate=AsyncMock(side_effect=_validate), verify_credentials=True
    )
    assert result["verdict"] == "degraded"
    assert any(s.startswith("[OPTIONAL] ChatGPT API key (openai)") for s in result["next_steps"])


# --------------------------------------------------------------------------- #
# Task / lock state
# --------------------------------------------------------------------------- #


def test_tasks_surface_active_and_queued_and_busy_device_step(temp_trace_env):
    queued_ticket = {
        "session_id": "trace-456",
        "goal": "Queued goal",
        "device_id": "pixel-1",
        "device_serial": "pixel-1",
        "adb_endpoint_id": None,
        "pid": 888,
        "token": "tok-2",
        "ingress": "cli",
        "status": "pending",
        "created_at": 1700000000.0,
        "start_time": 1700000000.0,
    }
    result = _run(_healthy_probes(), active_owners={"pixel-1": _owner()}, queued=[queued_ticket])

    assert result["tasks"] == {
        "active": [
            {
                "device": "pixel-1",
                "session_id": "trace-123",
                "pid": 777,
                "description": "Open settings and toggle wifi",
                "ingress": "mcp",
                "started_at": "2026-09-09T10:00:00+00:00",
            }
        ],
        "queued": [
            {
                "device": "pixel-1",
                "session_id": "trace-456",
                "pid": 888,
                "description": "Queued goal",
                "ingress": "cli",
                "created_at": 1700000000.0,
            }
        ],
    }
    assert result["verdict"] == "ready"
    busy = next(s for s in result["next_steps"] if s.startswith("Device 'pixel-1' is busy"))
    assert "task trace-123 (pid 777)" in busy
    assert 'mobile_manage_task(action="stop", trace_id="trace-123")' in busy


def test_busy_step_only_for_the_targeted_device(temp_trace_env):
    probes = _healthy_probes()
    probes[5].metadata["devices"].append({"serial": "pixel-2", "state": "device"})

    result = _run(probes, active_owners={"pixel-2": _owner("pixel-2")}, device_serial="pixel-1")
    assert not any("is busy" in s for s in result["next_steps"])

    result = _run(probes, active_owners={"pixel-2": _owner("pixel-2")}, device_serial="pixel-2")
    assert any(s.startswith("Device 'pixel-2' is busy") for s in result["next_steps"])

    # Two ready devices and no requested serial: nothing to single out.
    result = _run(probes, active_owners={"pixel-2": _owner("pixel-2")})
    assert not any("is busy" in s for s in result["next_steps"])


# --------------------------------------------------------------------------- #
# attempt_fix
# --------------------------------------------------------------------------- #


def test_attempt_fix_heals_corrupted_keys_then_rechecks(temp_trace_env):
    broken = _healthy_probes()
    broken[5].metadata["adb_keys"] = {"is_corrupted": True}
    broken[5].status = ProbeStatus.WARN
    broken[5].summary = "ADB Key Corrupted"
    healthy = _healthy_probes()

    run_all = AsyncMock(side_effect=[_report(broken), _report(healthy)])
    heal = AsyncMock(return_value={"success": True, "message": "keys regenerated"})
    restart = AsyncMock()
    with (
        patch.object(diagnose.readiness_engine, "run_all", run_all),
        patch.object(diagnose.readiness_engine, "heal_adb_keys", heal),
        patch.object(diagnose.readiness_engine, "restart_adb_server", restart),
        patch.object(diagnose.DeviceExecutionLock, "cleanup_stale_locks", return_value=0),
        patch.object(diagnose.DeviceExecutionLock, "get_active_owners", return_value={}),
        patch.object(diagnose.DeviceExecutionLock, "get_queued_tasks", return_value=[]),
        patch.object(IntegrationHostProbe, "probe", AsyncMock(return_value=_host())),
        patch.object(
            diagnose,
            "_helper_status",
            lambda serial: {**_helper_healthy(serial), "serial": serial, "backend": "auto"},
        ),
    ):
        result = asyncio.run(mobile_diagnose(attempt_fix=True))

    heal.assert_awaited_once()
    restart.assert_not_awaited()  # healing already restarted ADB
    assert run_all.await_count == 2
    assert result["verdict"] == "ready"
    assert result["fixes_applied"] == [
        {"fix": "heal_adb_keys", "success": True, "message": "keys regenerated", "skipped": False}
    ]
    assert any(s.startswith("Auto-fix heal_adb_keys applied") for s in result["next_steps"])


def test_attempt_fix_restarts_adb_only_without_ready_device_or_active_task(temp_trace_env):
    no_device = _no_device_probes([])
    restart = AsyncMock(return_value={"success": False, "message": "adb start-server failed"})

    with patch.object(diagnose.readiness_engine, "restart_adb_server", restart):
        result = _run(no_device, attempt_fix=True)
    restart.assert_awaited_once()
    assert result["fixes_applied"][0]["fix"] == "restart_adb_server"
    assert result["fixes_applied"][0]["success"] is False
    assert any("did not help" in s for s in result["next_steps"])

    restart.reset_mock()
    with patch.object(diagnose.readiness_engine, "restart_adb_server", restart):
        result = _run(no_device, active_owners={"d": _owner("d")}, attempt_fix=True)
    restart.assert_not_awaited()
    assert result["fixes_applied"][0]["skipped"] is True

    # A ready device means there is nothing to restart, even with attempt_fix.
    restart.reset_mock()
    with patch.object(diagnose.readiness_engine, "restart_adb_server", restart):
        result = _run(_healthy_probes(), attempt_fix=True)
    restart.assert_not_awaited()
    assert result["fixes_applied"] == []


def test_attempt_fix_records_stale_lock_cleanup_only_when_something_was_removed(temp_trace_env):
    run_all = AsyncMock(return_value=_report(_healthy_probes()))

    result = _run(_healthy_probes(), run_all=run_all, cleanup=0, attempt_fix=True)
    assert result["fixes_applied"] == []
    assert run_all.await_count == 1

    result = _run(_healthy_probes(), run_all=run_all, cleanup=2, attempt_fix=True)
    assert result["fixes_applied"] == [
        {
            "fix": "cleanup_stale_locks",
            "success": True,
            "skipped": False,
            "message": "removed 2 stale device lock(s) / queue ticket(s)",
        }
    ]
    assert run_all.await_count == 2  # lock cleanup does not change probe results: no re-run
    assert any(s.startswith("Auto-fix cleanup_stale_locks applied") for s in result["next_steps"])

    failing = MagicMock(side_effect=PermissionError("locked dir"))
    result = _run(_healthy_probes(), cleanup=failing, attempt_fix=True)
    assert result["fixes_applied"] == [
        {
            "fix": "cleanup_stale_locks",
            "success": False,
            "skipped": False,
            "message": "cleanup raised PermissionError: locked dir",
        }
    ]

    # Without attempt_fix the cleanup is never attempted.
    failing.reset_mock()
    _run(_healthy_probes(), cleanup=failing)
    failing.assert_not_called()


# --------------------------------------------------------------------------- #
# probe_device
# --------------------------------------------------------------------------- #


def test_probe_device_ok_keeps_verdict_ready(temp_trace_env):
    smoke = AsyncMock(return_value=_smoke_ok())
    result = _run(_healthy_probes(), smoke=smoke, probe_device=True)

    smoke.assert_awaited_once_with("pixel-1")
    assert result["verdict"] == "ready"
    assert result["device_probe"] == _smoke_ok()
    assert not any("Device probe failed" in s for s in result["next_steps"])


def test_probe_device_failure_blocks_with_fix_guidance(temp_trace_env):
    smoke = AsyncMock(
        return_value={
            "ok": False,
            "serial": "pixel-1",
            "elapsed_seconds": 8.1,
            "screenshot_bytes": None,
            "element_count": None,
            "error": "uiautomator dump timed out after 8s",
            "fix": ["Unlock the device.", "Run: adb -s pixel-1 shell input keyevent 82"],
        }
    )
    result = _run(_healthy_probes(), smoke=smoke, device_serial="pixel-1", probe_device=True)

    smoke.assert_awaited_once_with("pixel-1")
    assert result["verdict"] == "blocked"
    steps = result["next_steps"]
    idx = steps.index(
        "[REQUIRED] Device probe failed on pixel-1: uiautomator dump timed out after 8s"
    )
    assert steps[idx + 1] == "  Guidance: Unlock the device."
    assert steps[idx + 2] == "  Guidance: Run: adb -s pixel-1 shell input keyevent 82"
    assert "Device probe: uiautomator dump timed out" in result["summary"]


def test_probe_device_is_skipped_without_a_ready_device(temp_trace_env):
    smoke = AsyncMock()
    result = _run(_no_device_probes([]), smoke=smoke, probe_device=True)

    smoke.assert_not_awaited()
    assert result["device_probe"]["ok"] is False
    assert "no authorized device" in result["device_probe"]["error"]


# --------------------------------------------------------------------------- #
# Timeout / logs
# --------------------------------------------------------------------------- #


def test_hung_probe_yields_blocked_verdict_instead_of_hanging(temp_trace_env, monkeypatch):
    async def _never():
        await asyncio.sleep(10)

    monkeypatch.setattr(diagnose, "DIAGNOSIS_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(diagnose, "collect_readiness", _never)
    with (
        patch.object(diagnose.DeviceExecutionLock, "get_active_owners", return_value={}),
        patch.object(diagnose.DeviceExecutionLock, "get_queued_tasks", return_value=[]),
    ):
        result = asyncio.run(mobile_diagnose())

    assert result["verdict"] == "blocked"
    assert result["checks"] == []
    assert result["next_steps"][:2] == ["Run: adb kill-server", "Run: adb start-server"]
    assert "hung" in result["summary"]
    for field in ("device", "credentials", "device_probe"):
        assert result[field] is None
    assert result["tasks"] == {"active": [], "queued": []}


def test_hung_device_probe_still_times_out(temp_trace_env, monkeypatch):
    async def _hang(serial):
        await asyncio.sleep(10)

    monkeypatch.setattr(diagnose, "DIAGNOSIS_TIMEOUT_SECONDS", 0.05)
    result = _run(_healthy_probes(), smoke=AsyncMock(side_effect=_hang), probe_device=True)
    assert result["verdict"] == "blocked"
    assert "hung" in result["summary"]


def test_logs_surface_recent_errors_and_last_failed_task(temp_trace_env, monkeypatch):
    monkeypatch.setattr(diagnose.env_utils, "get_project_root", lambda: temp_trace_env)
    scratch = diagnose.Path(temp_trace_env) / "scratch"
    scratch.mkdir()
    (scratch / "mcp_stderr.log").write_text(
        "info: booted\n\x1b[33mTraceback (most recent call last):\x1b[0m\nModuleNotFoundError: No module named 'x'\nall good\n",
        encoding="utf-8",
    )
    trace_store.init_trace("ok-trace", "fine", "Flash", None)
    trace_store.update_trace_status("ok-trace", "completed")
    trace_store.init_trace("bad-trace", "broken", "Flash", None)
    trace_store.update_trace_status("bad-trace", "failed", error="adb: device offline")
    with open(trace_store.get_trace_stderr_log_path("bad-trace"), "w", encoding="utf-8") as fh:
        fh.write("starting runner\n")
        fh.write("ERROR adb: device offline\n")
        fh.write("Traceback (most recent call last):\n")
        fh.write("RuntimeError: device offline\n")

    result = _run(_healthy_probes())

    logs = result["logs"]
    assert logs["recent_mcp_errors"] == [
        "Traceback (most recent call last):",
        "ModuleNotFoundError: No module named 'x'",
    ]
    failed = logs["last_failed_task"]
    assert failed["trace_id"] == "bad-trace"
    assert failed["error"] == "adb: device offline"
    assert failed["stderr_log"].endswith("stderr.log")
    assert failed["recent_errors"] == [
        "ERROR adb: device offline",
        "Traceback (most recent call last):",
        "RuntimeError: device offline",
    ]


# --------------------------------------------------------------------------- #
# Accessibility helper (UI hierarchy backend)
# --------------------------------------------------------------------------- #


def test_missing_helper_in_auto_mode_degrades_with_optional_step(temp_trace_env):
    status = {
        **_helper_healthy(),
        "installed": False,
        "installed_version": None,
        "reachable": False,
    }
    result = _run(_healthy_probes(), helper_status=status)

    assert result["verdict"] == "degraded"
    helper = result["device"] is None or result["device"]["accessibility_helper"]
    assert helper
    optional = [s for s in result["next_steps"] if s.startswith("[OPTIONAL] Accessibility helper")]
    assert len(optional) == 1 and "is not installed" in optional[0]
    assert "  Run: uv run artemis helper install --serial pixel-1" in result["next_steps"]
    assert any("attempt_fix=true" in s for s in result["next_steps"])
    result["_fake_helper"].provision.assert_not_called()


def test_missing_helper_in_helper_mode_blocks(temp_trace_env):
    status = {**_helper_healthy(), "installed": False, "reachable": False}
    result = _run(_healthy_probes(), helper_status=status, backend="helper")

    assert result["verdict"] == "blocked"
    assert any(s.startswith("[REQUIRED] Accessibility helper") for s in result["next_steps"])


def test_uiautomator_backend_ignores_helper_state(temp_trace_env):
    status = {**_helper_healthy(), "installed": False, "reachable": False}
    result = _run(_healthy_probes(), helper_status=status, backend="uiautomator")

    assert result["verdict"] == "ready"
    assert not any("Accessibility helper" in s for s in result["next_steps"])


def test_outdated_helper_reports_versions(temp_trace_env):
    status = {**_helper_healthy(), "installed_version": 1, "outdated": True}
    result = _run(_healthy_probes(), helper_status=status)

    step = next(s for s in result["next_steps"] if "Accessibility helper" in s)
    assert "device has version 1, bundled is 2" in step


def test_attempt_fix_provisions_missing_helper_on_idle_device(temp_trace_env):
    status = {
        **_helper_healthy(),
        "installed": False,
        "installed_version": None,
        "reachable": False,
    }
    result = _run(_healthy_probes(), helper_status=status, attempt_fix=True)

    result["_fake_helper"].provision.assert_called_once_with("pixel-1")
    fix = next(f for f in result["fixes_applied"] if f["fix"] == "install_accessibility_helper")
    assert fix["success"] is True and fix["skipped"] is False
    assert "installed" in fix["message"]


def test_attempt_fix_skips_helper_install_while_task_holds_device(temp_trace_env):
    status = {**_helper_healthy(), "installed": False, "reachable": False}
    result = _run(
        _healthy_probes(),
        helper_status=status,
        attempt_fix=True,
        active_owners={"pixel-1": _owner()},
    )

    result["_fake_helper"].provision.assert_not_called()
    fix = next(f for f in result["fixes_applied"] if f["fix"] == "install_accessibility_helper")
    assert fix["skipped"] is True


def test_attempt_fix_leaves_healthy_helper_alone(temp_trace_env):
    result = _run(_healthy_probes(), attempt_fix=True)

    result["_fake_helper"].provision.assert_not_called()
    assert not any(f["fix"] == "install_accessibility_helper" for f in result["fixes_applied"])


def test_disabled_helper_step_includes_the_manual_path(temp_trace_env):
    from artemis.runtime.helper_manager import MANUAL_ENABLE_PATH

    status = {**_helper_healthy(), "enabled": False, "reachable": False}
    result = _run(_healthy_probes(), helper_status=status)
    step = next(s for s in result["next_steps"] if "Accessibility helper" in s)
    assert "service is disabled" in step
    assert any(MANUAL_ENABLE_PATH in s for s in result["next_steps"])


def test_newer_helper_than_bundle_is_not_a_finding(temp_trace_env):
    status = {**_helper_healthy(), "installed_version": 9, "newer_than_bundled": True}
    result = _run(_healthy_probes(), helper_status=status)
    assert result["verdict"] == "ready"
    assert not any("Accessibility helper" in s for s in result["next_steps"])
