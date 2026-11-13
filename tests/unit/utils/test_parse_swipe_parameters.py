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

"""Tests for duration handling in parse_swipe_parameters."""

from artemis.utils.coordinates import parse_swipe_parameters


def test_integer_zero_duration_is_honored_not_defaulted():
    # An explicit integer 0 must be honored (0ms), matching how the string "0"
    # is already treated. Previously integer 0 was falsy and silently replaced
    # by the default, so the same value behaved differently across types.
    _, _, from_int = parse_swipe_parameters({"direction": "up", "duration": 0})
    _, _, from_str = parse_swipe_parameters({"direction": "up", "duration": "0"})
    assert from_int == 0
    assert from_str == 0


def test_missing_duration_uses_default():
    _, _, duration = parse_swipe_parameters({"direction": "up"})
    assert duration == 800


def test_duration_precedence_prefers_duration_key():
    _, _, duration = parse_swipe_parameters(
        {"direction": "up", "duration": 100, "duration_ms": 200}
    )
    assert duration == 100


def test_unusable_higher_priority_duration_falls_through():
    # An empty string in the higher-priority key is not a usable duration, so
    # it must fall through to the next key rather than being taken and dropped.
    _, _, duration = parse_swipe_parameters({"duration": "", "duration_ms": 500})
    assert duration == 500


def test_boolean_duration_is_ignored():
    # Booleans are not durations; they must not be honored (True/False), and a
    # usable lower-priority key should still win.
    _, _, duration = parse_swipe_parameters({"duration": False, "duration_ms": 500})
    assert duration == 500
