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

import pytest

from artemis.runtime.device_lock import DeviceExecutionLock
from artemis.runtime.device_pool import DevicePool, DeviceStatus


@pytest.fixture(autouse=True)
def isolated_lock_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "artemis.runtime.device_lock.get_temp_dir",
        lambda _name: tmp_path,
    )


def test_device_pool_parse_lines():
    lines = [
        "List of devices attached",
        "emulator-5554          device product:sdk_gphone64_arm64 model:sdk_gphone64_arm64 device:emu64a",
        "9A123XYZ               device product:husky model:Pixel_8_Pro device:husky",
        "offline-dev            offline",
        "",
    ]
    parsed = DevicePool._parse_device_lines(lines)
    assert len(parsed) == 3
    assert parsed[0] == ("emulator-5554", "device", "sdk gphone64 arm64", "sdk_gphone64_arm64")
    assert parsed[1] == ("9A123XYZ", "device", "Pixel 8 Pro", "husky")
    assert parsed[2] == ("offline-dev", "offline", None, None)


def test_device_pool_list_devices_and_busy_status(monkeypatch):
    pool = DevicePool()
    monkeypatch.setattr(
        pool,
        "_query_adb_devices_sync",
        lambda: [
            ("emulator-5554", "device", "Emulator", "sdk"),
            ("pixel-8-pro", "device", "Pixel 8 Pro", "husky"),
        ],
    )

    # Initially neither is busy
    devs = pool.list_devices()
    assert len(devs) == 2
    assert not devs[0].is_busy
    assert not devs[1].is_busy

    # Acquire lock for emulator-5554
    lock = DeviceExecutionLock("emulator-5554", "test automation")
    lock.acquire()
    try:
        devs = pool.list_devices()
        emu = next(d for d in devs if d.serial == "emulator-5554")
        pixel = next(d for d in devs if d.serial == "pixel-8-pro")

        assert emu.is_busy is True
        assert emu.active_task_desc == "test automation"
        assert pixel.is_busy is False

        # Test idle device filtering
        idle = pool.get_idle_devices()
        assert len(idle) == 1
        assert idle[0].serial == "pixel-8-pro"

        # Test select_device picks the idle device automatically
        selected = pool.select_device()
        assert selected == "pixel-8-pro"

        # Test explicit selection returns requested device
        assert pool.select_device("emulator-5554") == "emulator-5554"
    finally:
        lock.release()
