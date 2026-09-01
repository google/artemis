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

"""Unit tests for Artemis System Diagnostics & Readiness Engine."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from artemis.core.diagnostics.engine import ReadinessEngine
from artemis.core.diagnostics.probes.adb_probe import AdbDeviceProbe
from artemis.core.diagnostics.probes.credentials_probe import (
    LLMCredentialsProbe,
    VisionOCRProbe,
)
from artemis.core.diagnostics.probes.runtime_probe import (
    PythonRuntimeProbe,
    SystemConfigProbe,
)
from artemis.core.diagnostics.probes.toolchain_probe import ToolchainProbe
from artemis.core.diagnostics.schema import (
    ProbeCategory,
    ProbeResult,
    ProbeStatus,
    SystemReadinessReport,
)


@pytest.mark.asyncio
async def test_readiness_engine_run_all():
    """Verify that ReadinessEngine aggregates probes and builds structured report."""
    engine = ReadinessEngine()
    report: SystemReadinessReport = await engine.run_all()

    assert isinstance(report, SystemReadinessReport)
    assert report.blocker_count >= 3
    assert isinstance(report.probes, list)
    assert len(report.probes) >= 5

    probe_ids = [p.id for p in report.probes]
    assert "python_runtime" in probe_ids
    assert "system_config" in probe_ids
    assert "android_adb" in probe_ids
    assert "gemini_api_key" in probe_ids
    assert "vision_ocr_key" in probe_ids
    assert "toolchain" in probe_ids


@pytest.mark.asyncio
async def test_python_runtime_probe_structure():
    """Verify PythonRuntimeProbe returns valid runtime inspection result."""
    probe = PythonRuntimeProbe()
    assert probe.probe_id == "python_runtime"
    assert probe.category == ProbeCategory.RUNTIME
    assert probe.is_blocker is True

    result: ProbeResult = await probe.probe()
    assert isinstance(result, ProbeResult)
    assert result.status in (ProbeStatus.PASS, ProbeStatus.FAIL)
    assert "version" in result.metadata
    assert "executable" in result.metadata


@pytest.mark.asyncio
async def test_system_config_probe_structure():
    """Verify SystemConfigProbe checks configuration file health."""
    probe = SystemConfigProbe()
    assert probe.probe_id == "system_config"
    assert probe.category == ProbeCategory.RUNTIME
    assert probe.is_blocker is True

    result: ProbeResult = await probe.probe()
    assert isinstance(result, ProbeResult)
    assert result.status in (ProbeStatus.PASS, ProbeStatus.FAIL)
    assert "valid" in result.metadata


@pytest.mark.asyncio
async def test_vision_ocr_probe_structure():
    """Verify VisionOCRProbe returns optional non-blocker status."""
    probe = VisionOCRProbe()
    assert probe.probe_id == "vision_ocr_key"
    assert probe.category == ProbeCategory.CREDENTIALS
    assert probe.is_blocker is False

    result: ProbeResult = await probe.probe()
    assert isinstance(result, ProbeResult)
    assert result.status == ProbeStatus.PASS
    assert "configured" in result.metadata


@pytest.mark.asyncio
async def test_adb_probe_structure():
    """Verify AdbDeviceProbe returns correct category, blocker status, and schema."""
    probe = AdbDeviceProbe()
    assert probe.probe_id == "android_adb"
    assert probe.category == ProbeCategory.DEVICE
    assert probe.is_blocker is True

    result: ProbeResult = await probe.probe()
    assert isinstance(result, ProbeResult)
    assert result.status in (ProbeStatus.PASS, ProbeStatus.WARN, ProbeStatus.FAIL)
    assert "installed" in result.metadata


@pytest.mark.parametrize(
    ("policy_output", "trust_output", "expected"),
    [
        (
            "KeyguardServiceDelegate\n  showing=true\n  occluded=false\n",
            'User "Owner" (current): deviceLocked=1',
            True,
        ),
        (
            "KeyguardServiceDelegate\n  showing=false\n  occluded=false\n",
            'User "Owner" (current): deviceLocked=0',
            False,
        ),
        (
            "mShowingLockscreen=true mKeyguardOccluded=false",
            "",
            True,
        ),
        ("", "", None),
    ],
)
def test_adb_probe_parses_device_lock_state(policy_output, trust_output, expected):
    """Keyguard and current-user trust signals produce a fail-safe lock state."""
    assert AdbDeviceProbe._parse_device_lock_state(policy_output, trust_output) is expected


def test_modern_unlock_state_is_not_overridden_by_legacy_fields():
    policy = (
        "KeyguardServiceDelegate\n"
        "  showing=false\n"
        "mShowingLockscreen=true mKeyguardOccluded=false"
    )
    trust = 'User "Owner" (current): deviceLocked=0'

    assert AdbDeviceProbe._parse_device_lock_state(policy, trust) is False


@pytest.mark.asyncio
async def test_positive_lock_state_requires_confirmation(monkeypatch):
    probe = AdbDeviceProbe()
    raw_probe = AsyncMock(side_effect=[True, None])
    monkeypatch.setattr(probe, "_get_device_lock_state", raw_probe)

    result = await probe._get_confirmed_device_lock_state("adb", "device-1")

    assert result is None
    assert raw_probe.await_count == 2


@pytest.mark.asyncio
async def test_dashboard_reuses_recent_confirmed_state_on_one_timeout(monkeypatch):
    probe = AdbDeviceProbe()
    confirmed_probe = AsyncMock(side_effect=[False, None])
    monkeypatch.setattr(
        probe, "_get_confirmed_device_lock_state", confirmed_probe
    )

    assert await probe._get_dashboard_lock_state("adb", "device-1") is False
    assert await probe._get_dashboard_lock_state("adb", "device-1") is False
    assert probe._lock_state_sources["device-1"] == "recent_confirmed"


@pytest.mark.asyncio
async def test_readiness_engine_coalesces_concurrent_full_scans():
    engine = ReadinessEngine()
    result = ProbeResult(
        id="test_probe",
        category=ProbeCategory.RUNTIME,
        title="Test",
        status=ProbeStatus.PASS,
        is_blocker=True,
        summary="Ready",
        description="Ready",
    )

    async def slow_probe():
        await asyncio.sleep(0.01)
        return result

    probe = Mock()
    probe.probe = AsyncMock(side_effect=slow_probe)
    engine._probes = {"test_probe": probe}

    reports = await asyncio.gather(*(engine.run_all() for _ in range(8)))

    assert probe.probe.await_count == 1
    assert all(report.overall_ready for report in reports)


@pytest.mark.asyncio
async def test_readiness_engine_reuses_cache_until_forced():
    engine = ReadinessEngine()
    result = ProbeResult(
        id="test_probe",
        category=ProbeCategory.RUNTIME,
        title="Test",
        status=ProbeStatus.PASS,
        is_blocker=True,
        summary="Ready",
        description="Ready",
    )
    probe = Mock()
    probe.probe = AsyncMock(return_value=result)
    engine._probes = {"test_probe": probe}

    first = await engine.run_all()
    cached = await engine.run_all()
    refreshed = await engine.run_all(force_refresh=True)

    assert probe.probe.await_count == 2
    assert cached.timestamp == first.timestamp
    assert refreshed.timestamp >= first.timestamp


@pytest.mark.asyncio
async def test_invalidation_prevents_in_flight_report_from_becoming_shared_cache():
    engine = ReadinessEngine()
    result = ProbeResult(
        id="test_probe",
        category=ProbeCategory.RUNTIME,
        title="Test",
        status=ProbeStatus.PASS,
        is_blocker=True,
        summary="Ready",
        description="Ready",
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def controlled_probe():
        started.set()
        await release.wait()
        return result

    probe = Mock()
    probe.probe = AsyncMock(side_effect=controlled_probe)
    engine._probes = {"test_probe": probe}

    old_scan = asyncio.create_task(engine.run_all())
    await started.wait()
    engine.invalidate_cache()
    release.set()
    await old_scan
    await engine.run_all()

    assert probe.probe.await_count == 2


@pytest.mark.asyncio
async def test_submission_probe_skips_full_device_enrichment(monkeypatch):
    probe = AdbDeviceProbe(target_serial="device-2")
    get_states = AsyncMock(
        return_value=[("device-1", "device"), ("device-2", "device")]
    )
    get_lock_state = AsyncMock(return_value=False)
    full_probe = AsyncMock()
    monkeypatch.setattr(
        "artemis.core.diagnostics.probes.adb_probe.toolchain.resolve",
        lambda name: "adb",
    )
    monkeypatch.setattr(probe, "_get_device_states", get_states)
    monkeypatch.setattr(probe, "_get_device_lock_state", get_lock_state)
    monkeypatch.setattr(probe, "_parse_adb_devices", full_probe)

    result = await probe.probe_submission_readiness()

    assert result.summary == "Connected"
    assert result.metadata["submission_probe"] is True
    get_states.assert_awaited_once_with("adb")
    get_lock_state.assert_awaited_once_with(
        "adb", "device-2", timeout_seconds=1.0
    )
    full_probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_submission_probe_fails_closed_when_lock_state_is_unknown(monkeypatch):
    probe = AdbDeviceProbe()
    monkeypatch.setattr(
        "artemis.core.diagnostics.probes.adb_probe.toolchain.resolve",
        lambda name: "adb",
    )
    monkeypatch.setattr(
        probe,
        "_get_device_states",
        AsyncMock(return_value=[("device-1", "device")]),
    )
    monkeypatch.setattr(
        probe,
        "_get_device_lock_state",
        AsyncMock(return_value=None),
    )

    result = await probe.probe_submission_readiness()

    assert result.summary == "Lock State Unknown"
    assert result.status == ProbeStatus.WARN


@pytest.mark.asyncio
async def test_submission_probe_falls_back_to_unlocked_device(monkeypatch):
    """When the first device is locked but a second device is unlocked, submission probe falls back."""
    probe = AdbDeviceProbe()
    monkeypatch.setattr(
        probe,
        "_get_device_states",
        AsyncMock(return_value=[("device-locked", "device"), ("device-unlocked", "device")]),
    )

    async def mock_lock_state(adb_path, serial, timeout_seconds=1.0):
        return True if serial == "device-locked" else False

    monkeypatch.setattr(probe, "_get_confirmed_device_lock_state", mock_lock_state)

    result = await probe.probe_submission_readiness()

    assert result.status == ProbeStatus.PASS
    assert result.summary == "Connected"
    assert result.metadata["active_device"]["serial"] == "device-unlocked"
    assert result.metadata["active_device"]["is_locked"] is False


@pytest.mark.asyncio
async def test_adb_probe_prefers_unlocked_device_when_one_is_locked(monkeypatch):
    """When multiple ready devices exist, probe() should pick the unlocked one as active."""
    from artemis.core.diagnostics.schema import DeviceInfo

    probe = AdbDeviceProbe()
    monkeypatch.setattr(probe, "_locate_adb", lambda: "/usr/bin/adb")
    monkeypatch.setattr(probe, "_get_adb_version", AsyncMock(return_value="1.0.41"))
    monkeypatch.setattr(probe, "_locate_emulator", lambda: "/usr/bin/emulator")
    monkeypatch.setattr(probe, "_list_installed_avds", lambda _: [])

    devices = [
        DeviceInfo(serial="dev-locked-1", state="device", model="Pixel 7", is_locked=True),
        DeviceInfo(serial="dev-unlocked-2", state="device", model="Pixel 8", is_locked=False),
    ]
    monkeypatch.setattr(probe, "_parse_adb_devices", AsyncMock(return_value=devices))

    result = await probe.probe()

    assert result.status == ProbeStatus.PASS
    assert result.summary == "Connected"
    assert result.metadata["active_device"]["serial"] == "dev-unlocked-2"
    assert result.metadata["active_device"]["is_locked"] is False



@pytest.mark.asyncio
async def test_llm_credentials_probe_structure():
    """Verify LLMCredentialsProbe returns correct category and schema."""
    probe = LLMCredentialsProbe()
    assert probe.probe_id == "gemini_api_key"
    assert probe.category == ProbeCategory.CREDENTIALS
    assert probe.is_blocker is True

    result: ProbeResult = await probe.probe()
    assert isinstance(result, ProbeResult)
    assert result.status in (ProbeStatus.PASS, ProbeStatus.FAIL)
    assert "configured_count" in result.metadata


@pytest.mark.asyncio
async def test_toolchain_probe_structure():
    """Verify ToolchainProbe returns valid probe category, metadata, and schema."""
    probe = ToolchainProbe()
    assert probe.probe_id == "toolchain"
    assert probe.category == ProbeCategory.TOOLCHAIN
    assert probe.is_blocker is False

    result: ProbeResult = await probe.probe()
    assert isinstance(result, ProbeResult)
    assert result.status in (ProbeStatus.PASS, ProbeStatus.FAIL)
    assert "ffmpeg" in result.metadata
    assert "scrcpy" in result.metadata


@pytest.mark.asyncio
async def test_probe_target_serial_forwards_to_adb_probe():
    """Verify the probe target preference reaches the ADB probe and can be cleared."""
    engine = ReadinessEngine()
    engine.set_probe_target_serial("test-emulator-1234")
    assert engine._adb_probe._target_serial == "test-emulator-1234"
    engine.set_probe_target_serial(None)
    assert engine._adb_probe._target_serial is None


@pytest.mark.asyncio
async def test_credentials_probe_and_dynamic_update():
    """Verify dynamic API key updates and metadata reflection."""
    from artemis.config import settings

    settings.set_api_key("google", "test_gemini_key_1234567890", persist_to_env=False)

    probe = LLMCredentialsProbe()
    result = await probe.probe()
    assert result.status == ProbeStatus.PASS
    assert "current_key" in result.metadata
    assert result.metadata["current_key"] == "test_gemini_key_1234567890"
    assert "api_keys" in result.metadata
    assert result.metadata["api_keys"]["google"] == "test_gemini_key_1234567890"


@pytest.mark.asyncio
async def test_emulator_manager_lifecycle():
    """Verify EmulatorManager status querying, validation, and dismissal."""
    from artemis.core.diagnostics.emulator_manager import (
        EmulatorLaunchStage,
        EmulatorManager,
    )

    manager = EmulatorManager()
    status = manager.get_status()
    assert status.status == EmulatorLaunchStage.IDLE

    # Invalid empty AVD name
    empty_res = await manager.launch("   ")
    assert empty_res.status == EmulatorLaunchStage.FAILED
    assert "empty" in (empty_res.error or "").lower()

    # Dismiss state
    dismiss_res = manager.dismiss()
    assert dismiss_res["success"] is True
    assert manager.get_status().status == EmulatorLaunchStage.IDLE
