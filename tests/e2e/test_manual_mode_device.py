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

"""End-to-end manual-mode cycle against a real attached device.

Run explicitly via `make test-device` (never part of the default `make test`
suite): requires an attached, authorized Android device or emulator.
"""

import pytest

from artemis.runtime.device_lock import DeviceExecutionLock
import mcp_server.tools.manual_mode as manual_mode

pytestmark = pytest.mark.asyncio


@pytest.mark.android
async def test_observe_act_observe_cycle_releases_lock_on_session_end():
    first = await manual_mode.mobile_observe()
    assert first["status"] == "success"
    assert first["elements_text"]

    device_serial = first["device_serial"]
    session = manual_mode._registry.get_or_create(
        None if device_serial == "auto-select" else device_serial
    )
    assert session.indexed_elements, "no elements observed on the live device's home screen"

    act_result = await manual_mode.mobile_act(action="press_key", args={"key": "HOME"})
    assert act_result["status"] == "success"

    second = await manual_mode.mobile_observe()
    assert second["status"] == "success"

    end_result = manual_mode.mobile_session_end()
    assert end_result["ended"] is True

    # The device lock must be fully released: a fresh lock for the same
    # device must be acquirable immediately afterward.
    probe = DeviceExecutionLock(device_id="default", description="post-test probe")
    probe.acquire(blocking=False)
    probe.release()
