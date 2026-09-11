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

"""Example: Accessibility Helper Diagnostics & Resilient Hierarchy Dumps.

This example shows how ARTEMIS uses its managed Accessibility Helper APK
to perform conflict-free UI tree inspection alongside external test frameworks
(e.g., Mobly, Appium) without locking the Android UiAutomation service.
"""

import asyncio
from artemis.controllers.platform_specific_commands_controller import get_first_device
from artemis.drivers.android.adb_driver import AndroidAdbDriver


async def main():
    device_id, platform, _ = get_first_device()
    if not device_id:
        print("⚠️ No connected Android device found via ADB.")
        return

    print(f"📱 Connected device: {device_id} (Platform: {platform})")

    # Initialize the Artemis Android ADB Driver
    driver = AndroidAdbDriver(device_id=device_id)

    # Capture screen data through Artemis managed hierarchy pipeline
    screen_data = await driver.get_screen_data()

    print(f"📐 Screen Dimensions: {screen_data.width}x{screen_data.height}")
    print(f"🌲 Hierarchy Elements Captured: {len(screen_data.elements)}")
    if screen_data.elements:
        first = screen_data.elements[0]
        print(f"   Root Element Class: {first.get('class', 'N/A')}")
        print(f"   Package: {first.get('package', 'N/A')}")

    await driver.disconnect()
    print("✅ Diagnostics complete.")


if __name__ == "__main__":
    asyncio.run(main())
