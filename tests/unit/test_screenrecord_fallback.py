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

"""Unit tests for native adb screenrecord fallback in AdbDriver and UnifiedMobileController."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.drivers.android.adb_driver import AndroidAdbDriver
from artemis.utils.video import detect_video_tools_enabled


def test_detect_video_tools_enabled_with_adb():
    """Verify detect_video_tools_enabled returns True when adb is present even without scrcpy."""
    with patch("shutil.which") as mock_which:
        mock_which.side_effect = lambda tool: "/usr/bin/adb" if tool == "adb" else None
        assert detect_video_tools_enabled() is True


@pytest.mark.asyncio
async def test_adb_driver_screenrecord_fallback(tmp_path):
    """Verify AdbDriver falls back to adb shell screenrecord when scrcpy fails."""
    mock_adb = MagicMock()
    driver = AndroidAdbDriver(device_id="test_device", adb_client=mock_adb)

    fake_mp4 = tmp_path / "recording.mp4"

    with patch("artemis.drivers.android.adb_driver.find_scrcpy", side_effect=Exception("No scrcpy")), \
         patch("artemis.drivers.android.adb_driver.find_adb", return_value="adb"), \
         patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        
        mock_proc = AsyncMock()
        mock_proc.returncode = None
        mock_exec.return_value = mock_proc

        await driver.start_video_recording(output_dir=tmp_path)
        assert driver._adb_record_process is not None

        # Simulate creation of output file upon pull
        fake_mp4.write_bytes(b"fake video data")

        result_path = await driver.stop_video_recording()
        assert result_path == str(fake_mp4)


@pytest.mark.asyncio
async def test_unified_controller_scrcpy_failure_fallback(tmp_path):
    """Verify UnifiedMobileController uses driver fallback when scrcpy spawn fails."""
    mock_driver = MagicMock()
    mock_driver.is_mock = False
    mock_driver.device_id = "test_device"
    mock_driver.start_video_recording = AsyncMock()
    fake_mp4 = tmp_path / "recording.mp4"
    fake_mp4.write_bytes(b"test mp4")
    mock_driver.stop_video_recording = AsyncMock(return_value=str(fake_mp4))

    ctx = MagicMock()
    ctx.device.device_id = "test_device"
    ctx.data_engine = None
    with patch("artemis.controllers.unified_controller.get_driver", return_value=mock_driver):
        controller = UnifiedMobileController(ctx)

    with patch.object(controller, "_spawn_scrcpy", side_effect=Exception("scrcpy error")), \
         patch("artemis.controllers.unified_controller.get_android_display_state", return_value=None):
        
        start_res = await controller.start_video_recording(output_dir=tmp_path)
        assert start_res.success is True
        assert "Fallback native recording started" in start_res.message
        mock_driver.start_video_recording.assert_called_once()

        stop_res = await controller.stop_video_recording()
        assert stop_res.success is True
        assert stop_res.video_path == fake_mp4
        mock_driver.stop_video_recording.assert_called_once()
