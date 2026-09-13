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

import time
import uiautomator2 as u2


def test():
    device = u2.connect("emulator-5554")
    print("Testing send_keys with immediate IME switch off")
    device.set_fastinput_ime(True)
    device.send_keys("Test 1")
    device.set_fastinput_ime(False)

    time.sleep(2)
    print("Testing send_keys with delay before IME switch off")
    device.set_fastinput_ime(True)
    device.send_keys("Test 2")
    time.sleep(1.0)
    device.set_fastinput_ime(False)


if __name__ == "__main__":
    test()
