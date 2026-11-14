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

"""Shared platform constraints for the Flash and Pro device-action prompts."""


def device_action_guidance(ctx) -> str:
    if getattr(getattr(ctx, "device", None), "mobile_platform", None) != "ios":
        return ""
    return (
        "Target platform: iOS. For input_text, explicitly set clear_exist=false; "
        "type into an empty field or at its existing cursor. After typing into a URL "
        "or search field, submit it with press_key(enter) or by tapping the "
        "keyboard's Go/Search button; typing alone does not navigate. Whole-field "
        "clearing, Android keycodes, Back/Delete keys, ADB commands, and app "
        "locking are unavailable. iOS has no Back button: never call press_key "
        "with BACK; return to the previous screen with the app's back chevron "
        "(top-left) instead. press_key supports enter, home, power, volume_up, "
        "volume_down, and app_switch. manage_app accepts installed iOS display "
        "names or bundle identifiers. Action coordinates use the screenshot's "
        "normalized 0-1000 space.\n\n"
    )
