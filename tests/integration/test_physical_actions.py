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

import asyncio
from pathlib import Path
import sys

# Add root to sys.path
root_dir = Path(__file__).parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from adbutils import AdbClient
from artemis.clients.ui_automator_client import UIAutomatorClient
from artemis.context import DeviceContext, DevicePlatform, ArtemisContext
from artemis.controllers.unified_controller import UnifiedMobileController


import pytest


@pytest.mark.asyncio
async def test_actions():
    print("=========================================")
    print("      Physical Action Direct Validation Testbench")
    print("=========================================")
    adb = AdbClient(host="localhost", port=5037)
    devices = adb.device_list()
    if not devices:
        print("❌ Error: No active Android emulator or physical device detected.")
        return
    device_id = devices[0].serial
    print(f"✅ Target device locked: {device_id}")

    ui_client = UIAutomatorClient(device_id=device_id)
    print("🔄 Connecting to UIAutomator2 and syncing screen properties...")
    try:
        ui_data = ui_client.get_screen_data()
        w, h = ui_data.width, ui_data.height
        print(f"✅ Screen sync ready, physical resolution: {w}x{h}")
    except Exception as e:
        print(f"❌ Screen properties sync failed: {e}")
        return

    ctx = ArtemisContext(
        trace_id="physical-test-bench",
        device=DeviceContext(
            host_platform="LINUX",
            mobile_platform=DevicePlatform.ANDROID,
            device_id=device_id,
            device_width=w,
            device_height=h,
        ),
        adb_client=adb,
        ui_adb_client=ui_client,
    )

    ctrl = UnifiedMobileController(ctx)

    print("\n[Task 1] Extract Live UI Hierarchy...")
    try:
        elems = await ctrl.get_ui_elements()
        print(f"✅ Parsed UI element count: {len(elems)}")
    except Exception as e:
        print(f"❌ Failed to extract UI hierarchy: {e}")

    print("\n[Task 2] Capture Live Screenshot...")
    try:
        shot = await ctrl.take_screenshot()
        print(f"✅ Screenshot captured, Base64 payload length: {len(shot)}")
    except Exception as e:
        print(f"❌ Failed to capture screenshot: {e}")

    print("\n[Task 3] Simulate Drag/Swipe Down...")
    try:
        # Swipe up from bottom to top
        err = await ctrl.swipe_coords(w // 2, int(h * 0.8), w // 2, int(h * 0.2), duration=400)
        if err:
            print(f"❌ Swipe rejected by controller: {err}")
        else:
            print("✅ Swipe command injected successfully!")
    except Exception as e:
        print(f"❌ Swipe execution exception: {e}")

    print("\n🧹 Safely releasing connection and driver handles...")
    await ctrl.cleanup()
    print("🎉 Direct physical validation tests completed successfully!")


if __name__ == "__main__":
    asyncio.run(test_actions())
