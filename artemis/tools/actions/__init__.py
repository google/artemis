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

"""Universal device action tools."""

from artemis.tools.actions.device_actions import (
    ClickArgs,
    InputTextArgs,
    LongPressArgs,
    PressKeyArgs,
    SwipeArgs,
    WaitForDelayArgs,
    click,
    input_text,
    long_press,
    press_key,
    swipe,
    wait_for_delay,
)

__all__ = [
    "click",
    "long_press",
    "input_text",
    "swipe",
    "press_key",
    "wait_for_delay",
    "ClickArgs",
    "LongPressArgs",
    "InputTextArgs",
    "SwipeArgs",
    "PressKeyArgs",
    "WaitForDelayArgs",
]
