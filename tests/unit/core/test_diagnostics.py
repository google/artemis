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
async def test_active_device_selection():
    """Verify selecting a device updates engine state."""
    engine = ReadinessEngine()
    engine.set_active_device_serial("test-emulator-1234")
    assert engine.get_active_device_serial() == "test-emulator-1234"


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
