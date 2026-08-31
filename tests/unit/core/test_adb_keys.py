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

"""Unit tests for ADB RSA key inspection and automated self-healing."""

from pathlib import Path
from unittest.mock import patch

import pytest
from artemis.core.diagnostics.adb_keys import (
    AdbKeyStatus,
    get_adb_key_paths,
    heal_adb_keys,
    inspect_adb_keys,
)
from artemis.core.diagnostics.engine import ReadinessEngine
from artemis.core.diagnostics.probes.adb_probe import AdbDeviceProbe
from artemis.core.diagnostics.schema import DeviceInfo, ProbeStatus


def test_get_adb_key_paths_default():
    """Verify default ADB key paths are under ~/.android."""
    priv, pub = get_adb_key_paths()
    assert priv.name == "adbkey"
    assert pub.name == "adbkey.pub"
    assert ".android" in str(priv)


def test_get_adb_key_paths_custom_env(monkeypatch, tmp_path):
    """Verify custom ADB_VENDOR_KEYS environment variable is respected."""
    custom_key = tmp_path / "custom_adbkey"
    monkeypatch.setenv("ADB_VENDOR_KEYS", str(custom_key))

    priv, pub = get_adb_key_paths()
    assert priv == custom_key
    assert pub == Path(f"{custom_key}.pub")


def test_inspect_adb_keys_not_exist(monkeypatch, tmp_path):
    """When keys do not exist yet, they are considered valid (will auto-generate on first connect)."""
    priv = tmp_path / "adbkey"
    pub = tmp_path / "adbkey.pub"
    monkeypatch.setattr(
        "artemis.core.diagnostics.adb_keys.get_adb_key_paths", lambda: (priv, pub)
    )

    status = inspect_adb_keys()
    assert status.is_valid is True
    assert status.is_corrupted is False
    assert status.exists is False


def test_inspect_adb_keys_zero_bytes(monkeypatch, tmp_path):
    """When private key is 0-bytes, it must be detected as corrupted."""
    priv = tmp_path / "adbkey"
    pub = tmp_path / "adbkey.pub"
    priv.write_text("")
    pub.write_text("")

    monkeypatch.setattr(
        "artemis.core.diagnostics.adb_keys.get_adb_key_paths", lambda: (priv, pub)
    )

    status = inspect_adb_keys()
    assert status.is_valid is False
    assert status.is_corrupted is True
    assert status.exists is True
    assert status.key_size_bytes == 0
    assert "0 bytes" in (status.error_reason or "")


def test_inspect_adb_keys_invalid_pem_header(monkeypatch, tmp_path):
    """When private key is non-empty but does not contain PEM headers, it is corrupted."""
    priv = tmp_path / "adbkey"
    pub = tmp_path / "adbkey.pub"
    priv.write_text("GARBAGE DATA WITHOUT PEM HEADER " * 10)
    pub.write_text("some pubkey")

    monkeypatch.setattr(
        "artemis.core.diagnostics.adb_keys.get_adb_key_paths", lambda: (priv, pub)
    )

    status = inspect_adb_keys()
    assert status.is_valid is False
    assert status.is_corrupted is True
    assert "PEM" in (status.error_reason or "")


def test_inspect_adb_keys_valid(monkeypatch, tmp_path):
    """When private key has valid PEM structure and size, it is valid."""
    priv = tmp_path / "adbkey"
    pub = tmp_path / "adbkey.pub"
    priv.write_text(
        "-----BEGIN PRIVATE KEY-----\nMIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQC...\n-----END PRIVATE KEY-----\n"
    )
    pub.write_text("ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC... user@host")

    monkeypatch.setattr(
        "artemis.core.diagnostics.adb_keys.get_adb_key_paths", lambda: (priv, pub)
    )

    status = inspect_adb_keys()
    assert status.is_valid is True
    assert status.is_corrupted is False
    assert status.exists is True
    assert status.key_size_bytes > 0
    assert status.error_reason is None


def test_heal_adb_keys_healthy_no_op(monkeypatch, tmp_path):
    """When keys are healthy and force is False, heal_adb_keys is a no-op."""
    priv = tmp_path / "adbkey"
    pub = tmp_path / "adbkey.pub"
    priv.write_text(
        "-----BEGIN PRIVATE KEY-----\nMIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQC...\n-----END PRIVATE KEY-----\n"
    )
    pub.write_text("ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC... user@host")

    monkeypatch.setattr(
        "artemis.core.diagnostics.adb_keys.get_adb_key_paths", lambda: (priv, pub)
    )

    res = heal_adb_keys(force=False)
    assert res["success"] is True
    assert res["repaired"] is False


def test_heal_adb_keys_corrupted(monkeypatch, tmp_path):
    """When keys are corrupted (0 bytes), heal_adb_keys deletes them and restarts ADB."""
    priv = tmp_path / "adbkey"
    pub = tmp_path / "adbkey.pub"
    priv.write_text("")
    pub.write_text("")

    monkeypatch.setattr(
        "artemis.core.diagnostics.adb_keys.get_adb_key_paths", lambda: (priv, pub)
    )

    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        res = heal_adb_keys()

    assert res["success"] is True
    assert res["repaired"] is True
    # The corrupted 0-byte file was removed
    assert not priv.exists()


@pytest.mark.asyncio
async def test_adb_probe_detects_corrupted_key_with_unauthorized_device(monkeypatch, tmp_path):
    """When a device is unauthorized and keys are corrupted, probe reports ADB Key Corrupted."""
    priv = tmp_path / "adbkey"
    pub = tmp_path / "adbkey.pub"
    priv.write_text("")
    monkeypatch.setattr(
        "artemis.core.diagnostics.adb_keys.get_adb_key_paths", lambda: (priv, pub)
    )

    probe = AdbDeviceProbe()
    monkeypatch.setattr(probe, "_locate_adb", lambda: "/usr/bin/adb")

    async def mock_adb_ver(p):
        return "Android Debug Bridge version 1.0.41"

    monkeypatch.setattr(probe, "_get_adb_version", mock_adb_ver)

    async def mock_devices(adb_path):
        return [DeviceInfo(serial="TEST_SERIAL", state="unauthorized")]

    monkeypatch.setattr(probe, "_parse_adb_devices", mock_devices)

    result = await probe.probe()
    assert result.status == ProbeStatus.WARN
    assert result.summary == "ADB Key Corrupted"
    assert "0 bytes" in result.description
    assert any(a.label == "Auto-Heal ADB Keys" for a in result.actions)


@pytest.mark.asyncio
async def test_readiness_engine_heal_adb_keys():
    """Verify ReadinessEngine exposes heal_adb_keys async method."""
    engine = ReadinessEngine()
    with patch("artemis.core.diagnostics.adb_keys.heal_adb_keys") as mock_heal:
        mock_heal.return_value = {"success": True, "repaired": True, "message": "Repaired"}
        res = await engine.heal_adb_keys()
        assert res["success"] is True
        assert res["repaired"] is True
